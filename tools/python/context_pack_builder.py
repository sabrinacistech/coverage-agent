"""context_pack_builder.py — Build minimal per-SUT context packs for LLM agents.

Reads state/batch-plan.json and, for each planned SUT, performs a surgical extraction
from the JSON state layer (stack-profile, classification-index, dependency-graph,
fixture-catalog, symbol-contracts, coverage-targets, import-whitelist).

Writes one compact JSON per SUT to: state/context-packs/<safe_fqcn>.json

The context-pack is the ONLY artifact LLM agents are allowed to consume.
No agent may open raw source code, pom.xml, build.gradle, or jacoco.xml.

Step 16 in run_pipeline.py  (--skip context).

Usage:
    python context_pack_builder.py --out state/
    python context_pack_builder.py --out state/ --sut com.example.MyService
    python context_pack_builder.py --out state/ --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, atomic_write_json, emit_tool_summary, fail, load_json, normalize_params, validate  # noqa: E402,F401

SCHEMA_NAME = "context-pack"
DEFAULT_MAX_IMPORTS = 40
TOKENS_PER_BYTE = 0.25  # rough estimate: ~4 bytes per token for JSON
# Declared Body-Agent input budget (docs/token-minimization-strategy.md). An
# entry whose estimatedTokensIn exceeds this is flagged overBudget so the
# orchestrator can shrink the pack or split the SUT before dispatch — turning
# the documented budget from a comment into an enforceable signal (audit M4).
MAX_TOKENS_IN = 4000

# P3.d: cap FAILED entries from failure-memory.json that are projected into
# each per-SUT pack. Capped to keep the repair-agent budget bounded.
#
# Selection policy (see project_failure_memory):
#   1. group entries by errorCode so distinct failure modes are always
#      represented before duplicates of the same errorCode are added;
#   2. within each errorCode, prefer distinct fixId values so the agent never
#      reapplies a fix that already failed;
#   3. fall back to recency (lastSeenCycle desc) for any remaining slots.
#
# Raised from 2 → 8 (audit 2026-05-28): the previous cap hid the failure
# history of cycles 3+ from the repair-agent, which led to repeated reuse of
# strategies that had already been proven to fail. 8 is enough to surface ~4
# distinct errorCodes with one historical retry each.
FAILURE_MEMORY_MAX_PER_SUT = 8

# Planner-only fields excluded from compact packs (P2.2).
_COMPACT_CLASSIFICATION_DROP = {
    "risk", "score", "reasons", "tags", "loc",
    "publicMethods", "cyclomatic", "coverage",
}


def _classification_bucket(class_type: str | None) -> tuple[str, str | None]:
    """Reduce classification.type to a compact (bucket, subtype?) tuple.

    Local-only mapping — does NOT modify classification_analyzer.py.
    """
    if not class_type:
        return ("unknown", None)
    web = {"controller"}
    svc = {"service", "component"}
    data = {"repository", "mapper"}
    cfg = {"configuration", "config"}
    inert = {
        "non-instantiable", "data-carrier", "enum", "dto",
        "entity", "exception", "util",
    }
    excluded = {"generated", "generated/excluded"}
    if class_type in web:
        return ("web", class_type)
    if class_type in svc:
        return ("svc", class_type)
    if class_type in data:
        return ("data", class_type)
    if class_type in cfg:
        return ("cfg", class_type)
    if class_type in inert:
        return ("inert", class_type)
    if class_type in excluded:
        return ("skip", class_type)
    return (class_type, None)

FORBIDDEN_ACTIONS = [
    "READ_SOURCE_CODE",
    "READ_POM",
    "READ_JACOCO_XML",
    "READ_CLASSPATH",
    "READ_BYTECODE",
    "INVENT_SYMBOL",
    "INVENT_IMPORT",
    "RETURN_RAW_JAVA",
    "CALL_UNLISTED_METHOD",
    "INSTANTIATE_UNLISTED_TYPE",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_fqcn(fqcn: str) -> str:
    """Convert FQCN to a filesystem-safe filename stem."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", fqcn)


def project_failure_memory(failure_memory: dict | None, sut_fqcn: str) -> list[dict]:
    """P3.d: select up to FAILURE_MEMORY_MAX_PER_SUT entries from
    state/failure-memory.json that are scoped to this SUT.

    Selection rules:
      - keep entries whose symbolFQN starts with ``sut_fqcn`` (e.g. the FQCN
        itself or a nested method/field reference);
      - prefer ``lastResult == "FAILED"`` (these are the ones the repair-agent
        must avoid retrying); SUCCESS entries are filtered out;
      - diversity-first ordering: pick one representative per
        (errorCode, fixId) tuple before doubling up on any single failure
        mode (G7 anti-loop relies on seeing the full set of attempted fixes);
      - within each tuple, prefer the most recent ``lastSeenCycle``;
      - finally cap to ``FAILURE_MEMORY_MAX_PER_SUT``.
    """
    if not failure_memory:
        return []
    entries = failure_memory.get("entries", []) if isinstance(failure_memory, dict) else []
    if not isinstance(entries, list):
        return []

    matched: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("lastResult", "")).upper() != "FAILED":
            continue
        symbol = str(entry.get("symbolFQN", ""))
        if not (symbol == sut_fqcn or symbol.startswith(sut_fqcn + ".") or symbol.startswith(sut_fqcn + "#")):
            continue
        matched.append(entry)

    matched.sort(key=lambda e: int(e.get("lastSeenCycle") or 0), reverse=True)

    # Diversity-first pass: pick at most one entry per (errorCode, fixId) tuple
    # so distinct failure modes are always represented; recency already broke
    # ties via the sort above.
    seen_keys: set[tuple[str, str]] = set()
    diverse: list[dict] = []
    leftovers: list[dict] = []
    for entry in matched:
        key = (str(entry.get("errorCode", "")), str(entry.get("fixId", "")))
        if key in seen_keys:
            leftovers.append(entry)
        else:
            seen_keys.add(key)
            diverse.append(entry)

    selected = (diverse + leftovers)[:FAILURE_MEMORY_MAX_PER_SUT]

    projected: list[dict] = []
    for entry in selected:
        row = {
            "hash": str(entry.get("hash", "")),
            "errorCode": str(entry.get("errorCode", "")),
            "symbolFQN": str(entry.get("symbolFQN", "")),
            "fixId": str(entry.get("fixId", "")),
            "lastResult": "FAILED",
        }
        attempts = entry.get("attempts")
        if isinstance(attempts, int):
            row["attempts"] = attempts
        for k in ("firstSeenCycle", "lastSeenCycle"):
            v = entry.get(k)
            if isinstance(v, int):
                row[k] = v
        tcid = entry.get("testCaseId")
        if isinstance(tcid, str) and tcid:
            row["testCaseId"] = tcid
        projected.append(row)
    return projected


def load_optional(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return load_json(path)
    except Exception as exc:
        print(f"[WARN] Could not load {path}: {exc}", file=sys.stderr)
        return None


# ── Extractors ────────────────────────────────────────────────────────────────

def extract_stack(stack_profile: dict | None) -> tuple[dict, bool, str | None]:
    """Build minimal stack block from stack-profile.json; return (stack, blocked, reason).

    Blocked when the profile is absent or lacks at least one module with confirmed
    test.framework and mock.framework — no framework defaults are ever assumed.
    """
    _MISSING: dict = {
        "javaVersion": "unknown",
        "testFramework": "unknown",
        "mockFramework": "unknown",
    }

    if not stack_profile:
        return _MISSING, True, "stack-profile missing or incomplete"

    modules = stack_profile.get("modules", [])
    if not modules:
        return _MISSING, True, "stack-profile missing or incomplete"

    mod = modules[0]
    test_info = mod.get("test", {})
    mock_info = mod.get("mock", {})

    # A module without explicit framework values is treated as incomplete.
    if not test_info.get("framework") or not mock_info.get("framework"):
        return _MISSING, True, "stack-profile missing or incomplete"

    assert_info = mod.get("assert", {})
    di_info = mod.get("di", {})

    stack: dict = {
        "javaVersion": stack_profile.get("java", "unknown"),
        "testFramework": test_info.get("framework", "unknown"),
        "mockFramework": mock_info.get("framework", "unknown"),
    }

    test_version = test_info.get("version", "")
    if test_version:
        stack["testVersion"] = test_version

    mock_version = mock_info.get("version", "")
    if mock_version:
        stack["mockVersion"] = mock_version

    assert_framework = assert_info.get("framework")
    stack["assertFramework"] = assert_framework if assert_framework else "none"

    spring = bool(di_info.get("spring", False))
    stack["springEnabled"] = spring
    if spring:
        stack["springBootVersion"] = di_info.get("springBoot")
        slices = di_info.get("slices", [])
        if slices:
            stack["springSlices"] = slices

    namespace = _detect_namespace(stack_profile)
    stack["namespaceStyle"] = namespace

    processors = mod.get("annotationProcessors", [])
    if processors:
        stack["annotationProcessors"] = processors

    return stack, False, None


def _detect_namespace(stack_profile: dict) -> str:
    processors = []
    for mod in stack_profile.get("modules", []):
        processors.extend(mod.get("annotationProcessors", []))
    joined = " ".join(processors).lower()
    if "jakarta" in joined:
        return "jakarta"
    if "javax" in joined:
        return "javax"
    return "none"


def extract_classification(classification_index: dict | None, fqcn: str) -> dict:
    if not classification_index:
        return {}
    for entry in classification_index.get("classes", []):
        if entry.get("fqcn") == fqcn:
            result: dict = {}
            # Include each atomic field only when the value is present (not None).
            # Exception: recommendedTemplate accepts null in the schema → always include.
            for key in (
                "type", "testabilityRisk", "coverageValue", "reasons",
                "tags", "loc", "publicMethods", "cyclomatic", "coverage",
                "risk", "score",
            ):
                val = entry.get(key)
                if val is not None:
                    result[key] = val
            result["recommendedTemplate"] = entry.get("recommendedTemplate")
            return result
    return {}


def extract_coverage(coverage_targets: dict | None, sut: str, batch_items: list[dict]) -> dict:
    """Build coverage block: aggregate totals + per-target detail for this SUT."""
    sut_target_ids = {item["targetId"] for item in batch_items if item["sut"] == sut}
    targets_out: list[dict] = []
    total_lines = 0
    total_branches = 0

    if coverage_targets:
        for t in coverage_targets.get("targets", []):
            if t.get("sut") == sut and t.get("id") in sut_target_ids:
                ml = t.get("missedLines", 0)
                mb = t.get("missedBranches", 0)
                total_lines += ml
                total_branches += mb
                targets_out.append({
                    "targetId": t["id"],
                    "method": t.get("method", ""),
                    "missedLines": ml,
                    "missedBranches": mb,
                    "branchId": t.get("branchId", None),
                    "score": t.get("score"),
                })

    return {
        "totalMissedLines": total_lines,
        "totalMissedBranches": total_branches,
        "targets": targets_out,
    }


def extract_symbol_contract(
    symbol_contracts_dir: Path,
    fqcn: str,
) -> tuple[list[dict], list[dict]]:
    """Return (constructors, methods) from per-FQCN contract file."""
    contract_path = symbol_contracts_dir / f"{safe_fqcn(fqcn)}.json"
    contract = load_optional(contract_path)
    if not contract:
        return [], []

    # normalize_params() coerces any legacy ["String", ...] input into
    # [{"type": "String"}, ...] so the downstream compact-pack builder
    # (which assumes dicts) and the context-pack schema (which requires
    # [{type, name?}]) both stay valid.
    constructors = [
        {
            "evidenceId": c["evidenceId"],
            "visibility": c.get("visibility", "public"),
            "params": normalize_params(c.get("params", [])),
            "throws": c.get("throws", []),
        }
        for c in contract.get("constructors", [])
    ]

    methods = [
        {
            "evidenceId": m["evidenceId"],
            "name": m["name"],
            "returnType": m.get("returnType", "void"),
            "params": normalize_params(m.get("params", [])),
            "throws": m.get("throws", []),
            "usable": bool(m.get("usable", True)),
        }
        for m in contract.get("methods", [])
        if m.get("usable", True)
    ]

    return constructors, methods


def extract_dependencies(dependency_graph: dict | None, fqcn: str) -> tuple[list, list, dict]:
    """Return (dependencies, collaboratorUsage, springStrategy) for this SUT."""
    if not dependency_graph:
        return [], [], {}

    for graph in dependency_graph.get("graphs", []):
        if graph.get("sut") == fqcn:
            deps = [
                {
                    "name": d["name"],
                    "type": d["type"],
                    "injection": d["injection"],
                    "final": d.get("final", False),
                }
                for d in graph.get("dependencies", [])
            ]
            collab = graph.get("collaboratorUsage", [])
            spring = graph.get("springStrategy", {})
            return deps, collab, spring

    return [], [], {}


def enrich_deps_with_strategy(
    dependencies: list[dict],
    fixture_catalog: dict | None,
) -> list[dict]:
    """Attach instantiationStrategy from fixture-catalog to each dependency."""
    if not fixture_catalog:
        return dependencies

    type_to_strategy: dict[str, str] = {
        f["type"]: f["strategy"]
        for f in fixture_catalog.get("fixtures", [])
    }

    enriched = []
    for dep in dependencies:
        d = dict(dep)
        d["instantiationStrategy"] = type_to_strategy.get(dep["type"], "mock")
        enriched.append(d)
    return enriched


def extract_fixtures(
    fixture_catalog: dict | None,
    dependencies: list[dict],
    batch_items: list[dict],
    sut: str,
) -> list[dict]:
    """Extract fixtures relevant to this SUT's dependencies and batch fixture IDs."""
    if not fixture_catalog:
        return []

    dep_types = {d["type"] for d in dependencies}
    batch_fixture_ids: set[str] = set()
    for item in batch_items:
        if item["sut"] == sut:
            batch_fixture_ids.update(item.get("fixtureIds", []))

    relevant: list[dict] = []
    for fix in fixture_catalog.get("fixtures", []):
        if fix["type"] in dep_types or fix["id"] in batch_fixture_ids:
            relevant.append({
                "id": fix["id"],
                "type": fix["type"],
                "strategy": fix["strategy"],
                "builderEvidence": fix.get("builderEvidence"),
                "constructorEvidence": fix.get("constructorEvidence"),
                "factoryEvidence": fix.get("factoryEvidence"),
                "values": fix.get("values", {}),
                "variants": fix.get("variants", []),
                "cycleSafe": fix.get("cycleSafe", True),
            })
    return relevant


def _framework_imports_from_stack(stack: dict) -> set[str]:
    """Map confirmed stack capabilities to allowed import FQCNs (minimum privilege).

    No framework package is included unless its corresponding stack flag is
    explicitly confirmed — 'unknown' or 'none' values contribute nothing.
    """
    imports: set[str] = set()

    test_fw = stack.get("testFramework", "unknown")
    mock_fw = stack.get("mockFramework", "unknown")
    assert_fw = stack.get("assertFramework", "unknown")
    spring = bool(stack.get("springEnabled", False))

    if test_fw == "junit5":
        imports.update({
            "org.junit.jupiter.api.Test",
            "org.junit.jupiter.api.BeforeEach",
            "org.junit.jupiter.api.AfterEach",
            "org.junit.jupiter.api.Assertions",
            "org.junit.jupiter.api.extension.ExtendWith",
        })
    elif test_fw == "junit4":
        imports.update({
            "org.junit.Test",
            "org.junit.Before",
            "org.junit.After",
        })

    if mock_fw == "mockito":
        imports.update({
            "org.mockito.Mockito",
            "org.mockito.Mock",
            "org.mockito.InjectMocks",
        })
        if test_fw == "junit5":
            imports.add("org.mockito.junit.jupiter.MockitoExtension")
        elif test_fw == "junit4":
            imports.add("org.mockito.junit.MockitoJUnitRunner")

    if assert_fw == "assertj":
        imports.add("org.assertj.core.api.Assertions")
    elif assert_fw == "hamcrest":
        imports.update({
            "org.hamcrest.MatcherAssert",
            "org.hamcrest.Matchers",
        })

    if spring:
        imports.update({
            "org.springframework.boot.test.context.SpringBootTest",
            "org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest",
            "org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest",
            "org.springframework.boot.test.mock.mockito.MockBean",
            "org.springframework.test.web.servlet.MockMvc",
            "org.springframework.beans.factory.annotation.Autowired",
        })

    return imports


# Dotted Java identifier (a fully-qualified class name) embedded anywhere in a
# type string — survives generics, arrays and varargs (e.g. "Map<String,
# com.x.Foo>[]" yields "com.x.Foo"). Simple names like "String" or primitives
# like "int" have no dot and are intentionally ignored: they need no import.
_FQCN_TOKEN = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+")


# Direct members of java.lang (java.lang.String, java.lang.Object, …) are
# auto-imported by every compilation unit; an explicit import is redundant and
# tripped by common checkstyle rules. Subpackages (java.lang.reflect.*) still
# require imports, so only single-segment java.lang members are filtered.
_JAVA_LANG_MEMBER = re.compile(r"^java\.lang\.[A-Za-z_$][A-Za-z0-9_$]*$")


def _fqcns_in(text: str) -> set[str]:
    """Extract every importable FQCN embedded in a type string.

    Auto-imported java.lang members are dropped (no import needed).
    """
    if not text:
        return set()
    return {t for t in _FQCN_TOKEN.findall(text) if not _JAVA_LANG_MEMBER.match(t)}


def _referenced_types(
    constructors: list[dict],
    methods: list[dict],
    dependencies: list[dict],
    collaborator_usage: list[dict],
) -> set[str]:
    """FQCNs the SUT's evidenced API surface forces a test to reference.

    Walks constructor/method signatures (params, returnType, throws), declared
    dependencies, and collaborator-usage methods. Types are stored as FQCNs in
    symbol-contracts and dependency-graph, so this is the exact set of non-local
    types a generated test may legitimately need to import.
    """
    refs: set[str] = set()

    def add_params(params: list) -> None:
        for p in params:
            refs.update(_fqcns_in(p.get("type", "") if isinstance(p, dict) else str(p)))

    for c in constructors:
        add_params(c.get("params", []))
        for t in c.get("throws", []):
            refs.update(_fqcns_in(t))

    for m in methods:
        refs.update(_fqcns_in(m.get("returnType", "")))
        add_params(m.get("params", []))
        for t in m.get("throws", []):
            refs.update(_fqcns_in(t))

    for d in dependencies:
        refs.update(_fqcns_in(d.get("type", "")))

    for cu in collaborator_usage:
        refs.update(_fqcns_in(cu.get("type", "")))
        for cm in cu.get("methods", []):
            refs.update(_fqcns_in(cm.get("returnType", "")))
            add_params(cm.get("params", []))
            for t in cm.get("throws", []):
                refs.update(_fqcns_in(t))

    return refs


def extract_allowed_imports(
    import_whitelist: dict | None,
    stack: dict,
    sut_fqcn: str,
    constructors: list[dict],
    methods: list[dict],
    dependencies: list[dict],
    collaborator_usage: list[dict],
    baseline_presets: list[str] | None = None,
) -> list[str]:
    """Per-SUT minimum-privilege import whitelist.

    The full transitive classpath is NEVER admitted wholesale. Admitted FQCNs are
    scoped to what *this* SUT can plausibly need in a test:
      - test/mock/assert/spring framework imports confirmed by the stack,
      - architecture baseline preset imports (stack_profile.presets.imports.allowed),
      - the project's own source classes (origin='source' in the whitelist),
      - the SUT itself,
      - every FQCN actually referenced by the SUT's evidenced signatures
        (constructors, methods, dependencies, collaborator usage) — both project
        collaborators and external dependency types.

    A dependency/JDK class enters only when the SUT's bytecode-derived evidence
    references it — so multi-release JAR noise (META-INF/versions/…) and the
    thousands of unrelated transitive classes never reach the pack. Referenced
    types are admitted directly: they come from symbol-contracts and the
    dependency-graph, so they are resolvable on the classpath by construction
    (the resolved-classpath whitelist holds only external deps, never the
    project's own classes).
    """
    allowed: set[str] = _framework_imports_from_stack(stack)

    # Architecture baseline exception rules.
    if baseline_presets:
        allowed.update(baseline_presets)

    # Project-local source classes are always importable (bounded by project size).
    if import_whitelist:
        for entry in import_whitelist.get("classes", []):
            if entry.get("origin") == "source" and entry.get("fqcn"):
                allowed.add(entry["fqcn"])

    # The SUT itself.
    if sut_fqcn:
        allowed.add(sut_fqcn)

    # Types the SUT's evidenced API surface forces a test to reference.
    allowed.update(_referenced_types(constructors, methods, dependencies, collaborator_usage))

    return sorted(allowed)


# ── Pack builder ──────────────────────────────────────────────────────────────

def build_pack(
    fqcn: str,
    mode: str,
    batch_items: list[dict],
    stack_profile: dict | None,
    classification_index: dict | None,
    dependency_graph: dict | None,
    fixture_catalog: dict | None,
    coverage_targets: dict | None,
    import_whitelist: dict | None,
    symbol_contracts_dir: Path,
    failure_memory: dict | None = None,
) -> dict:
    """Assemble the minimal context-pack for one SUT."""
    stack, blocked, block_reason = extract_stack(stack_profile)
    classification = extract_classification(classification_index, fqcn)
    coverage = extract_coverage(coverage_targets, fqcn, batch_items)
    constructors, methods = extract_symbol_contract(symbol_contracts_dir, fqcn)
    deps_raw, collab_usage, spring_strategy = extract_dependencies(dependency_graph, fqcn)
    dependencies = enrich_deps_with_strategy(deps_raw, fixture_catalog)
    fixtures = extract_fixtures(fixture_catalog, deps_raw, batch_items, fqcn)

    baseline_presets: list[str] | None = None
    if stack_profile:
        raw_presets = stack_profile.get("presets", {}).get("imports.allowed")
        if isinstance(raw_presets, list):
            baseline_presets = raw_presets or None

    allowed_imports = extract_allowed_imports(
        import_whitelist,
        stack,
        fqcn,
        constructors,
        methods,
        dependencies,
        collab_usage,
        baseline_presets,
    )

    pack: dict = {
        "schemaVersion": 1,
        "sut": fqcn,
        "mode": mode,
        "stack": stack,
        "coverage": coverage,
        "constructors": constructors,
        "methods": methods,
        "dependencies": dependencies,
        "collaboratorUsage": collab_usage,
        "fixtures": fixtures,
        "allowedImports": allowed_imports,
        "forbidden": FORBIDDEN_ACTIONS,
    }

    if blocked:
        pack["blocked"] = True
        pack["blockReason"] = block_reason

    if classification:
        pack["classification"] = classification

    if spring_strategy:
        pack["springStrategy"] = spring_strategy

    fm_rows = project_failure_memory(failure_memory, fqcn)
    if fm_rows:
        pack["failureMemory"] = fm_rows

    return pack


# ── Compact pack ──────────────────────────────────────────────────────────────

def _compact_stack(stack: dict) -> list:
    """Positional tuple: [java, testFw, mockFw, assertFw, springEnabled,
    namespaceStyle, testVersion?, mockVersion?, springBootVersion?]"""
    return [
        stack.get("javaVersion", "unknown"),
        stack.get("testFramework", "unknown"),
        stack.get("mockFramework", "unknown"),
        stack.get("assertFramework", "none"),
        bool(stack.get("springEnabled", False)),
        stack.get("namespaceStyle", "none"),
        stack.get("testVersion", ""),
        stack.get("mockVersion", ""),
        stack.get("springBootVersion") or "",
    ]


def _compact_coverage(coverage: dict) -> list[list]:
    rows: list[list] = []
    for t in coverage.get("targets", []):
        rows.append([
            t.get("targetId", ""),
            t.get("method", ""),
            t.get("missedLines", 0),
            t.get("missedBranches", 0),
            t.get("branchId"),
        ])
    return rows


def _compact_imports(allowed: list[str], max_imports: int) -> tuple[Any, bool]:
    """Return (compactImports, truncated). Prefix-compress when >=3 hits per prefix."""
    truncated = False
    items = list(allowed)
    if len(items) > max_imports:
        items = items[:max_imports]
        truncated = True

    buckets: dict[str, list[str]] = {}
    flat: list[str] = []
    for fqcn in items:
        if "." not in fqcn:
            flat.append(fqcn)
            continue
        prefix, leaf = fqcn.rsplit(".", 1)
        buckets.setdefault(prefix, []).append(leaf)

    qualifying = {p: leaves for p, leaves in buckets.items() if len(leaves) >= 3}
    if not qualifying:
        return items, truncated

    prefixes_sorted = sorted(qualifying.keys())
    leaves_obj: dict[str, list[str]] = {}
    for idx, prefix in enumerate(prefixes_sorted):
        leaves_obj[str(idx)] = sorted(qualifying[prefix])

    extras = list(flat)
    for prefix, leaves in buckets.items():
        if prefix in qualifying:
            continue
        for leaf in leaves:
            extras.append(f"{prefix}.{leaf}")
    if extras:
        leaves_obj["_"] = sorted(extras)

    return {"prefixes": prefixes_sorted, "leaves": leaves_obj}, truncated


def build_compact_pack(pack: dict, max_imports: int) -> tuple[dict, bool]:
    """Project the legible pack into compact shape. Returns (compact, importsTruncated)."""
    # Build evidence id pool ordered by first appearance (constructors first, then methods).
    eid_pool: list[str] = []
    eid_index: dict[str, int] = {}

    def _eid_idx(eid: str) -> int:
        if eid not in eid_index:
            eid_index[eid] = len(eid_pool)
            eid_pool.append(eid)
        return eid_index[eid]

    ctor_rows: list[list] = []
    for c in pack.get("constructors", []):
        # Belt-and-suspenders: extract_symbol_contract already normalises, but
        # packs built outside this module may still carry string params.
        params = [
            [p.get("type", ""), p.get("name")] if p.get("name") else [p.get("type", "")]
            for p in normalize_params(c.get("params", []))
        ]
        ctor_rows.append([_eid_idx(c["evidenceId"]), params])

    meth_rows: list[list] = []
    for m in pack.get("methods", []):
        if m.get("usable") is False:
            continue
        args = [p.get("type", "") for p in normalize_params(m.get("params", []))]
        meth_rows.append([
            _eid_idx(m["evidenceId"]),
            m.get("name", ""),
            m.get("returnType", "void"),
            args,
        ])

    dep_rows: list[list] = []
    for d in pack.get("dependencies", []):
        dep_rows.append([
            d.get("name", ""),
            d.get("type", ""),
            d.get("injection", ""),
            d.get("instantiationStrategy", "mock"),
        ])

    fx_rows: list[list] = []
    for f in pack.get("fixtures", []):
        fx_rows.append([
            f.get("id", ""),
            f.get("type", ""),
            f.get("strategy", "mock"),
        ])

    imp, truncated = _compact_imports(pack.get("allowedImports", []), max_imports)

    compact: dict = {
        "v": 1,
        "sut": pack["sut"],
        "m": pack.get("mode", "coverage"),
        "stk": _compact_stack(pack.get("stack", {})),
        "cov": _compact_coverage(pack.get("coverage", {})),
        "ctor": ctor_rows,
        "meth": meth_rows,
        "deps": dep_rows,
        "fx": fx_rows,
        "imp": imp,
        "eid": eid_pool,
    }

    if pack.get("blocked"):
        compact["blk"] = True
        compact["br"] = pack.get("blockReason")

    cls = pack.get("classification") or {}
    cls_filtered = {k: v for k, v in cls.items() if k not in _COMPACT_CLASSIFICATION_DROP}
    if cls_filtered:
        bucket, subtype = _classification_bucket(cls_filtered.get("type"))
        compact["cls"] = [bucket, subtype] if subtype else [bucket]

    spring = pack.get("springStrategy") or {}
    if spring:
        compact["spr"] = [spring.get("slice", "none"), spring.get("mockBeans", [])]

    if truncated:
        compact["tr"] = ["imp"]

    fm_rows = pack.get("failureMemory") or []
    if fm_rows:
        # Positional row: [hash, errorCode, symbolFQN, fixId, attempts, lastResult, lastSeenCycle?]
        compact_fm: list[list] = []
        for row in fm_rows:
            tup = [
                row.get("hash", ""),
                row.get("errorCode", ""),
                row.get("symbolFQN", ""),
                row.get("fixId", ""),
                int(row.get("attempts") or 0),
                row.get("lastResult", "FAILED"),
            ]
            lsc = row.get("lastSeenCycle")
            if isinstance(lsc, int):
                tup.append(lsc)
            compact_fm.append(tup)
        compact["fm"] = compact_fm

    return compact, truncated


def _atomic_write_minified(path: Path, data: dict) -> int:
    """Write minified JSON atomically. Returns byte length written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = payload.encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(encoded)
    import os
    os.replace(tmp, path)
    return len(encoded)


def _emit_budget(
    state_dir: Path,
    sut: str,
    context_pack_bytes: int,
    compact_pack_bytes: int,
    truncated_fields: list[str],
) -> None:
    """Accumulate a per-SUT budget entry in state/_summaries/llm-budget.json (P1.c).

    The file is rewritten atomically with the merged entries[] list each call.
    Older entries for the same `sut` are replaced so a re-run does not duplicate.
    A run only resets the file when the first SUT of the run is written (the
    caller passes `truncated_fields=[]` for that bootstrap call via the
    pack-builder loop), keeping per-SUT history within a single Phase 0 run.
    """
    budget_path = state_dir / "_summaries" / "llm-budget.json"
    schema_version = 2
    if budget_path.exists():
        try:
            current = load_json(budget_path)
        except Exception:
            current = {}
    else:
        current = {}

    entries: list[dict] = (
        current.get("entries", [])
        if isinstance(current.get("entries"), list)
        else []
    )
    entries = [e for e in entries if e.get("sut") != sut]

    est_tokens = int(compact_pack_bytes * TOKENS_PER_BYTE)
    entry = {
        "sut": sut,
        "contextPackBytes": context_pack_bytes,
        "compactPackBytes": compact_pack_bytes,
        "estimatedTokensIn": est_tokens,
        "maxTokensIn": MAX_TOKENS_IN,
        "overBudget": est_tokens > MAX_TOKENS_IN,
        "truncatedFields": truncated_fields,
    }
    entries.append(entry)

    if entry["overBudget"]:
        print(
            f"[WARN] llm-budget: {sut} compact pack ~{est_tokens} tokens "
            f"exceeds MAX_TOKENS_IN={MAX_TOKENS_IN}; shrink the pack or split the SUT.",
            file=sys.stderr,
        )

    total_compact = sum(e.get("compactPackBytes", 0) for e in entries)
    total_tokens = sum(e.get("estimatedTokensIn", 0) for e in entries)
    over_budget_count = sum(1 for e in entries if e.get("overBudget"))

    payload = {
        "schemaVersion": schema_version,
        "tokensPerByte": TOKENS_PER_BYTE,
        "totals": {
            "suts": len(entries),
            "compactPackBytes": total_compact,
            "estimatedTokensIn": total_tokens,
            "maxTokensIn": MAX_TOKENS_IN,
            "overBudgetCount": over_budget_count,
        },
        "entries": entries,
    }
    atomic_write_json(budget_path, payload)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Build per-SUT context packs from the JSON state layer.\n"
            "Output: state/context-packs/<safe_fqcn>.json\n"
            "These packs are the ONLY JSON the LLM agents may read."
        )
    )
    ap.add_argument(
        "--out",
        required=True,
        help="State directory (contains batch-plan.json and other state files)",
    )
    ap.add_argument(
        "--sut",
        default=None,
        help="Build pack for a single FQCN only (default: all SUTs in batch-plan.json)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print each pack to stdout instead of writing files",
    )
    ap.add_argument(
        "--compact",
        action="store_true",
        help="In addition to the legible pack, emit a minified compact pack to "
             "state/context-packs-compact/<safe_fqcn>.json (token-optimised).",
    )
    ap.add_argument(
        "--max-imports",
        type=int,
        default=DEFAULT_MAX_IMPORTS,
        help=f"Maximum number of allowedImports kept in the compact pack "
             f"(default: {DEFAULT_MAX_IMPORTS}). Truncation is reported via "
             f"state/_summaries/llm-budget.json.",
    )
    args = ap.parse_args()

    state_dir = Path(args.out).resolve()
    packs_dir = state_dir / "context-packs"
    compact_dir = state_dir / "context-packs-compact"
    contracts_dir = state_dir / "symbol-contracts"

    # ── Load batch plan (required) ────────────────────────────────────────────
    batch_plan_path = state_dir / "batch-plan.json"
    if not batch_plan_path.exists():
        fail(f"batch-plan.json not found in {state_dir} — run coverage_planner.py first")
    batch_plan = load_json(batch_plan_path)
    mode: str = batch_plan.get("mode", "coverage")
    batch_items: list[dict] = batch_plan.get("items", [])

    # ── Collect unique SUTs ───────────────────────────────────────────────────
    if args.sut:
        suts = [args.sut]
    else:
        seen: dict[str, bool] = {}
        suts = [
            seen.setdefault(item["sut"], item["sut"])  # type: ignore[func-returns-value]
            for item in batch_items
            if item["sut"] not in seen
        ]
        suts = list(seen.keys())

    if not suts:
        print("[INFO] No SUTs found in batch-plan.json — nothing to build.", file=sys.stderr)
        return 0

    # ── Load shared state files (optional — warn but don't fail) ─────────────
    stack_profile = load_optional(state_dir / "stack-profile.json")
    classification_index = load_optional(state_dir / "classification-index.json")
    dependency_graph = load_optional(state_dir / "dependency-graph.json")
    fixture_catalog = load_optional(state_dir / "fixture-catalog.json")
    coverage_targets = load_optional(state_dir / "coverage-targets.json")
    import_whitelist = load_optional(state_dir / "import-whitelist.json")
    failure_memory = load_optional(state_dir / "failure-memory.json")

    if not stack_profile:
        print("[WARN] stack-profile.json missing — context packs will be marked blocked", file=sys.stderr)

    # ── Build and write one pack per SUT ─────────────────────────────────────
    errors = 0
    packs_dir.mkdir(parents=True, exist_ok=True)

    # P1.c: reset llm-budget.json at the start of a full-run (no --sut filter)
    # so per-SUT entries reflect the current run only. When --sut is supplied
    # we keep prior entries and replace just that SUT's row.
    if args.compact and not args.dry_run and not args.sut:
        budget_path = state_dir / "_summaries" / "llm-budget.json"
        if budget_path.exists():
            try:
                budget_path.unlink()
            except OSError:
                pass

    for fqcn in suts:
        try:
            pack = build_pack(
                fqcn=fqcn,
                mode=mode,
                batch_items=batch_items,
                stack_profile=stack_profile,
                classification_index=classification_index,
                dependency_graph=dependency_graph,
                fixture_catalog=fixture_catalog,
                coverage_targets=coverage_targets,
                import_whitelist=import_whitelist,
                symbol_contracts_dir=contracts_dir,
                failure_memory=failure_memory,
            )
        except Exception as exc:
            print(f"[ERROR] Building pack for {fqcn}: {exc}", file=sys.stderr)
            errors += 1
            continue

        try:
            validate(SCHEMA_NAME, pack)
        except Exception as exc:
            print(f"[WARN] Schema validation failed for {fqcn}: {exc}", file=sys.stderr)

        if args.dry_run:
            print(f"\n=== context-pack: {fqcn} ===")
            print(json.dumps(pack, ensure_ascii=False, indent=2))
            if args.compact:
                compact, truncated = build_compact_pack(pack, args.max_imports)
                print(f"\n=== context-pack-compact: {fqcn} ===")
                print(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))
                if truncated:
                    print(f"[INFO] imports truncated to {args.max_imports} for {fqcn}", file=sys.stderr)
        else:
            out_path = packs_dir / f"{safe_fqcn(fqcn)}.json"
            atomic_write_json(out_path, pack)
            print(f"[OK] {fqcn} → {out_path.relative_to(state_dir.parent)}")

            if args.compact:
                compact, truncated = build_compact_pack(pack, args.max_imports)
                compact_path = compact_dir / f"{safe_fqcn(fqcn)}.json"
                compact_bytes = _atomic_write_minified(compact_path, compact)
                print(f"[OK] compact {fqcn} → {compact_path.relative_to(state_dir.parent)}")

                # P1.c: emit budget unconditionally so totals are auditable.
                try:
                    context_pack_bytes = out_path.stat().st_size
                except OSError:
                    context_pack_bytes = 0
                _emit_budget(
                    state_dir=state_dir,
                    sut=fqcn,
                    context_pack_bytes=context_pack_bytes,
                    compact_pack_bytes=compact_bytes,
                    truncated_fields=["imp"] if truncated else [],
                )

    if errors:
        print(f"\n[FAIL] {errors} pack(s) failed to build.", file=sys.stderr)
        return 1

    print(f"\n[DONE] {len(suts)} context pack(s) written to {packs_dir}")
    return 0


if __name__ == "__main__":
    with _TimedRun("context_pack_builder") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
