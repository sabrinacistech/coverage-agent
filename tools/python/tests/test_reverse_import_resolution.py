"""test_reverse_import_resolution.py — symbol-used → import-required (root fix).

Regression for the compile failure "cannot find symbol: variable Assertions":
the LLM emitted `Assertions.assertEquals(...)` (or a bare `assertEquals(...)`)
without declaring its import, and nothing downstream added it. G1 only checks
declared→whitelisted, never used→declared, so the gap reached Maven.

`_ensure_required_imports` closes it deterministically by resolving the curated
JUnit / Mockito / AssertJ / Hamcrest symbol set to the exact import each needs.

Run: `python tools/python/tests/test_reverse_import_resolution.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from test_patch_applier import _ensure_required_imports, _stack_view  # noqa: E402


def _wrap(body: str, imports: str = "") -> str:
    return (
        "package com.acme;\n\n"
        "import org.junit.jupiter.api.Test;\n"
        f"{imports}"
        "\nclass FooTest {\n"
        "    @Test\n"
        "    void should_do_x() {\n"
        f"{body}\n"
        "    }\n"
        "}\n"
    )


# ── (a) the exact reported failure: qualified Assertions ───────────────────────

def case_qualified_assertions_adds_class_import() -> None:
    text = _wrap("        // then\n        Assertions.assertEquals(120L, sut.get());")
    out = _ensure_required_imports(text, {"testFramework": "junit5", "assertFramework": "assertj"})
    if "import org.junit.jupiter.api.Assertions;" not in out:
        raise AssertionError("qualified Assertions.assertEquals did not get its class import")


def case_qualified_assertions_junit4_resolves_to_legacy() -> None:
    text = _wrap("        Assertions.assertEquals(1, 1);")
    out = _ensure_required_imports(text, {"testFramework": "junit4", "assertFramework": "assertj"})
    if "import org.junit.Assert;" not in out:
        raise AssertionError("junit4 stack must resolve Assertions to org.junit.Assert")


# ── (b) bare static helpers ───────────────────────────────────────────────────

def case_bare_junit_assert_adds_static_import() -> None:
    text = _wrap("        assertEquals(120L, sut.get());\n        assertThrows(RuntimeException.class, sut::boom);")
    out = _ensure_required_imports(text, {"testFramework": "junit5", "assertFramework": "assertj"})
    for needed in (
        "import static org.junit.jupiter.api.Assertions.assertEquals;",
        "import static org.junit.jupiter.api.Assertions.assertThrows;",
    ):
        if needed not in out:
            raise AssertionError(f"missing static import: {needed}")


def case_bare_mockito_and_matchers() -> None:
    text = _wrap("        when(repo.find(any(String.class))).thenReturn(null);\n        verify(repo).find(eq(\"x\"));")
    out = _ensure_required_imports(text, None)  # defaults: junit5 / assertj
    for needed in (
        "import static org.mockito.Mockito.when;",
        "import static org.mockito.Mockito.verify;",
        "import static org.mockito.ArgumentMatchers.any;",
        "import static org.mockito.ArgumentMatchers.eq;",
    ):
        if needed not in out:
            raise AssertionError(f"missing Mockito static import: {needed}")


# ── (c) type tokens ───────────────────────────────────────────────────────────

def case_type_tokens_added() -> None:
    text = _wrap(
        "        ArgumentCaptor<String> cap = ArgumentCaptor.forClass(String.class);\n"
        "        try (MockedStatic<Foo> ms = Mockito.mockStatic(Foo.class)) { }"
    )
    out = _ensure_required_imports(text, None)
    for needed in (
        "import org.mockito.ArgumentCaptor;",
        "import org.mockito.MockedStatic;",
        "import org.mockito.Mockito;",
    ):
        if needed not in out:
            raise AssertionError(f"missing type import: {needed}")


# ── (d) assertThat disambiguation by stack ────────────────────────────────────

def case_assertthat_defaults_to_assertj() -> None:
    text = _wrap("        assertThat(sut.get()).isEqualTo(1);")
    out = _ensure_required_imports(text, {"testFramework": "junit5", "assertFramework": "assertj"})
    if "import static org.assertj.core.api.Assertions.assertThat;" not in out:
        raise AssertionError("assertThat must resolve to AssertJ by default")


def case_assertthat_hamcrest_when_stack_says_so() -> None:
    text = _wrap("        assertThat(sut.get(), is(1));")
    out = _ensure_required_imports(text, {"testFramework": "junit5", "assertFramework": "hamcrest"})
    if "import static org.hamcrest.MatcherAssert.assertThat;" not in out:
        raise AssertionError("hamcrest stack must resolve assertThat to MatcherAssert")
    if "org.assertj.core.api.Assertions.assertThat" in out:
        raise AssertionError("hamcrest stack must NOT pull AssertJ assertThat")


# ── (e) safety: idempotent, no false positives from comments/strings/projects ──

def case_idempotent_and_no_duplicates() -> None:
    text = _wrap(
        "        assertEquals(1, 1);",
        imports="import static org.junit.jupiter.api.Assertions.assertEquals;\n",
    )
    out = _ensure_required_imports(text, None)
    if out.count("import static org.junit.jupiter.api.Assertions.assertEquals;") != 1:
        raise AssertionError("existing static import was duplicated")


def case_comment_and_string_do_not_trigger() -> None:
    text = _wrap(
        "        // when we call verify it should pass\n"
        '        String note = "remember to assertEquals later";\n'
        "        sut.run();"
    )
    out = _ensure_required_imports(text, None)
    if "org.mockito.Mockito.verify" in out:
        raise AssertionError("`// when ... verify` comment must not add a Mockito import")
    if "Assertions.assertEquals" in out:
        raise AssertionError("assertEquals inside a string literal must not add an import")


def case_project_symbols_untouched() -> None:
    text = _wrap("        MyService svc = new MyService();\n        svc.doWork();")
    out = _ensure_required_imports(text, None)
    # No framework token matches → nothing added beyond the original imports.
    if "import org.mockito" in out or "import static org" in out:
        raise AssertionError("project-only test must not gain framework imports")


# ── (f) compact-pack stack view ───────────────────────────────────────────────

def case_stack_view_reads_compact() -> None:
    sv = _stack_view({"stk": ["21", "junit5", "mockito", "assertj", True, "jakarta"]})
    if sv != {"testFramework": "junit5", "assertFramework": "assertj"}:
        raise AssertionError(f"compact stk not parsed: {sv}")
    sv2 = _stack_view({"stack": {"testFramework": "junit4", "assertFramework": "hamcrest"}})
    if sv2 != {"testFramework": "junit4", "assertFramework": "hamcrest"}:
        raise AssertionError(f"verbose stack not parsed: {sv2}")


def main() -> int:
    cases = [
        ("qualified-assertions-adds-class-import",  case_qualified_assertions_adds_class_import),
        ("qualified-assertions-junit4-legacy",      case_qualified_assertions_junit4_resolves_to_legacy),
        ("bare-junit-assert-static-import",         case_bare_junit_assert_adds_static_import),
        ("bare-mockito-and-matchers",               case_bare_mockito_and_matchers),
        ("type-tokens-added",                       case_type_tokens_added),
        ("assertthat-defaults-to-assertj",          case_assertthat_defaults_to_assertj),
        ("assertthat-hamcrest-when-stack-says-so",  case_assertthat_hamcrest_when_stack_says_so),
        ("idempotent-and-no-duplicates",            case_idempotent_and_no_duplicates),
        ("comment-and-string-do-not-trigger",       case_comment_and_string_do_not_trigger),
        ("project-symbols-untouched",               case_project_symbols_untouched),
        ("stack-view-reads-compact",                case_stack_view_reads_compact),
    ]
    failed = 0
    for name, fn in cases:
        try:
            fn()
            print(f"OK   {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    if failed:
        print(f"\n{failed} test(s) failed")
        return 1
    print("\nAll reverse-import-resolution cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
