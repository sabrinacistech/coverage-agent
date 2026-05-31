"""classification_analyzer.py — deterministic SUT classification without LLM.

Reads (produced by prior pipeline steps):
  - state/index/classes.json       (FQCN → kind, modifiers, annotations, parents)
  - state/index/annotations.json   (FQCN → [annotation simple names])
  - state/index/methods.json       (FQCN → [{name, params, returnType, ...}])
  - state/coverage-targets.json    (coverage per FQCN, optional)
  - state/generated-code-index.json (excluded FQCNs / packages)
  - state/stack-profile.json       (test framework → recommended template prefix)

Falls back to reading state/symbol-contracts/<fqcn>.json directly if the index
is absent or empty (e.g. first run before semantic_index_writer has populated it).

Classification rules (applied in priority order)
-------------------------------------------------
  1. FQCN in excludedFqcns / excludedPackages → "generated/excluded"
  2. Annotation @RestController | @Controller | @ControllerAdvice → "controller"
  3. Annotation @Service → "service"
  4. Annotation @Repository → "repository"
  5. Annotation @Component → "component"
  6. Annotation @Configuration | @SpringBootApplication → "configuration"
  7. Annotation @Mapper → "mapper"
  8. kind=interface OR modifier=abstract → "non-instantiable"
  9. kind=record → "data-carrier"
 10. kind=enum → "enum"
 11. Name heuristics (Controller, Service, Repository, Mapper, Config, …)
 12. Default → "component"

Heuristic metrics (all deterministic, no LLM)
----------------------------------------------
  - testabilityRisk: low | medium | high
  - coverageValue:   low | medium | high
  - recommendedTemplate: path inside templates/ (or null)
  - reasons: human-readable strings explaining each decision

Schema update
-------------
  Retrocompatibly extends state/_schemas/classification-index.schema.json to include
  the full type enum and the new metric fields.

CLI:
    python tools/python/classification_analyzer.py --out state
    python tools/python/classification_analyzer.py --out state --contracts state/symbol-contracts
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from common import SCHEMAS_DIR, atomic_write_json, load_json, validate

# ─────────────────────────────────────────────────────────────────────────────
# Classification type constants
# ─────────────────────────────────────────────────────────────────────────────

T_CONTROLLER       = "controller"
T_SERVICE          = "service"
T_REPOSITORY       = "repository"
T_COMPONENT        = "component"
T_CONFIGURATION    = "configuration"
T_MAPPER           = "mapper"
T_NON_INSTANTIABLE = "non-instantiable"
T_DATA_CARRIER     = "data-carrier"
T_ENUM             = "enum"
T_GENERATED        = "generated/excluded"

ALL_TYPES: list[str] = [
    T_CONTROLLER, T_SERVICE, T_REPOSITORY, T_COMPONENT,
    T_CONFIGURATION, T_MAPPER, T_NON_INSTANTIABLE,
    T_DATA_CARRIER, T_ENUM, T_GENERATED,
    # legacy types from original schema — kept for backward compat
    "util", "config", "dto", "generated", "entity", "exception",
]

# ─────────────────────────────────────────────────────────────────────────────
# Annotation → type mapping (simple name, case-insensitive prefix match)
# ─────────────────────────────────────────────────────────────────────────────

# Maps simple annotation name (lower-case) → classification type
_ANN_MAP: list[tuple[str, str]] = [
    # Controllers — checked before @Component to avoid false positives
    ("restcontroller",          T_CONTROLLER),
    ("controller",              T_CONTROLLER),
    ("controlleradvice",        T_CONTROLLER),
    ("restcontrolleradvice",    T_CONTROLLER),
    ("feignclient",             T_CONTROLLER),
    # Services
    ("service",                 T_SERVICE),
    # Repositories
    ("repository",              T_REPOSITORY),
    ("repositoryrestresource",  T_REPOSITORY),
    # Configuration
    ("configuration",           T_CONFIGURATION),
    ("springbootapplication",   T_CONFIGURATION),
    ("enablewebmvc",            T_CONFIGURATION),
    ("enablewebflux",           T_CONFIGURATION),
    # Mappers
    ("mapper",                  T_MAPPER),
    ("mapperconfig",            T_MAPPER),
    # Generic component — last annotation rule, lowest priority
    ("component",               T_COMPONENT),
]

# ─────────────────────────────────────────────────────────────────────────────
# Name-suffix heuristics (applied when no annotation matched)
# ─────────────────────────────────────────────────────────────────────────────

_SUFFIX_MAP: list[tuple[tuple[str, ...], str]] = [
    (("controller", "resource", "endpoint", "rest"),                T_CONTROLLER),
    (("service", "serviceimpl", "servicefacade"),                    T_SERVICE),
    (("repository", "repositoryimpl", "repo", "dao", "daoimpl"),    T_REPOSITORY),
    (("mapper", "mapperimpl", "converter", "converterimpl"),        T_MAPPER),
    (("config", "configuration", "settings", "properties"),        T_CONFIGURATION),
    (("exception", "error", "fault"),                               "exception"),
    (("dto", "request", "response", "vo", "payload", "command"),    T_DATA_CARRIER),
    (("util", "utils", "helper", "helpers", "factory", "builder"),  "util"),
    (("entity", "model", "domain"),                                 "entity"),
]

# ─────────────────────────────────────────────────────────────────────────────
# Risk / value / template tables
# ─────────────────────────────────────────────────────────────────────────────

_TESTABILITY_RISK: dict[str, str] = {
    T_GENERATED:        "high",   # not testable directly
    T_NON_INSTANTIABLE: "high",   # can't instantiate, needs mock or subclass
    T_CONFIGURATION:    "high",   # requires Spring context
    T_CONTROLLER:       "medium", # needs MockMvc / WebTestClient
    T_COMPONENT:        "low",
    T_SERVICE:          "low",
    T_REPOSITORY:       "low",
    T_MAPPER:           "low",
    T_DATA_CARRIER:     "low",
    T_ENUM:             "low",
    "entity":           "low",
    "exception":        "low",
    "util":             "low",
    "config":           "high",
    "dto":              "low",
    "generated":        "high",
}

_COVERAGE_VALUE: dict[str, str] = {
    T_GENERATED:        "low",
    T_NON_INSTANTIABLE: "low",
    T_CONFIGURATION:    "low",
    T_CONTROLLER:       "high",
    T_SERVICE:          "high",
    T_REPOSITORY:       "medium",
    T_COMPONENT:        "medium",
    T_MAPPER:           "medium",
    T_DATA_CARRIER:     "low",
    T_ENUM:             "low",
    "entity":           "low",
    "exception":        "low",
    "util":             "medium",
    "config":           "low",
    "dto":              "low",
    "generated":        "low",
}

_TEMPLATE: dict[str, str | None] = {
    T_CONTROLLER:       "templates/webmvc-test.java",
    T_SERVICE:          "templates/junit5-mockito.java",
    T_REPOSITORY:       "templates/junit5-mockito.java",
    T_COMPONENT:        "templates/junit5-mockito.java",
    T_MAPPER:           "templates/junit5-mockito.java",
    T_DATA_CARRIER:     "templates/junit5-mockito.java",
    T_ENUM:             "templates/junit5-mockito.java",
    "util":             "templates/junit5-mockito.java",
    "entity":           "templates/junit5-mockito.java",
    "exception":        "templates/junit5-mockito.java",
    "dto":              "templates/junit5-mockito.java",
    T_NON_INSTANTIABLE: None,
    T_CONFIGURATION:    None,
    T_GENERATED:        None,
    "generated":        None,
    "config":           None,
}

# ─────────────────────────────────────────────────────────────────────────────
# Schema update
# ─────────────────────────────────────────────────────────────────────────────

_FULL_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "classification-index.schema.json",
    "type": "object",
    "required": ["schemaVersion", "classes"],
    "properties": {
        "schemaVersion": {"const": 1},
        "generatedAt": {"type": "string"},
        "classes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["fqcn", "type"],
                "properties": {
                    "fqcn":                {"type": "string"},
                    "module":              {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ALL_TYPES,
                    },
                    "tags":                {"type": "array", "items": {"type": "string"}},
                    "testabilityRisk":     {"type": "string", "enum": ["low", "medium", "high"]},
                    "coverageValue":       {"type": "string", "enum": ["low", "medium", "high"]},
                    "recommendedTemplate": {"type": ["string", "null"]},
                    "reasons":             {"type": "array", "items": {"type": "string"}},
                    # legacy numeric fields — kept for backward compat
                    "loc":         {"type": "integer"},
                    "publicMethods": {"type": "integer"},
                    "cyclomatic":  {"type": "integer"},
                    "coverage": {
                        "type": "object",
                        "properties": {
                            "lines":    {"type": "number"},
                            "branches": {"type": "number"},
                        },
                    },
                    "risk":  {"type": "number"},
                    "score": {"type": "number"},
                },
            },
        },
    },
}


def _ensure_schema() -> None:
    """Write or retrocompatibly update classification-index.schema.json."""
    schema_path = SCHEMAS_DIR / "classification-index.schema.json"
    if not schema_path.exists():
        atomic_write_json(schema_path, _FULL_SCHEMA)
        print(f"[INFO] created schema: {schema_path.name}")
        return

    try:
        existing = load_json(schema_path)
    except Exception as exc:
        print(f"[WARN] cannot read existing schema, overwriting: {exc}", file=sys.stderr)
        atomic_write_json(schema_path, _FULL_SCHEMA)
        return

    changed = False
    item_props: dict = (
        existing
        .get("properties", {})
        .get("classes", {})
        .get("items", {})
        .get("properties", {})
    )

    # Extend type enum to include all new types
    type_node = item_props.get("type", {})
    existing_enum: list = type_node.get("enum", [])
    missing_types = [t for t in ALL_TYPES if t not in existing_enum]
    if missing_types:
        type_node["enum"] = existing_enum + missing_types
        item_props["type"] = type_node
        changed = True

    # Add new metric fields if absent
    for field, definition in [
        ("testabilityRisk",     {"type": "string", "enum": ["low", "medium", "high"]}),
        ("coverageValue",       {"type": "string", "enum": ["low", "medium", "high"]}),
        ("recommendedTemplate", {"type": ["string", "null"]}),
        ("reasons",             {"type": "array", "items": {"type": "string"}}),
        ("module",              {"type": "string"}),
        ("generatedAt",         {"type": "string"}),
    ]:
        if field not in item_props:
            item_props[field] = definition
            changed = True

    if changed:
        # Remove legacy required field "score" if classification no longer sets it
        item_required: list = (
            existing
            .get("properties", {})
            .get("classes", {})
            .get("items", {})
            .get("required", [])
        )
        if "score" in item_required:
            item_required.remove("score")
            changed = True

        (existing
         .setdefault("properties", {})
         .setdefault("classes", {})
         .setdefault("items", {})
         )["properties"] = item_props

        atomic_write_json(schema_path, existing)
        print(f"[INFO] updated schema: {schema_path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# Annotation helper
# ─────────────────────────────────────────────────────────────────────────────

def _simple_name(annotation: str) -> str:
    """'org.springframework.stereotype.Service' → 'service'."""
    return annotation.rsplit(".", 1)[-1].lstrip("@").lower()


def _classify_by_annotations(annotations: list[str]) -> tuple[str | None, str | None]:
    """Return (type, reason) or (None, None) if no annotation matched."""
    for ann in annotations:
        simple = _simple_name(ann)
        for key, cls_type in _ANN_MAP:
            if simple == key:
                return cls_type, f"@{ann.rsplit('.',1)[-1]} annotation detected"
    return None, None


def _classify_by_name(fqcn: str) -> tuple[str, str]:
    """Heuristic: match class simple name against known suffixes."""
    simple = fqcn.rsplit(".", 1)[-1].lower()
    for suffixes, cls_type in _SUFFIX_MAP:
        for suffix in suffixes:
            if simple.endswith(suffix):
                return cls_type, f"class name ends with '{simple[-len(suffix):]}'  (heuristic)"
    return T_COMPONENT, "no annotation or name heuristic matched; defaulting to component"


# ─────────────────────────────────────────────────────────────────────────────
# Exclusion helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_exclusion_matchers(
    gen_index: dict,
) -> tuple[frozenset[str], list[re.Pattern]]:
    excluded_fqcns: frozenset[str] = frozenset(gen_index.get("excludedFqcns", []))
    excluded_pkg_patterns: list[re.Pattern] = []
    for pkg in gen_index.get("excludedPackages", []):
        try:
            excluded_pkg_patterns.append(re.compile(re.escape(pkg).replace(r"\*", ".*")))
        except re.error:
            pass
    for blocked in gen_index.get("blocked", []):
        pkg = blocked if isinstance(blocked, str) else blocked.get("package", "")
        if pkg:
            try:
                excluded_pkg_patterns.append(re.compile(re.escape(pkg).replace(r"\*", ".*")))
            except re.error:
                pass
    return excluded_fqcns, excluded_pkg_patterns


def _is_excluded(
    fqcn: str,
    excluded_fqcns: frozenset[str],
    excluded_pkg_patterns: list[re.Pattern],
) -> bool:
    if fqcn in excluded_fqcns:
        return True
    for pat in excluded_pkg_patterns:
        if pat.match(fqcn):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Coverage helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_coverage_map(cov_targets: dict) -> dict[str, dict]:
    """Return {fqcn: {lines: float, branches: float}} from coverage-targets.json."""
    cov: dict[str, dict] = {}
    for entry in cov_targets.get("targets", []):
        fqcn = entry.get("fqcn") or entry.get("class")
        if not fqcn:
            continue
        cov[fqcn] = {
            "lines": entry.get("lineCoverage") or entry.get("lines"),
            "branches": entry.get("branchCoverage") or entry.get("branches"),
        }
    return cov


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def _safe_load(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return load_json(path)
    except Exception as exc:
        print(f"[WARN] cannot load {path}: {exc}", file=sys.stderr)
        return default


def _collect_fqcns(
    index_dir: Path,
    contracts_dir: Path,
) -> dict[str, dict]:
    """
    Return {fqcn: {kind, modifiers, annotations}} from index or contracts fallback.

    Index structure: state/index/classes.json →
        {version, generatedAt, count, classes: {fqcn: {kind, modifiers, annotations, ...}}}

    Contract structure: state/symbol-contracts/<fqcn>.json →
        {fqcn, kind, modifiers, annotations, ...}
    """
    classes_index = _safe_load(index_dir / "classes.json", {})
    classes_raw: dict = classes_index.get("classes", {})

    # Also try annotation index
    ann_index = _safe_load(index_dir / "annotations.json", {})
    ann_by_fqcn: dict[str, list[str]] = ann_index.get("classes", {})

    result: dict[str, dict] = {}

    if classes_raw:
        for fqcn, meta in classes_raw.items():
            # Merge annotation index into class meta
            annotations = list(meta.get("annotations", []))
            for ann in ann_by_fqcn.get(fqcn, []):
                if ann not in annotations:
                    annotations.append(ann)
            result[fqcn] = {
                "kind": meta.get("kind", "class"),
                "modifiers": meta.get("modifiers", []),
                "annotations": annotations,
            }
    elif contracts_dir.exists():
        # Fallback: read symbol contracts directly
        for cp in sorted(contracts_dir.glob("*.json")):
            try:
                c = load_json(cp)
            except Exception:
                continue
            fqcn = c.get("fqcn")
            if not fqcn:
                continue
            result[fqcn] = {
                "kind": c.get("kind", "class"),
                "modifiers": c.get("modifiers", []),
                "annotations": c.get("annotations", []),
            }

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Classification core
# ─────────────────────────────────────────────────────────────────────────────

def _classify_one(
    fqcn: str,
    meta: dict,
    excluded_fqcns: frozenset[str],
    excluded_pkg_patterns: list[re.Pattern],
    coverage_map: dict[str, dict],
) -> dict:
    reasons: list[str] = []
    cls_type: str | None = None

    # Rule 1 — generated/excluded
    if _is_excluded(fqcn, excluded_fqcns, excluded_pkg_patterns):
        cls_type = T_GENERATED
        reasons.append("FQCN or package is in generated-code-index.json exclusions")

    # Rules 2-7 — annotation-based
    if cls_type is None:
        t, r = _classify_by_annotations(meta.get("annotations", []))
        if t:
            cls_type = t
            reasons.append(r)  # type: ignore[arg-type]

    # Rule 8 — kind / modifiers
    if cls_type is None:
        kind = meta.get("kind", "class").lower()
        modifiers = [m.lower() for m in meta.get("modifiers", [])]
        if kind == "interface":
            cls_type = T_NON_INSTANTIABLE
            reasons.append("kind=interface")
        elif "abstract" in modifiers:
            cls_type = T_NON_INSTANTIABLE
            reasons.append("modifier=abstract")
        elif kind == "record":
            cls_type = T_DATA_CARRIER
            reasons.append("kind=record")
        elif kind == "enum":
            cls_type = T_ENUM
            reasons.append("kind=enum")

    # Rules 11-12 — name heuristics
    if cls_type is None:
        cls_type, r = _classify_by_name(fqcn)
        reasons.append(r)

    assert cls_type is not None

    # ── Metrics ───────────────────────────────────────────────────────────────
    testability_risk = _TESTABILITY_RISK.get(cls_type, "medium")
    coverage_value   = _COVERAGE_VALUE.get(cls_type, "medium")
    template         = _TEMPLATE.get(cls_type)

    entry: dict = {
        "fqcn": fqcn,
        "type": cls_type,
        "testabilityRisk": testability_risk,
        "coverageValue": coverage_value,
        "recommendedTemplate": template,
        "reasons": reasons,
    }

    # Coverage data if available
    if fqcn in coverage_map:
        entry["coverage"] = coverage_map[fqcn]

    return entry


# ─────────────────────────────────────────────────────────────────────────────
# Main logic
# ─────────────────────────────────────────────────────────────────────────────

def analyse(state_dir: Path, contracts_dir: Path | None = None) -> dict:
    """Classify all known FQCNs and return the full classification-index dict."""
    from datetime import datetime, timezone

    index_dir = state_dir / "index"
    _contracts_dir = contracts_dir or (state_dir / "symbol-contracts")

    # Load inputs
    gen_index   = _safe_load(state_dir / "generated-code-index.json", {})
    cov_targets = _safe_load(state_dir / "coverage-targets.json", {})

    excluded_fqcns, excluded_pkg_patterns = _build_exclusion_matchers(gen_index)
    coverage_map = _build_coverage_map(cov_targets)

    fqcns = _collect_fqcns(index_dir, _contracts_dir)

    classes: list[dict] = []
    skipped = 0
    for fqcn, meta in sorted(fqcns.items()):
        # Skip test classes — they are not SUTs
        simple = fqcn.rsplit(".", 1)[-1]
        if simple.endswith(("Test", "Tests", "IT", "Spec", "Specs")):
            skipped += 1
            continue
        classes.append(
            _classify_one(fqcn, meta, excluded_fqcns, excluded_pkg_patterns, coverage_map)
        )

    if skipped:
        print(f"[INFO] skipped {skipped} test class(es) (Test/IT/Spec suffix)")

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "classes": classes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Classify Java classes deterministically (no LLM) from state/index/ data.\n\n"
            "Reads state/index/classes.json, annotations.json (from semantic_index_writer),\n"
            "plus state/coverage-targets.json and state/generated-code-index.json.\n"
            "Falls back to state/symbol-contracts/*.json if the index is empty.\n\n"
            "Classification rules (in priority order):\n"
            "  generated/excluded  — in generated-code-index exclusions\n"
            "  controller          — @RestController / @Controller\n"
            "  service             — @Service\n"
            "  repository          — @Repository\n"
            "  component           — @Component\n"
            "  configuration       — @Configuration / @SpringBootApplication\n"
            "  mapper              — @Mapper\n"
            "  non-instantiable    — interface or abstract class\n"
            "  data-carrier        — record\n"
            "  enum                — enum\n"
            "  <type>              — name-suffix heuristic\n"
            "  component           — default\n\n"
            "Writes state/classification-index.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out", required=True, help="State directory (e.g. state/)")
    ap.add_argument(
        "--contracts",
        default=None,
        help="Path to symbol-contracts directory (default: <out>/symbol-contracts)",
    )
    args = ap.parse_args()

    state_dir = Path(args.out).resolve()
    contracts_dir = Path(args.contracts).resolve() if args.contracts else None

    # Ensure / update schema before generating data
    _ensure_schema()

    result = analyse(state_dir, contracts_dir)
    validate("classification-index", result)
    atomic_write_json(state_dir / "classification-index.json", result)

    n = len(result["classes"])
    by_type: dict[str, int] = {}
    for c in result["classes"]:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    summary = "  ".join(f"{t}={cnt}" for t, cnt in sorted(by_type.items()))
    print(f"[OK] state/classification-index.json  {n} class(es)  {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
