"""test_g1_reverse.py — fix #4: reverse-G1 pre-Maven gate.

test_linter.check_g1_reverse flags a JUnit/Mockito/AssertJ/Hamcrest symbol used
in the body whose import is absent — the "cannot find symbol" compile failure
that forward G1 (declared→whitelisted) cannot see. Detection is by simple name
and conservative around wildcards.

Run: `python tools/python/tests/test_g1_reverse.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from test_linter import check_g1_reverse, lint  # noqa: E402


def _kinds(text: str) -> set[str]:
    return {f"{v['kind']}:{v.get('symbol')}" for v in check_g1_reverse(text)}


_HEAD = "package com.acme;\n"


# ── type symbol (the original failure) ────────────────────────────────────────

def case_qualified_assertions_missing_flagged() -> None:
    text = _HEAD + "class T { void m() { Assertions.assertEquals(1, x); } }"
    if "IMPORT_MISSING_FOR_SYMBOL:Assertions" not in _kinds(text):
        raise AssertionError("missing Assertions import was not flagged")


def case_qualified_assertions_present_ok() -> None:
    text = (_HEAD + "import org.junit.jupiter.api.Assertions;\n"
            "class T { void m() { Assertions.assertEquals(1, x); } }")
    if check_g1_reverse(text):
        raise AssertionError(f"present import must not flag: {check_g1_reverse(text)}")


def case_nonstatic_wildcard_satisfies_type() -> None:
    text = (_HEAD + "import org.junit.jupiter.api.*;\n"
            "class T { void m() { Assertions.assertEquals(1, x); } }")
    if check_g1_reverse(text):
        raise AssertionError("non-static wildcard must satisfy the type symbol")


def case_argumentcaptor_missing_flagged() -> None:
    text = _HEAD + "class T { void m() { ArgumentCaptor<String> c; } }"
    if "IMPORT_MISSING_FOR_SYMBOL:ArgumentCaptor" not in _kinds(text):
        raise AssertionError("missing ArgumentCaptor import was not flagged")


# ── static helper symbols ─────────────────────────────────────────────────────

def case_bare_static_missing_flagged() -> None:
    text = _HEAD + "class T { void m() { assertEquals(1, x); } }"
    if "STATIC_IMPORT_MISSING_FOR_SYMBOL:assertEquals" not in _kinds(text):
        raise AssertionError("missing static assertEquals import was not flagged")


def case_bare_static_present_ok() -> None:
    text = (_HEAD + "import static org.junit.jupiter.api.Assertions.assertEquals;\n"
            "class T { void m() { assertEquals(1, x); } }")
    if check_g1_reverse(text):
        raise AssertionError("present static import must not flag")


def case_static_wildcard_satisfies() -> None:
    text = (_HEAD + "import static org.mockito.Mockito.*;\n"
            "class T { void m() { when(r.f()).thenReturn(null); verify(r).f(); } }")
    if check_g1_reverse(text):
        raise AssertionError("static wildcard must satisfy bare Mockito helpers")


def case_assertthat_name_based_any_owner() -> None:
    # Hamcrest assertThat import satisfies a bare assertThat regardless of stack.
    text = (_HEAD + "import static org.hamcrest.MatcherAssert.assertThat;\n"
            "class T { void m() { assertThat(x, is(1)); } }")
    flagged = {v.get("symbol") for v in check_g1_reverse(text)}
    if "assertThat" in flagged:
        raise AssertionError("statically imported assertThat must be satisfied by name")


# ── no false positives ────────────────────────────────────────────────────────

def case_project_symbols_clean() -> None:
    text = _HEAD + "class T { void m() { svc.doWork(); foo.bar(); } }"
    if check_g1_reverse(text):
        raise AssertionError(f"project symbols must not be flagged: {check_g1_reverse(text)}")


def case_comment_and_string_clean() -> None:
    text = (_HEAD + "class T { void m() {\n"
            "  // when verify assertEquals are only words here\n"
            '  String s = "assertEquals and Assertions and ArgumentCaptor";\n'
            "} }")
    if check_g1_reverse(text):
        raise AssertionError("symbols in comments/strings must not be flagged")


# ── integration: wired into lint() ────────────────────────────────────────────

def case_lint_reports_reverse_g1() -> None:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        wl = {"schemaVersion": 1, "module": "x", "packages": [], "classes": []}
        f = tdp / "FooTest.java"
        f.write_text(_HEAD + "class FooTest { void m() { Assertions.assertEquals(1, x); } }",
                     encoding="utf-8")
        report = lint(f, wl, contracts_dir=None)
        kinds = {v.get("kind") for v in report["violations"]}
        if "IMPORT_MISSING_FOR_SYMBOL" not in kinds:
            raise AssertionError(f"lint() did not surface reverse-G1: {report['violations']}")


def main() -> int:
    cases = [
        ("qualified-assertions-missing-flagged",  case_qualified_assertions_missing_flagged),
        ("qualified-assertions-present-ok",        case_qualified_assertions_present_ok),
        ("nonstatic-wildcard-satisfies-type",      case_nonstatic_wildcard_satisfies_type),
        ("argumentcaptor-missing-flagged",         case_argumentcaptor_missing_flagged),
        ("bare-static-missing-flagged",            case_bare_static_missing_flagged),
        ("bare-static-present-ok",                 case_bare_static_present_ok),
        ("static-wildcard-satisfies",              case_static_wildcard_satisfies),
        ("assertthat-name-based-any-owner",        case_assertthat_name_based_any_owner),
        ("project-symbols-clean",                  case_project_symbols_clean),
        ("comment-and-string-clean",               case_comment_and_string_clean),
        ("lint-reports-reverse-g1",                case_lint_reports_reverse_g1),
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
    print("\nAll reverse-G1 cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
