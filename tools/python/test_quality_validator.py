"""test_quality_validator.py — AST-based Test Quality Gate (F4.R6, post-audit 2026-05-28).

Complements `test_linter.py:check_quality` (regex-based) with javalang-AST checks
that regex cannot reliably catch:

  * TQG_12_SWALLOWED       — try/catch with no fail()/assert*/verify in the catch
  * TQG_10_TEST_ORDER_DEP  — @TestMethodOrder without a justifying marker comment
  * TQG_02_NO_AAA          — // given / // when / // then present but OUT OF ORDER
                              (test_linter only checks presence)
  * TQG_12_TAUTOLOGY       — assertNotNull(<literal>) / assertNotNull(new X())

The 10 regex-based checks from `test_linter.check_quality` are re-run here so a
single tool invocation produces a complete report.

Architectural premise (`docs/deterministic-architecture.md`):
  * Determinism over agency — this tool runs BEFORE any LLM repair turn
  * Reports violations as JSON; never rewrites Java source
  * Repair is delegated to repair-rules/ or escalateToLLM

CLI
---
  python tools/python/test_quality_validator.py \
      --test-file path/to/FooTest.java \
      [--context-pack <state/context-packs/X.json>] \
      [--out report.json]

Exit codes
----------
  0  no blocker violations
  2  one or more violations found
  3  malformed input (file unreadable or unparseable)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import _TimedRun, atomic_write_json  # noqa: E402

try:
    import javalang  # type: ignore
    from javalang.tree import (  # type: ignore
        TryStatement,
        MethodInvocation,
        Annotation,
        Literal,
        ClassCreator,
    )
    _HAS_JAVALANG = True
except Exception:
    _HAS_JAVALANG = False

# Re-use regex-based checks so the tool emits a single consolidated report.
from test_linter import check_quality  # noqa: E402

EXIT_OK = 0
EXIT_VIOLATIONS = 2
EXIT_MALFORMED = 3

# ── AST-based checks ─────────────────────────────────────────────────────────

_ASSERT_OR_VERIFY_NAMES = {
    "fail", "assertEquals", "assertNotEquals", "assertTrue", "assertFalse",
    "assertNull", "assertNotNull", "assertThat", "assertThrows",
    "assertDoesNotThrow", "assertSame", "assertNotSame", "assertArrayEquals",
    "assertAll", "assertIterableEquals", "assertLinesMatch", "assertTimeout",
    "verify", "verifyNoInteractions", "verifyNoMoreInteractions",
    "then",  # BDDMockito.then(mock).should()
}


def _catch_has_assert_or_fail(catch_block: list) -> bool:
    """Walk every MethodInvocation inside a catch block; True if any is an
    assert*/verify/fail call. Empty or comment-only catches return False."""
    if not catch_block:
        return False
    for stmt in catch_block:
        for path, node in _walk(stmt):
            if isinstance(node, MethodInvocation) and node.member in _ASSERT_OR_VERIFY_NAMES:
                return True
    return False


def _walk(node: Any):
    """Yield every (path, child) under `node` (javalang-style filter, but on a
    sub-tree instead of the whole compilation unit)."""
    if hasattr(node, "filter"):
        yield from node.filter(object)
    else:
        # Leaf or non-Node — nothing to walk
        return


def _enclosing_method_name(path: tuple) -> str | None:
    """Walk the javalang `path` tuple to find the nearest MethodDeclaration."""
    from javalang.tree import MethodDeclaration  # local import — only used when AST available

    for node in reversed(path):
        if isinstance(node, MethodDeclaration):
            return node.name
    return None


def check_try_swallowed(tree: Any) -> list[dict]:
    """TQG_12_SWALLOWED — every try/catch must end its catch in fail() or
    assert*()/verify() or have an explicit `// expected` marker."""
    violations: list[dict] = []
    for path, node in tree.filter(TryStatement):
        method_name = _enclosing_method_name(path)
        for catch in (node.catches or []):
            if _catch_has_assert_or_fail(catch.block):
                continue
            violations.append({
                "gate": "G6",
                "kind": "TQG_12_SWALLOWED",
                "skill": "11-quality/12",
                "method": method_name,
                "reason": (
                    "Empty catch block or catch without assert/fail/verify — "
                    "exception swallowed silently"
                ),
            })
    return violations


def check_test_method_order(tree: Any, text: str) -> list[dict]:
    """TQG_10_TEST_ORDER_DEP — class-level @TestMethodOrder is forbidden unless
    a comment in the class body justifies it: `// test-order-justified:<reason>`.
    """
    violations: list[dict] = []
    from javalang.tree import ClassDeclaration  # local import

    has_justification = "test-order-justified:" in text
    for path, node in tree.filter(ClassDeclaration):
        for ann in (node.annotations or []):
            simple = (ann.name or "").rsplit(".", 1)[-1]
            if simple == "TestMethodOrder" and not has_justification:
                violations.append({
                    "gate": "G6",
                    "kind": "TQG_10_TEST_ORDER_DEP",
                    "skill": "11-quality/10",
                    "symbol": node.name,
                    "reason": (
                        "@TestMethodOrder forces inter-test dependency — add "
                        "`// test-order-justified:<reason>` comment to opt in"
                    ),
                })
    return violations


def check_aaa_ordering(text: str) -> list[dict]:
    """TQG_02_NO_AAA (stronger) — // given, // when, // then must appear in
    order within each test method. test_linter only checks presence, not order.

    Operates on the raw text per-method to avoid losing line numbers."""
    violations: list[dict] = []
    method_bodies = _extract_method_bodies_with_comments(text)
    for name, body in method_bodies:
        markers: list[tuple[int, str]] = []
        for label in ("given", "when", "then"):
            idx = body.find(f"// {label}")
            if idx >= 0:
                markers.append((idx, label))
        if len(markers) < 3:
            # presence already covered by test_linter — skip here
            continue
        ordered_labels = [lbl for _, lbl in sorted(markers)]
        if ordered_labels != ["given", "when", "then"]:
            violations.append({
                "gate": "G6",
                "kind": "TQG_02_NO_AAA",
                "skill": "11-quality/02",
                "method": name,
                "reason": (
                    f"AAA separators out of order: found {ordered_labels} — "
                    "expected given → when → then"
                ),
            })
    return violations


def check_unused_stubs(text: str) -> list[dict]:
    """TQG_06_UNUSED_STUB — Mockito stub declared but the stubbed method is
    never invoked elsewhere in the same test method.

    Heuristic: per @Test body, collect every ``when(mock.method(...))`` and
    every ``given(mock.method(...))``, then count occurrences of
    ``mock.method`` anywhere in the body. If the count is exactly one (the
    stub itself), the stub is unused. ``doX().when(mock).method(...)`` and
    ``BDDMockito.willX().given(mock).method(...)`` are also recognised.

    The check is intentionally conservative: any uncertainty (different mock
    names with shared method, fluent chains) skips the report. False
    negatives are preferred to false positives — the LLM repair-agent is
    still the backstop.
    """
    import re

    when_call = re.compile(r"\bwhen\s*\(\s*(\w+)\s*\.\s*(\w+)\s*\(")
    given_call = re.compile(r"\bgiven\s*\(\s*(\w+)\s*\.\s*(\w+)\s*\(")
    do_when = re.compile(
        r"\b(?:doReturn|doThrow|doAnswer|doNothing|doCallRealMethod)\s*\([^)]*\)\s*\.\s*when\s*\(\s*(\w+)\s*\)\s*\.\s*(\w+)\s*\("
    )
    will_given = re.compile(
        r"\b(?:willReturn|willThrow|willAnswer|willDoNothing|willCallRealMethod)\s*\([^)]*\)\s*\.\s*given\s*\(\s*(\w+)\s*\)\s*\.\s*(\w+)\s*\("
    )

    violations: list[dict] = []
    for name, body in _extract_method_bodies_with_comments(text):
        stubs: set[tuple[str, str]] = set()
        for rx in (when_call, given_call, do_when, will_given):
            for m in rx.finditer(body):
                stubs.add((m.group(1), m.group(2)))
        for mock, method in stubs:
            usage_rx = re.compile(rf"\b{re.escape(mock)}\.\s*{re.escape(method)}\s*\(")
            if len(usage_rx.findall(body)) <= 1:
                violations.append({
                    "gate": "G6",
                    "kind": "TQG_06_UNUSED_STUB",
                    "skill": "11-quality/06",
                    "method": name,
                    "symbol": f"{mock}.{method}",
                    "reason": (
                        f"Stub {mock}.{method}(...) is declared but never invoked "
                        f"elsewhere in {name} — drop the stub or exercise the method"
                    ),
                })
    return violations


def check_assert_not_null_tautology(tree: Any) -> list[dict]:
    """TQG_12_TAUTOLOGY (extension) — assertNotNull(<literal>) or
    assertNotNull(new X()) is a tautology: the argument can never be null."""
    violations: list[dict] = []
    for path, node in tree.filter(MethodInvocation):
        if node.member != "assertNotNull":
            continue
        args = node.arguments or []
        if not args:
            continue
        first = args[0]
        # Literal (string, number, char, bool) or `new X()` is never null
        if isinstance(first, (Literal, ClassCreator)):
            method_name = _enclosing_method_name(path)
            violations.append({
                "gate": "G6",
                "kind": "TQG_12_TAUTOLOGY",
                "skill": "11-quality/12",
                "method": method_name,
                "reason": (
                    "assertNotNull(<literal>) is a tautology — assert the "
                    "actual observable behaviour instead"
                ),
            })
    return violations


# ── Method-body extraction (line-aware, keeps comments) ──────────────────────

_TEST_METHOD_HEAD = (
    # @Test (any variant) on prev lines, then `<modifiers> [return] name(...) {`
    # Naive but matches the same shape that test_linter._extract_test_method_bodies uses.
)


def _extract_method_bodies_with_comments(text: str) -> list[tuple[str, str]]:
    """Return [(method_name, body_with_comments), ...] for every method whose
    signature is preceded by an @Test/@ParameterizedTest annotation.

    Keeps comments inside the body (unlike test_linter._extract_test_method_bodies
    which is regex-driven and may collapse whitespace)."""
    import re

    # Match @Test / @ParameterizedTest / @RepeatedTest (optionally fully
    # qualified, e.g. @org.junit.jupiter.api.Test) on its own line, optionally
    # followed by extra annotation lines, then a method signature with `{`.
    pattern = re.compile(
        r"@(?:[\w.]+\.)?(?:Test|ParameterizedTest|RepeatedTest)\b[^\n]*\n"
        r"(?:\s*@[\w.]+[^\n]*\n)*"
        r"\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?"
        r"(?:[\w.<>,\s\[\]]+?\s+)?"
        r"(?P<name>[A-Za-z_]\w*)\s*\([^)]*\)\s*(?:throws\s+[^{]+)?\{",
        re.MULTILINE,
    )
    out: list[tuple[str, str]] = []
    for m in pattern.finditer(text):
        name = m.group("name")
        body_start = m.end()  # right after the opening `{`
        # Balance braces to find body end
        depth = 1
        i = body_start
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        body_end = i - 1 if depth == 0 else len(text)
        out.append((name, text[body_start:body_end]))
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def validate(
    test_file: Path,
    context_pack: dict | None = None,
) -> tuple[list[dict], str | None]:
    """Run every quality check and return (violations, parse_error).

    `parse_error` is None on success; a string when javalang fails to parse the
    test file (the regex-based checks still run in that case).
    """
    text = test_file.read_text(encoding="utf-8", errors="ignore")
    violations: list[dict] = []

    # 1) Regex-based checks from test_linter (10 of the 14 TQG rules)
    violations.extend(check_quality(text, test_file, context_pack))

    # 2) AAA ordering — pure text, independent of javalang availability
    violations.extend(check_aaa_ordering(text))

    # 3) Unused-stub detection — pure text, independent of javalang
    violations.extend(check_unused_stubs(text))

    # 4) AST-based checks — only if javalang is installed and parse succeeds
    parse_error: str | None = None
    if not _HAS_JAVALANG:
        parse_error = "javalang not installed — AST checks skipped"
    else:
        try:
            tree = javalang.parse.parse(text)
        except Exception as e:
            parse_error = f"javalang parse error: {e}"
        else:
            violations.extend(check_try_swallowed(tree))
            violations.extend(check_test_method_order(tree, text))
            violations.extend(check_assert_not_null_tautology(tree))

    return violations, parse_error


# ── CLI ──────────────────────────────────────────────────────────────────────

def _build_report(
    test_file: Path,
    violations: list[dict],
    parse_error: str | None,
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "testFile": str(test_file),
        "javalangAvailable": _HAS_JAVALANG,
        "parseError": parse_error,
        "totalViolations": len(violations),
        "violations": violations,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the AST-based test quality gate (F4.R6).",
    )
    ap.add_argument("--test-file", required=True, help="Path to *Test.java")
    ap.add_argument(
        "--context-pack",
        default=None,
        help="Optional path to state/context-packs/<fqcn>.json (enables SUT-aware checks).",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Optional output JSON path. If omitted, report is printed to stdout.",
    )
    args = ap.parse_args()

    test_file = Path(args.test_file).resolve()
    if not test_file.exists():
        print(f"[FAIL] test file not found: {test_file}", file=sys.stderr)
        return EXIT_MALFORMED

    context_pack: dict | None = None
    if args.context_pack:
        cp_path = Path(args.context_pack).resolve()
        if not cp_path.exists():
            print(f"[FAIL] context pack not found: {cp_path}", file=sys.stderr)
            return EXIT_MALFORMED
        try:
            context_pack = json.loads(cp_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[FAIL] cannot parse context pack: {e}", file=sys.stderr)
            return EXIT_MALFORMED

    violations, parse_error = validate(test_file, context_pack)
    report = _build_report(test_file, violations, parse_error)

    if args.out:
        atomic_write_json(Path(args.out).resolve(), report)
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))

    if violations:
        return EXIT_VIOLATIONS
    return EXIT_OK


if __name__ == "__main__":
    with _TimedRun("test_quality_validator") as _tr:
        _rc = main()
        if _rc == EXIT_VIOLATIONS:
            _tr.set_status("VIOLATIONS")
        elif _rc != EXIT_OK:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
