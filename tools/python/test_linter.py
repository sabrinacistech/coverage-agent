"""test_linter.py — deterministic guardrails for generated Java tests.

Checks implemented:
* G1: every import/import static must be present in import-whitelist.json.
* G2-lite: `new X(...)` is rejected when X is an interface/abstract type or the
  contract says instantiation is not constructor-based.
* G2-lite: builder setters/method calls on variables with known contract types are
  rejected when the method is not enumerated in the contract.
* FreeBuilder guard: `new Interface()` and direct `Type_Builder` usage are blocked.
* G5 (--stack-profile): JUnit/Mockito/Spring version compatibility enforced.
* G6-quality (--quality-checks): enforces the 14 rules in test-quality-gate.md,
  derived from skills/11-quality/ (AAA structure, naming, anti-patterns,
  non-determinism, over-mocking, assert-free, eager tests, etc.).
* Index supplement (--index): state/index/methods.json used as G2 fallback.
* Context-pack cross-validation (--context-pack): SUT FQCN consistency check.

This is not a full Java compiler. It is a cheap pre-build gate designed to stop the
most common Copilot/LLM hallucinations before Maven/Gradle is invoked.

Usage:
  python test_linter.py \\
    --test-file  src/test/java/com/acme/FooServiceTest.java \\
    --whitelist  state/import-whitelist.json \\
    --contracts  state/symbol-contracts/ \\
    --stack-profile state/stack-profile.json \\
    --index      state/index \\
    --context-pack state/context-packs/com.acme.FooService.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from common import load_json

# ── Base regexes ──────────────────────────────────────────────────────────────
IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w\.]+(?:\.\*)?)\s*;", re.MULTILINE)
PACKAGE_RE = re.compile(r"^\s*package\s+([\w\.]+)\s*;", re.MULTILINE)
NEW_RE = re.compile(r"\bnew\s+([A-Z]\w*(?:\.[A-Z]\w*)?)\s*\(")
DIRECT_GENERATED_BUILDER_RE = re.compile(r"\bnew\s+([A-Z]\w+_Builder)\s*\(")
VAR_DECL_RE = re.compile(
    r"\b(?P<type>[A-Z]\w*(?:\.[A-Z]\w+)?(?:<[^;=]+>)?)\s+(?P<var>[a-zA-Z_]\w*)\s*(?:=|;)"
)
CALL_RE = re.compile(r"\b(?P<var>[a-zA-Z_]\w*)\.(?P<method>[a-zA-Z_]\w*)\s*\(")
STATIC_CALL_RE = re.compile(r"\b(?P<type>[A-Z]\w*)\.(?P<method>[a-zA-Z_]\w*)\s*\(")
MOCK_STATIC_RE = re.compile(r"\bmockStatic\s*\(")

# ── G6-quality regexes (skills/11-quality/) ───────────────────────────────────
# Method names annotated with @Test (capture the identifier).
TEST_METHOD_NAME_RE = re.compile(
    r"@Test\b(?:\s*\([^)]*\))?\s+(?:public\s+|private\s+|protected\s+)?"
    r"(?:static\s+|final\s+)*(?:void|[\w<>,\s\[\]]+?)\s+([a-zA-Z_]\w*)\s*\(",
    re.MULTILINE,
)
# Two accepted naming forms: shouldX_whenY  |  method_condition_expected.
NAMING_OK_RE = re.compile(
    r"^(?:should[A-Z]\w*_when[A-Z]\w*|[a-z]\w+_[a-z]\w+_[a-z]\w+)$"
)
THREAD_SLEEP_RE = re.compile(r"\bThread\.sleep\s*\(")
NON_DETERMINISTIC_RE = re.compile(
    r"\b(?:Math\.random|System\.currentTimeMillis|System\.nanoTime|"
    r"LocalDate\.now|LocalDateTime\.now|Instant\.now|UUID\.randomUUID)\s*\("
)
AWAITILITY_NO_TIMEOUT_RE = re.compile(
    r"\bAwait(?:ility)?\.\s*await\s*\(\s*\)(?!\s*\.\s*atMost\b)"
)
ASSERT_TRUE_TAUTOLOGY_RE = re.compile(r"\bassertTrue\s*\(\s*true\s*[,)]")
ASSERT_FALSE_TAUTOLOGY_RE = re.compile(r"\bassertFalse\s*\(\s*false\s*[,)]")
# Any real assert/verify call. Used to detect assert-free tests.
ASSERT_OR_VERIFY_RE = re.compile(r"\b(?:assert\w+|verify)\s*\(")
LOGIC_IN_TEST_RE = re.compile(r"\b(if|for|while|switch)\s*\(")
WHEN_COMMENT_RE = re.compile(r"//\s*when\b", re.IGNORECASE)
GIVEN_COMMENT_RE = re.compile(r"//\s*given\b", re.IGNORECASE)
THEN_COMMENT_RE = re.compile(r"//\s*then\b", re.IGNORECASE)
VERIFY_NO_MORE_RE = re.compile(r"\bverifyNoMoreInteractions\s*\(")
# Static field that is NOT final (mutable static state in the test class).
STATIC_MUTABLE_RE = re.compile(
    r"^\s*(?:private|public|protected)?\s*static\s+"
    r"(?!final\b)[\w<>,\s\[\]]+\s+\w+\s*[=;]",
    re.MULTILINE,
)
# `mock(SUTType.class)` / `spy(SUTType.class)` — captured type checked against SUT.
MOCK_OF_TYPE_RE = re.compile(r"\b(?:mock|spy)\s*\(\s*([A-Z]\w*)\s*\.\s*class\s*\)")
# `@Mock SUTType`, `@Spy SUTType`, `@MockBean SUTType` — captured type vs SUT.
MOCK_ANNOTATION_TYPE_RE = re.compile(
    r"@(?:Mock|Spy)(?:Bean)?\b[^\n;]*?\s+([A-Z]\w*)\s+\w+\s*[;=]"
)
# Value object / primitive wrapper types that must never be mocked.
NEVER_MOCK_TYPES: frozenset[str] = frozenset({
    "String", "Integer", "Long", "Double", "Float", "Boolean", "Character",
    "Short", "Byte", "BigDecimal", "BigInteger", "Optional",
    "LocalDate", "LocalDateTime", "LocalTime", "Instant", "Duration",
    "Period", "ZonedDateTime", "OffsetDateTime", "Date", "UUID",
})
# Marker comment that legitimises `verifyNoMoreInteractions` in negative scenarios.
NEGATIVE_SCENARIO_MARKER_RE = re.compile(
    r"//\s*(?:negative\s*scenario|no-more-interactions:\s*intentional)\b",
    re.IGNORECASE,
)

ALLOWED_IMPLICIT = {
    "String", "Integer", "Long", "Double", "Float", "Boolean", "Object",
    "Short", "Byte", "Character",
    "RuntimeException", "Exception", "Throwable", "AssertionError",
    "IllegalArgumentException", "IllegalStateException",
    "ArrayList", "LinkedList", "HashMap", "HashSet",
    "List", "Map", "Set", "Optional", "Collections", "Arrays",
}
ALLOWED_CALLS = {
    "toString", "equals", "hashCode", "getClass", "size", "isEmpty",
    "contains", "add", "put", "get", "orElse", "orElseThrow",
}

# Static-import owners always considered safe regardless of whitelist contents.
# Avoids IMPORT_NOT_WHITELISTED churn on every regenerated test for ubiquitous
# assertion / mocking helpers (AssertJ, Mockito static, Hamcrest, JUnit assertions).
ALLOWED_STATIC_OWNER_PREFIXES: tuple[str, ...] = (
    "org.assertj.core.api.",
    "org.mockito.",
    "org.mockito.Mockito",
    "org.mockito.ArgumentMatchers",
    "org.mockito.BDDMockito",
    "org.hamcrest.",
    "org.hamcrest.MatcherAssert",
    "org.hamcrest.Matchers",
    "org.junit.jupiter.api.Assertions",
    "org.junit.jupiter.api.Assumptions",
    "org.junit.Assert",
)

# ── G5 framework import prefixes ──────────────────────────────────────────────
_G5_JUNIT5_PREFIXES = ("org.junit.jupiter.",)
_G5_JUNIT4_PREFIXES = (
    "org.junit.Test",
    "org.junit.Before",
    "org.junit.After",
    "org.junit.runner.",
    "org.junit.Rule",
    "org.junit.ClassRule",
)
_G5_JUNIT4_EXACT = {
    "org.junit.Test", "org.junit.Before", "org.junit.After",
    "org.junit.Rule", "org.junit.ClassRule",
}
_G5_MOCKITO_PREFIXES = ("org.mockito.",)
_G5_POWERMOCK_PREFIXES = ("org.powermock.",)
_G5_SPRING_TEST_PREFIXES = (
    "org.springframework.boot.test.",
    "org.springframework.test.",
)


# ── Contract helpers ──────────────────────────────────────────────────────────

def load_contracts(
    contracts_dir: Path | None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    by_fqcn: dict[str, dict] = {}
    by_simple: dict[str, dict] = {}
    if not contracts_dir or not contracts_dir.exists():
        return by_fqcn, by_simple
    for p in contracts_dir.glob("*.json"):
        try:
            c = load_json(p)
        except Exception:
            continue
        fqcn = c.get("fqcn")
        if not fqcn:
            continue
        by_fqcn[fqcn] = c
        by_simple[fqcn.rsplit(".", 1)[-1]] = c
    return by_fqcn, by_simple


def method_names(c: dict) -> set[str]:
    out = {m.get("name") for m in c.get("methods", []) if m.get("usable", True)}
    for b in c.get("builders", []):
        out.update(s.get("name") for s in b.get("setters", []))
        out.add((b.get("build") or "build()").split("(")[0])
    return {x for x in out if x}


def resolve_type(
    type_name: str,
    imports: dict[str, str],
    same_pkg: str,
    contracts_by_fqcn: dict[str, dict],
    contracts_by_simple: dict[str, dict],
) -> tuple[str | None, dict | None]:
    raw = re.sub(r"<.*>", "", type_name).strip()
    raw = raw.split(".")[0] if ".Builder" not in raw else raw
    simple = raw.split(".")[0]
    if raw in contracts_by_fqcn:
        return raw, contracts_by_fqcn[raw]
    if simple in imports and imports[simple] in contracts_by_fqcn:
        return imports[simple], contracts_by_fqcn[imports[simple]]
    candidate = f"{same_pkg}.{simple}" if same_pkg else simple
    if candidate in contracts_by_fqcn:
        return candidate, contracts_by_fqcn[candidate]
    if simple in contracts_by_simple:
        c = contracts_by_simple[simple]
        return c.get("fqcn"), c
    return None, None


# ── Index helpers (--index) ───────────────────────────────────────────────────

def load_index_methods(index_dir: Path | None) -> dict[str, set[str]]:
    """Load state/index/methods.json as {fqcn: {method_names}}."""
    if not index_dir:
        return {}
    mf = index_dir / "methods.json"
    if not mf.exists():
        return {}
    try:
        raw = load_json(mf)
    except Exception:
        return {}
    result: dict[str, set[str]] = {}
    for fqcn, entries in raw.items():
        if isinstance(entries, list):
            result[fqcn] = {m.get("name") for m in entries if isinstance(m, dict)}
        elif isinstance(entries, dict):
            methods_list = entries.get("methods") or []
            result[fqcn] = {m.get("name") for m in methods_list if isinstance(m, dict)}
    return result


# ── G5 stack-profile checks ────────────────────────────────────────────────────

def _get_first_module(stack: dict) -> dict:
    modules = stack.get("modules")
    if modules and isinstance(modules, list) and modules:
        return modules[0]
    return stack


def check_g5(stack: dict, imports_list: list[str], text: str) -> list[dict]:
    """Return G5 violations based on stack-profile.json vs declared imports/usage."""
    mod = _get_first_module(stack)
    test_info = mod.get("test") or {}
    mock_info = mod.get("mock") or {}
    di_info = mod.get("di") or {}

    test_fw: str = test_info.get("framework") or ""
    mock_fw: str = mock_info.get("framework") or ""
    mock_features: list[str] = mock_info.get("features") or []
    has_spring: bool = bool(di_info.get("spring"))

    is_junit5 = test_fw in {"junit5", "junit-jupiter"}
    is_junit4 = test_fw in {"junit4", "junit"}
    is_mockito = "mockito" in mock_fw.lower()
    is_powermock = "powermock" in mock_fw.lower()
    has_inline = any("inline" in f for f in mock_features)

    violations: list[dict] = []

    for imp in imports_list:
        # JUnit 5 imports require junit5 in stack
        if any(imp.startswith(p) for p in _G5_JUNIT5_PREFIXES):
            if not is_junit5:
                violations.append({
                    "gate": "G5",
                    "kind": "JUNIT_VERSION_MISMATCH",
                    "import": imp,
                    "reason": f"JUnit5 import but stack declares framework='{test_fw}'",
                })

        # JUnit 4 imports in a JUnit 5 stack
        if imp in _G5_JUNIT4_EXACT or any(
            imp.startswith(p) for p in _G5_JUNIT4_PREFIXES
        ):
            if is_junit5:
                violations.append({
                    "gate": "G5",
                    "kind": "JUNIT_VERSION_MISMATCH",
                    "import": imp,
                    "reason": "JUnit4 import in a JUnit5 stack — mixing runner versions",
                })

        # Mockito imports require mockito in stack
        if any(imp.startswith(p) for p in _G5_MOCKITO_PREFIXES):
            if not is_mockito:
                violations.append({
                    "gate": "G5",
                    "kind": "FRAMEWORK_NOT_IN_STACK",
                    "import": imp,
                    "reason": f"Mockito import but stack declares mock.framework='{mock_fw}'",
                })

        # PowerMock imports require powermock in stack
        if any(imp.startswith(p) for p in _G5_POWERMOCK_PREFIXES):
            if not is_powermock:
                violations.append({
                    "gate": "G5",
                    "kind": "FRAMEWORK_NOT_IN_STACK",
                    "import": imp,
                    "reason": "PowerMock import but powermock not declared in stack",
                })

        # Spring test imports require spring in stack
        if any(imp.startswith(p) for p in _G5_SPRING_TEST_PREFIXES):
            if not has_spring:
                violations.append({
                    "gate": "G5",
                    "kind": "FRAMEWORK_NOT_IN_STACK",
                    "import": imp,
                    "reason": "Spring test import but di.spring is not true in stack",
                })

    # mockStatic usage requires mockito-inline
    if MOCK_STATIC_RE.search(text) and is_mockito and not has_inline:
        violations.append({
            "gate": "G5",
            "kind": "FEATURE_NOT_IN_STACK",
            "feature": "mockStatic",
            "reason": (
                "mockStatic() requires mockito-inline but "
                f"mock.features={mock_features!r} does not declare it"
            ),
        })

    return violations


# ── Context-pack cross-validation (--context-pack) ────────────────────────────

def check_context_pack(cp: dict, test_file: Path) -> list[dict]:
    """Emit a warning-level violation if the context pack SUT doesn't align with the test file name."""
    warnings: list[dict] = []
    sut_raw = cp.get("sut") or ""
    sut_fqcn: str = (sut_raw.get("fqcn") if isinstance(sut_raw, dict) else sut_raw) or cp.get("fqcn") or ""
    if not sut_fqcn:
        return warnings
    expected_simple = sut_fqcn.rsplit(".", 1)[-1] + "Test"
    actual_stem = test_file.stem
    if actual_stem != expected_simple:
        warnings.append({
            "gate": "G5",
            "kind": "CONTEXT_PACK_SUT_MISMATCH",
            "expected": expected_simple + ".java",
            "actual": test_file.name,
            "reason": (
                f"Context pack targets SUT '{sut_fqcn}' "
                f"but the linted file is '{test_file.name}'"
            ),
        })
    return warnings


# ── G6-quality checks (skills/11-quality/) ────────────────────────────────────

def _extract_test_method_bodies(text: str) -> list[tuple[str, str]]:
    """Return [(method_name, body_text), ...] for each `@Test` method.

    Body is delimited by balanced braces starting after the opening `{`.
    """
    out: list[tuple[str, str]] = []
    for m in TEST_METHOD_NAME_RE.finditer(text):
        name = m.group(1)
        # Find the first `{` after the match end (skip throws clause, etc.).
        brace = text.find("{", m.end())
        if brace == -1:
            continue
        depth = 1
        i = brace + 1
        n = len(text)
        while i < n and depth > 0:
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        body = text[brace + 1 : i - 1] if depth == 0 else text[brace + 1 :]
        out.append((name, body))
    return out


def _resolve_sut_simple_name(
    test_file: Path, context_pack: dict | None
) -> str | None:
    """Return the simple name of the SUT (e.g. `FooService`).

    Prefers `context_pack.sut.fqcn`; falls back to stripping `Test` from the
    test file stem.
    """
    if context_pack:
        sut_raw = context_pack.get("sut") or context_pack.get("fqcn") or ""
        sut_fqcn = sut_raw.get("fqcn") if isinstance(sut_raw, dict) else sut_raw
        if sut_fqcn:
            return str(sut_fqcn).rsplit(".", 1)[-1]
    stem = test_file.stem
    if stem.endswith("Test") and len(stem) > 4:
        return stem[:-4]
    if stem.endswith("Tests") and len(stem) > 5:
        return stem[:-5]
    return None


def check_quality(
    text: str, test_file: Path, context_pack: dict | None
) -> list[dict]:
    """Enforce the 14 rules from skills/07-generation/test-quality-gate.md."""
    v: list[dict] = []
    sut_simple = _resolve_sut_simple_name(test_file, context_pack)

    # ── TQG_11_NON_DETERMINISTIC: Thread.sleep / Math.random / *.now() / UUID.random.
    if THREAD_SLEEP_RE.search(text):
        v.append({
            "gate": "G6",
            "kind": "TQG_11_NON_DETERMINISTIC",
            "skill": "11-quality/11",
            "reason": "Thread.sleep is forbidden — use Awaitility.await().atMost(...)",
        })
    for m in NON_DETERMINISTIC_RE.finditer(text):
        v.append({
            "gate": "G6",
            "kind": "TQG_11_NON_DETERMINISTIC",
            "skill": "11-quality/11",
            "symbol": m.group(0).rstrip("("),
            "reason": "Non-deterministic call — inject a Clock/Supplier or use a fixed value",
        })
    if AWAITILITY_NO_TIMEOUT_RE.search(text):
        v.append({
            "gate": "G6",
            "kind": "TQG_11_NON_DETERMINISTIC",
            "skill": "11-quality/11",
            "reason": "Awaitility.await() without .atMost(...) timeout",
        })

    # ── TQG_12_TAUTOLOGY: assertTrue(true) / assertFalse(false).
    for m in ASSERT_TRUE_TAUTOLOGY_RE.finditer(text):
        v.append({
            "gate": "G6",
            "kind": "TQG_12_TAUTOLOGY",
            "skill": "11-quality/12",
            "reason": "assertTrue(true) is a tautology — assert the actual behaviour",
        })
    for m in ASSERT_FALSE_TAUTOLOGY_RE.finditer(text):
        v.append({
            "gate": "G6",
            "kind": "TQG_12_TAUTOLOGY",
            "skill": "11-quality/12",
            "reason": "assertFalse(false) is a tautology — assert the actual behaviour",
        })

    # ── TQG_12_OVER_MOCK: mocking the SUT or value objects.
    # Post-audit 2026-05-28: split into TQG_12_OVER_MOCK_SUT (auto-repairable
    # via convertMockSutToInjectMocks) vs TQG_12_OVER_MOCK (value-object case
    # which still needs LLM judgement). The sub-kind lets repair_dispatch.py
    # pick the deterministic path for SUT-mocks without misfiring on value
    # objects.
    for m in MOCK_OF_TYPE_RE.finditer(text):
        typ = m.group(1)
        if sut_simple and typ == sut_simple:
            v.append({
                "gate": "G6",
                "kind": "TQG_12_OVER_MOCK_SUT",
                "skill": "11-quality/12",
                "symbol": typ,
                "reason": f"SUT '{typ}' must not be mocked",
            })
        elif typ in NEVER_MOCK_TYPES:
            v.append({
                "gate": "G6",
                "kind": "TQG_12_OVER_MOCK",
                "skill": "11-quality/12",
                "symbol": typ,
                "reason": f"Value object '{typ}' must not be mocked",
            })
    for m in MOCK_ANNOTATION_TYPE_RE.finditer(text):
        typ = m.group(1)
        if sut_simple and typ == sut_simple:
            v.append({
                "gate": "G6",
                "kind": "TQG_12_OVER_MOCK_SUT",
                "skill": "11-quality/12",
                "symbol": typ,
                "reason": f"SUT '{typ}' must not be annotated @Mock/@Spy/@MockBean",
            })
        elif typ in NEVER_MOCK_TYPES:
            v.append({
                "gate": "G6",
                "kind": "TQG_12_OVER_MOCK",
                "skill": "11-quality/12",
                "symbol": typ,
                "reason": f"Value object '{typ}' must not be annotated @Mock/@Spy",
            })

    # ── TQG_10_OVER_VERIFY: verifyNoMoreInteractions without negative marker.
    if VERIFY_NO_MORE_RE.search(text) and not NEGATIVE_SCENARIO_MARKER_RE.search(text):
        v.append({
            "gate": "G6",
            "kind": "TQG_10_OVER_VERIFY",
            "skill": "11-quality/10",
            "reason": (
                "verifyNoMoreInteractions is brittle — add "
                "`// negative scenario` marker comment if intentional"
            ),
        })

    # ── TQG_10_STATIC_STATE: non-final static field in the test class.
    for m in STATIC_MUTABLE_RE.finditer(text):
        v.append({
            "gate": "G6",
            "kind": "TQG_10_STATIC_STATE",
            "skill": "11-quality/10",
            "reason": "Mutable static field in test class — leaks state between tests",
        })

    # ── Per-method checks: naming, AAA, logic-in-test, eager test, assert-free.
    method_bodies = _extract_test_method_bodies(text)
    for name, body in method_bodies:
        # TQG_03_NAMING
        if not NAMING_OK_RE.match(name):
            v.append({
                "gate": "G6",
                "kind": "TQG_03_NAMING",
                "skill": "11-quality/03",
                "method": name,
                "reason": (
                    f"'{name}' does not match should*_when* or "
                    "snake_case spec form (method_condition_expected)"
                ),
            })

        # TQG_02_NO_AAA: body must contain // given, // when, // then.
        has_given = bool(GIVEN_COMMENT_RE.search(body))
        has_when = bool(WHEN_COMMENT_RE.search(body))
        has_then = bool(THEN_COMMENT_RE.search(body))
        if not (has_given and has_when and has_then):
            missing = [
                lbl for lbl, present in (
                    ("given", has_given),
                    ("when", has_when),
                    ("then", has_then),
                ) if not present
            ]
            v.append({
                "gate": "G6",
                "kind": "TQG_02_NO_AAA",
                "skill": "11-quality/02",
                "method": name,
                "reason": f"AAA separators missing: // {' // '.join(missing)}",
            })

        # TQG_09_LOGIC_IN_TEST: control flow inside test body.
        if LOGIC_IN_TEST_RE.search(body):
            v.append({
                "gate": "G6",
                "kind": "TQG_09_LOGIC_IN_TEST",
                "skill": "11-quality/09",
                "method": name,
                "reason": "Control flow (if/for/while/switch) inside test body",
            })

        # TQG_11_EAGER_TEST: more than one `// when` separator.
        if len(WHEN_COMMENT_RE.findall(body)) > 1:
            v.append({
                "gate": "G6",
                "kind": "TQG_11_EAGER_TEST",
                "skill": "11-quality/11",
                "method": name,
                "reason": "Multiple `// when` separators — split into multiple tests",
            })

        # TQG_12_ASSERT_FREE: no real assert*/verify call in body.
        if not ASSERT_OR_VERIFY_RE.search(body):
            v.append({
                "gate": "G6",
                "kind": "TQG_12_ASSERT_FREE",
                "skill": "11-quality/12",
                "method": name,
                "reason": "Test has no assert*/verify call — assert observable behaviour",
            })

    return v


# ── Core lint function ────────────────────────────────────────────────────────

def lint(
    test_file: Path,
    whitelist: dict,
    contracts_dir: Path | None,
    stack_profile: dict | None = None,
    index_dir: Path | None = None,
    context_pack: dict | None = None,
    quality_checks: bool = True,
) -> dict:
    text = test_file.read_text(encoding="utf-8", errors="ignore")
    classes = {c["fqcn"]: c for c in whitelist.get("classes", [])}
    packages = {p["name"] for p in whitelist.get("packages", [])}
    violations: list[dict] = []
    contracts_by_fqcn, contracts_by_simple = load_contracts(contracts_dir)
    index_methods = load_index_methods(index_dir)

    pkg_m = PACKAGE_RE.search(text)
    same_pkg = pkg_m.group(1) if pkg_m else ""

    # ── G1: import whitelist check ────────────────────────────────────────────
    declared_imports: dict[str, str] = {}
    static_imports: set[str] = set()
    raw_imports: list[str] = []

    for m in IMPORT_RE.finditer(text):
        is_static = bool(m.group(1))
        target = m.group(2)
        if is_static:
            static_imports.add(target)
            continue
        if target.endswith(".*"):
            pkg = target[:-2]
            if pkg not in packages:
                violations.append({
                    "gate": "G1",
                    "kind": "IMPORT_PKG_NOT_WHITELISTED",
                    "import": target,
                })
            raw_imports.append(target[:-2])
            continue
        raw_imports.append(target)
        declared_imports[target.rsplit(".", 1)[-1]] = target
        if target in classes:
            continue
        pkg = target.rsplit(".", 1)[0]
        if pkg not in packages:
            violations.append({
                "gate": "G1",
                "kind": "IMPORT_NOT_WHITELISTED",
                "import": target,
            })

    for target in static_imports:
        owner = target.rsplit(".", 1)[0]
        # Allow ubiquitous test-framework static helpers without forcing them
        # through the whitelist (avoids false positives on AssertJ/Mockito/etc.).
        if any(target.startswith(p) or owner.startswith(p)
               for p in ALLOWED_STATIC_OWNER_PREFIXES):
            continue
        if owner not in classes and owner.rsplit(".", 1)[0] not in packages:
            violations.append({
                "gate": "G1",
                "kind": "STATIC_IMPORT_NOT_WHITELISTED",
                "import": "static " + target,
            })

    # ── G5: stack-profile compatibility ──────────────────────────────────────
    if stack_profile:
        violations.extend(check_g5(stack_profile, raw_imports, text))

    # ── Context-pack cross-validation ─────────────────────────────────────────
    if context_pack:
        violations.extend(check_context_pack(context_pack, test_file))

    # ── G6-quality (skills/11-quality/) ───────────────────────────────────────
    if quality_checks:
        violations.extend(check_quality(text, test_file, context_pack))

    # ── G2-lite: FreeBuilder guard ────────────────────────────────────────────
    for m in DIRECT_GENERATED_BUILDER_RE.finditer(text):
        violations.append({
            "gate": "G2",
            "kind": "DIRECT_GENERATED_BUILDER_FORBIDDEN",
            "symbol": m.group(1),
        })

    # ── G2-lite: variable type map ────────────────────────────────────────────
    var_types: dict[str, tuple[str, dict | None, bool]] = {}
    for m in VAR_DECL_RE.finditer(text):
        typ = m.group("type").strip()
        var = m.group("var")
        is_builder = typ.endswith(".Builder") or typ == "Builder"
        owner = typ.replace(".Builder", "")
        fq, c = resolve_type(
            owner, declared_imports, same_pkg, contracts_by_fqcn, contracts_by_simple
        )
        if c:
            var_types[var] = (fq or owner, c, is_builder)

    # ── G2-lite: new X(...) ───────────────────────────────────────────────────
    for m in NEW_RE.finditer(text):
        typ = m.group(1)
        owner = typ.replace(".Builder", "")
        is_builder_ctor = typ.endswith(".Builder")
        fq, c = resolve_type(
            owner, declared_imports, same_pkg, contracts_by_fqcn, contracts_by_simple
        )
        if not c:
            if (
                owner.split(".")[0] not in ALLOWED_IMPLICIT
                and owner.split(".")[0] in declared_imports
            ):
                pass  # imported but no contract — leave to the compiler
            continue
        inst = c.get("instantiation", {})
        kind = c.get("kind")
        if is_builder_ctor:
            builders = c.get("builders", [])
            entry_ok = any(
                ".Builder()" in (b.get("entry") or "")
                for b in builders
            )
            if not entry_ok:
                violations.append({
                    "gate": "G2",
                    "kind": "BUILDER_NOT_VERIFIED",
                    "symbol": typ,
                    "fqcn": c.get("fqcn"),
                })
            continue
        if (
            kind in {"interface", "abstract", "annotation"}
            or not inst.get("allowed", False)
            or inst.get("strategy") not in {"constructor", "concrete"}
        ):
            violations.append({
                "gate": "G2",
                "kind": "INSTANTIATION_NOT_ALLOWED",
                "symbol": typ,
                "fqcn": c.get("fqcn"),
                "strategy": inst.get("strategy"),
                "reason": inst.get("reason"),
            })

    # ── G2-lite: instance method calls ───────────────────────────────────────
    for m in CALL_RE.finditer(text):
        var = m.group("var")
        meth = m.group("method")
        if meth in ALLOWED_CALLS or var not in var_types:
            continue
        fq, c, is_builder = var_types[var]
        allowed = method_names(c)
        if meth in allowed:
            continue
        # Supplement from state/index/methods.json
        index_allowed = index_methods.get(fq or "", set())
        if meth in index_allowed:
            continue
        violations.append({
            "gate": "G2",
            "kind": "METHOD_NOT_IN_CONTRACT",
            "receiver": var,
            "receiverType": fq,
            "method": meth,
        })

    # ── G2-lite: static method calls ─────────────────────────────────────────
    for m in STATIC_CALL_RE.finditer(text):
        typ = m.group("type")
        meth = m.group("method")
        if typ in {
            "Mockito", "Assertions", "Assert", "Collections", "Arrays", "Optional"
        }:
            continue
        fq, c = resolve_type(
            typ, declared_imports, same_pkg, contracts_by_fqcn, contracts_by_simple
        )
        if not c:
            continue
        allowed = method_names(c)
        if meth in allowed:
            continue
        index_allowed = index_methods.get(fq or "", set())
        if meth in index_allowed:
            continue
        violations.append({
            "gate": "G2",
            "kind": "STATIC_METHOD_NOT_IN_CONTRACT",
            "receiverType": fq,
            "method": meth,
        })

    return {"file": str(test_file), "violations": violations}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Static pre-compile linter for generated Java test files. "
            "Enforces G1 (import whitelist), G2-lite (symbol evidence), "
            "and optionally G5 (stack-profile compatibility)."
        )
    )
    ap.add_argument(
        "--test-file",
        required=False,
        metavar="PATH",
        help="Java test file to lint (single-file mode).",
    )
    ap.add_argument(
        "--batch",
        default=None,
        metavar="DIR_OR_GLOB",
        help=(
            "Batch mode: lint every *.java under DIR (recursively) or every "
            "path matching the given glob. Produces ONE consolidated JSON report "
            "with per-file 'violations'. Avoids re-spawning the linter per micro-edit."
        ),
    )
    ap.add_argument(
        "--fail-fast",
        action="store_true",
        help="In --batch mode, stop on the first file with violations.",
    )
    ap.add_argument(
        "--whitelist",
        required=True,
        metavar="PATH",
        help="state/import-whitelist.json produced by the Python pre-stage.",
    )
    ap.add_argument(
        "--contracts",
        default=None,
        metavar="DIR",
        help="state/symbol-contracts/ directory (one JSON file per FQCN).",
    )
    ap.add_argument(
        "--stack-profile",
        default=None,
        metavar="PATH",
        help=(
            "state/stack-profile.json — enables G5 checks: "
            "JUnit/Mockito/Spring version compatibility."
        ),
    )
    ap.add_argument(
        "--index",
        default=None,
        metavar="DIR",
        help=(
            "state/index/ directory — loads methods.json as G2 fallback "
            "when symbol contracts are absent for a type."
        ),
    )
    ap.add_argument(
        "--context-pack",
        default=None,
        metavar="PATH",
        help=(
            "state/context-packs/<fqcn>.json — cross-validates that the "
            "linted test file corresponds to the expected SUT."
        ),
    )
    ap.add_argument(
        "--no-quality-checks",
        action="store_false",
        dest="quality_checks",
        default=True,
        help=(
            "Disable G6-quality checks (skills/11-quality/). Default: enabled. "
            "When enabled, enforces AAA structure, naming, anti-patterns "
            "(mystery-guest, coupled/brittle, eager, over-mocking, assert-free) "
            "and non-determinism. See "
            "skills/07-generation/test-quality-gate.md for the rule catalog."
        ),
    )
    args = ap.parse_args()

    if not args.test_file and not args.batch:
        print("[FAIL] One of --test-file or --batch is required", file=sys.stderr)
        return 2

    try:
        wl = load_json(Path(args.whitelist))
    except Exception as exc:
        print(f"[FAIL] Cannot load whitelist: {exc}", file=sys.stderr)
        return 2

    contracts = Path(args.contracts) if args.contracts else None

    stack_profile: dict | None = None
    if args.stack_profile:
        sp_path = Path(args.stack_profile)
        if sp_path.exists():
            try:
                stack_profile = load_json(sp_path)
            except Exception as exc:
                print(
                    f"[WARN] Cannot load stack-profile, G5 skipped: {exc}",
                    file=sys.stderr,
                )
        else:
            print(
                f"[WARN] --stack-profile path not found ({sp_path}), G5 skipped.",
                file=sys.stderr,
            )

    index_dir: Path | None = Path(args.index) if args.index else None

    context_pack: dict | None = None
    if args.context_pack:
        cp_path = Path(args.context_pack)
        if cp_path.exists():
            try:
                context_pack = load_json(cp_path)
            except Exception as exc:
                print(
                    f"[WARN] Cannot load context-pack, cross-validation skipped: {exc}",
                    file=sys.stderr,
                )

    def _lint_one(fp: Path) -> dict:
        return lint(
            fp,
            wl,
            contracts,
            stack_profile=stack_profile,
            index_dir=index_dir,
            context_pack=context_pack,
            quality_checks=args.quality_checks,
        )

    if args.batch:
        b = args.batch
        bp = Path(b)
        if bp.is_dir():
            files = sorted(bp.rglob("*.java"))
        else:
            from glob import glob
            files = [Path(p) for p in sorted(glob(b, recursive=True)) if p.endswith(".java")]
        if not files:
            print(f"[FAIL] --batch matched zero Java files: {b}", file=sys.stderr)
            return 2
        batch_report: dict = {"mode": "batch", "files": []}
        total_violations = 0
        for fp in files:
            r = _lint_one(fp)
            batch_report["files"].append(r)
            total_violations += len(r.get("violations") or [])
            if args.fail_fast and r.get("violations"):
                batch_report["stoppedEarly"] = True
                break
        batch_report["totalFiles"] = len(batch_report["files"])
        batch_report["totalViolations"] = total_violations
        print(json.dumps(batch_report, indent=2, ensure_ascii=False))
        return 1 if total_violations else 0

    test_file = Path(args.test_file)
    if not test_file.exists():
        print(f"[FAIL] Test file not found: {test_file}", file=sys.stderr)
        return 2
    report = _lint_one(test_file)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
