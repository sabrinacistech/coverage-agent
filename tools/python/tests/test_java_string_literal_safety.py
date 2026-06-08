"""test_java_string_literal_safety.py — Java string-literal escaping + detection.

Covers the two centralized helpers in ``common.py`` that keep generated Java
test source compilable:

  * ``java_string_literal(value)`` — produce-side: escape test data into a valid
    Java ``String`` literal (quotes included).
  * ``has_raw_newline_inside_java_string(source)`` — verify-side: detect a raw
    newline/CR inside a normal Java string literal (the "unclosed string literal"
    compile error), used as a pre-write backstop in test_patch_applier.py.

Plus an end-to-end check that the patcher's render pass turns a body carrying
REAL control characters into escaped, compilable Java that the guard accepts.

Legacy-suite convention: expose ``main() -> int`` (0 = ok). Auto-discovered by
test_aa_suite_runner.py. Run standalone:
    python tools/python/tests/test_java_string_literal_safety.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # import common, test_patch_applier

from common import has_raw_newline_inside_java_string, java_string_literal  # noqa: E402

FAILURES: list[str] = []


def _check(label: str, got, expected) -> None:
    if got != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {got!r}")


# ── java_string_literal: produce-side ─────────────────────────────────────────

def case_escape_plain() -> None:
    _check("plain", java_string_literal("abc"), '"abc"')


def case_escape_newline() -> None:
    _check("newline", java_string_literal("a\nb"), '"a\\nb"')


def case_escape_tab() -> None:
    _check("tab", java_string_literal("a\tb"), '"a\\tb"')


def case_escape_cr() -> None:
    _check("cr", java_string_literal("a\rb"), '"a\\rb"')


def case_escape_quote() -> None:
    _check("quote", java_string_literal('a"b'), '"a\\"b"')


def case_escape_backslash() -> None:
    # A single backslash must become two; quotes are added around it.
    _check("backslash", java_string_literal("a\\b"), '"a\\\\b"')


def case_escape_combined() -> None:
    _check("combined", java_string_literal("a\nb\tc"), '"a\\nb\\tc"')


def case_escape_backslash_before_n() -> None:
    # Order guarantee: a literal backslash followed by 'n' must NOT collapse into
    # a newline escape. Input is two chars: '\\' and 'n'.
    _check("backslash-then-n", java_string_literal("\\n"), '"\\\\n"')


def case_escape_output_is_clean() -> None:
    # Whatever we emit must pass the guard (no raw control char survives).
    for raw in ["a\nb\tc", 'q"q', "back\\slash", "\r\n\t"]:
        lit = java_string_literal(raw)
        src = f"String v = {lit};"
        if has_raw_newline_inside_java_string(src):
            FAILURES.append(f"escaped output still trips guard: raw={raw!r} -> {src!r}")


# ── has_raw_newline_inside_java_string: verify-side ───────────────────────────

def case_detect_raw_newline() -> None:
    src = 'String value = "a\nb";'
    _check("detect-raw-newline", has_raw_newline_inside_java_string(src), True)


def case_detect_raw_crlf() -> None:
    src = 'String value = "a\r\nb";'
    _check("detect-raw-crlf", has_raw_newline_inside_java_string(src), True)


def case_accept_escaped_newline() -> None:
    src = 'String value = "a\\nb";'  # backslash-n, valid Java
    _check("accept-escaped-newline", has_raw_newline_inside_java_string(src), False)


def case_accept_escaped_tab() -> None:
    src = 'String value = "a\\tb";'
    _check("accept-escaped-tab", has_raw_newline_inside_java_string(src), False)


def case_accept_plain() -> None:
    src = 'String value = "abc";'
    _check("accept-plain", has_raw_newline_inside_java_string(src), False)


def case_accept_escaped_quote() -> None:
    src = 'String value = "a\\"b";'  # escaped quote does not close the literal
    _check("accept-escaped-quote", has_raw_newline_inside_java_string(src), False)


def case_accept_escaped_backslash() -> None:
    src = 'String value = "a\\\\b";'
    _check("accept-escaped-backslash", has_raw_newline_inside_java_string(src), False)


def case_newline_between_statements_ok() -> None:
    # Real newlines OUTSIDE string literals (between statements) are fine.
    src = 'String a = "x";\nString b = "y";\n'
    _check("newline-between-stmts", has_raw_newline_inside_java_string(src), False)


def case_quote_in_line_comment_no_false_positive() -> None:
    # A dangling quote inside a // comment must not open a string and trip on the
    # following real newline.
    src = '// comentario con "a\nint x = 1;\n'
    _check("quote-in-line-comment", has_raw_newline_inside_java_string(src), False)


def case_quote_in_block_comment_no_false_positive() -> None:
    src = '/* a "b\nc */\nint x = 1;\n'
    _check("quote-in-block-comment", has_raw_newline_inside_java_string(src), False)


# ── End-to-end: render of REAL control chars yields compilable Java ───────────

def case_render_real_control_chars_is_valid() -> None:
    import test_patch_applier as T

    method = {
        "name": "shouldReplaceControlChars_whenValueHasNewlinesAndTabs",
        "annotations": ["@Test"],
        # Body carries REAL 0x0A / 0x09 bytes inside the literal — the documented
        # root cause. The render pass must escape them.
        "body": (
            "// given\nString value = \"a" + chr(10) + "b" + chr(9) + "c\";\n"
            "// when\nString result = LogSanitizer.sanitizeForLog(value);\n"
            "// then\nassertThat(result).isEqualTo(\"a_b_c\");"
        ),
        "evidenceIds": ["sym:com.acme.LogSanitizer#sanitizeForLog:12345678"],
    }
    rendered = T._render_method(method)
    if has_raw_newline_inside_java_string(rendered):
        FAILURES.append(
            "render of real control chars still contains a raw newline inside a "
            f"literal:\n{rendered}"
        )
    if '"a\\nb\\tc"' not in rendered:
        FAILURES.append(
            f"render did not escape control chars to \"a\\nb\\tc\"; got:\n{rendered}"
        )


def main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("case_")]
    for c in cases:
        try:
            c()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{c.__name__} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print("FAIL test_java_string_literal_safety:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK   test_java_string_literal_safety: {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
