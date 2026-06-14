"""framework_imports.py — single source of truth: the JUnit / Mockito / AssertJ /
Hamcrest symbols a generated test may use, and the import each one requires.

Shared by two consumers so the auto-fixer and the gate can never drift:

  - test_patch_applier._ensure_required_imports  (FIX): resolves the imports a
    body needs and injects the missing ones. This is the deterministic pass that
    makes "cannot find symbol: variable Assertions" impossible.

  - test_linter.check_g1_reverse                 (GATE): pre-Maven detection that
    flags a *used* framework symbol whose import is *absent* — the inverse of G1
    (which only checks declared→whitelisted). The gate compares by SIMPLE NAME so
    it never depends on which assert library the stack resolved to (a statically
    imported ``assertThat`` from AssertJ or Hamcrest both satisfy a bare call).

Resolution is purely lexical: it scans code with comments / string literals /
import lines blanked, so a ``// when`` AAA marker or a symbol named only in a
string is never mistaken for a real use. Only the curated framework symbol set is
ever recognised — project types are never touched, so neither consumer can mask a
real hallucination (those still fail G1/G2).
"""
from __future__ import annotations

import re

# ── Static helper owners (a bare `name(...)` call → `import static <owner>.<name>;`)
OWNER_JUNIT5_ASSERT = "org.junit.jupiter.api.Assertions"
OWNER_JUNIT4_ASSERT = "org.junit.Assert"
OWNER_ASSERTJ = "org.assertj.core.api.Assertions"
OWNER_HAMCREST = "org.hamcrest.MatcherAssert"
OWNER_MOCKITO = "org.mockito.Mockito"
OWNER_MATCHERS = "org.mockito.ArgumentMatchers"
OWNER_BDD = "org.mockito.BDDMockito"

JUNIT_ASSERT_METHODS: frozenset[str] = frozenset({
    "assertEquals", "assertNotEquals", "assertTrue", "assertFalse", "assertNull",
    "assertNotNull", "assertSame", "assertNotSame", "assertArrayEquals",
    "assertThrows", "assertThrowsExactly", "assertDoesNotThrow", "assertAll",
    "assertInstanceOf", "assertIterableEquals", "assertLinesMatch",
    "assertTimeout", "assertTimeoutPreemptively", "fail",
})
ASSERTJ_METHODS: frozenset[str] = frozenset({
    "assertThat", "assertThatThrownBy", "assertThatExceptionOfType",
    "assertThatCode", "assertThatNullPointerException",
    "assertThatIllegalArgumentException", "assertThatIllegalStateException",
    "assertThatNoException", "catchThrowable", "catchThrowableOfType",
})
MOCKITO_METHODS: frozenset[str] = frozenset({
    "mock", "spy", "when", "verify", "verifyNoInteractions",
    "verifyNoMoreInteractions", "doThrow", "doNothing", "doReturn", "doAnswer",
    "doCallRealMethod", "inOrder", "mockStatic", "mockConstruction", "reset",
    "clearInvocations", "withSettings", "times", "never", "atLeast",
    "atLeastOnce", "atMost", "calls",
})
MATCHER_METHODS: frozenset[str] = frozenset({
    "any", "anyInt", "anyLong", "anyShort", "anyByte", "anyChar", "anyDouble",
    "anyFloat", "anyBoolean", "anyString", "anyList", "anyMap", "anySet",
    "anyCollection", "anyIterable", "eq", "argThat", "isNull", "isNotNull",
    "notNull", "nullable", "isA", "same", "refEq",
})
BDD_METHODS: frozenset[str] = frozenset({
    "given", "willReturn", "willThrow", "willDoNothing", "willAnswer",
    "willCallRealMethod", "then",
})

# Framework TYPE tokens referenced bare or qualified (`Token.x` / `Token<...>`)
# → non-static `import <fqcn>;`. `Assertions` is handled separately because it is
# ambiguous between JUnit and AssertJ (resolved by the method that follows).
TYPE_IMPORTS: dict[str, str] = {
    "Mockito": "org.mockito.Mockito",
    "BDDMockito": "org.mockito.BDDMockito",
    "ArgumentCaptor": "org.mockito.ArgumentCaptor",
    "ArgumentMatchers": "org.mockito.ArgumentMatchers",
    "MockedStatic": "org.mockito.MockedStatic",
    "MockedConstruction": "org.mockito.MockedConstruction",
    "Answers": "org.mockito.Answers",
    "InOrder": "org.mockito.InOrder",
}

# All static helper method names known to the catalog (any owner).
ALL_STATIC_METHODS: frozenset[str] = (
    JUNIT_ASSERT_METHODS | ASSERTJ_METHODS | MOCKITO_METHODS
    | MATCHER_METHODS | BDD_METHODS
)

_QUALIFIED_ASSERTIONS_RE = re.compile(r"(?<![\w.])Assertions\s*\.\s*(\w+)")
_BARE_CALL_RE = re.compile(r"(?<![\w.])([a-zA-Z]\w*)\s*\(")
_IMPORT_LINE_RE = re.compile(
    r"^[ \t]*import[ \t]+(?:static[ \t]+)?[\w.]+(?:\.\*)?[ \t]*;[ \t]*$",
    re.MULTILINE,
)


def strip_noise(text: str) -> str:
    """Blank out import lines, comments and string/char literals so a symbol named
    only there is never counted as a real use."""
    text = _IMPORT_LINE_RE.sub(" ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)   # block comments
    text = re.sub(r"//[^\n]*", " ", text)                      # line comments
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', " ", text)           # string literals
    text = re.sub(r"'(?:\\.|[^'\\\n])*'", " ", text)           # char literals
    return text


def static_helper_registry(test_fw: str, assert_fw: str) -> dict[str, str]:
    """Map bare helper name → owning class FQCN, parameterised by the stack.

    JUnit-assert helpers point at JUnit4 vs JUnit5 per ``test_fw``; ``assertThat``
    & friends point at AssertJ or Hamcrest per ``assert_fw`` (AssertJ is the
    default, matching the deterministic template's import block).
    """
    junit_owner = OWNER_JUNIT4_ASSERT if test_fw == "junit4" else OWNER_JUNIT5_ASSERT
    reg: dict[str, str] = {}
    for m in JUNIT_ASSERT_METHODS:
        reg[m] = junit_owner
    for m in MOCKITO_METHODS:
        reg[m] = OWNER_MOCKITO
    for m in MATCHER_METHODS:
        reg[m] = OWNER_MATCHERS
    for m in BDD_METHODS:
        reg[m] = OWNER_BDD
    if assert_fw == "hamcrest":
        reg["assertThat"] = OWNER_HAMCREST
    elif assert_fw in ("assertj", "", "unknown") or assert_fw is None:
        for m in ASSERTJ_METHODS:
            reg[m] = OWNER_ASSERTJ
    return reg


def resolve_imports(
    text: str, test_fw: str = "junit5", assert_fw: str = "assertj"
) -> tuple[list[str], list[str]]:
    """Owner-accurate resolution for the FIX path.

    Returns ``(type_imports, static_imports)`` where type_imports are FQCNs for
    ``import X;`` and static_imports are ``owner.member`` for ``import static
    owner.member;`` — exactly what a body's framework symbols require.
    """
    scan = strip_noise(text)
    type_imports: list[str] = []
    static_imports: list[str] = []

    # (a) `Assertions.<method>` qualified usage → import the right Assertions class.
    for m in _QUALIFIED_ASSERTIONS_RE.finditer(scan):
        method = m.group(1)
        if method in ASSERTJ_METHODS:
            type_imports.append(OWNER_ASSERTJ)
        else:
            type_imports.append(
                OWNER_JUNIT4_ASSERT if test_fw == "junit4" else OWNER_JUNIT5_ASSERT
            )

    # (b) other framework TYPE tokens referenced bare or qualified.
    for token, fqcn in TYPE_IMPORTS.items():
        if re.search(rf"(?<![\w.]){re.escape(token)}\b", scan):
            type_imports.append(fqcn)

    # (c) bare static helper calls → static import of the owning class member.
    registry = static_helper_registry(test_fw, assert_fw)
    for m in _BARE_CALL_RE.finditer(scan):
        owner = registry.get(m.group(1))
        if owner:
            static_imports.append(f"{owner}.{m.group(1)}")

    # Preserve order, drop dups.
    return list(dict.fromkeys(type_imports)), list(dict.fromkeys(static_imports))


def used_framework_symbols(text: str) -> tuple[set[str], set[str]]:
    """Name-based detection for the GATE path.

    Returns ``(type_simple_names, static_member_names)`` actually used in the
    body. Owner-agnostic on purpose: the gate only needs to know that some import
    must provide each name, not which library it came from.
    """
    scan = strip_noise(text)
    type_names: set[str] = set()
    static_names: set[str] = set()

    # Qualified `Assertions.<method>` requires the `Assertions` TYPE to be imported.
    if _QUALIFIED_ASSERTIONS_RE.search(scan):
        type_names.add("Assertions")

    for token in TYPE_IMPORTS:
        if re.search(rf"(?<![\w.]){re.escape(token)}\b", scan):
            type_names.add(token)

    for m in _BARE_CALL_RE.finditer(scan):
        name = m.group(1)
        if name in ALL_STATIC_METHODS:
            static_names.add(name)

    return type_names, static_names
