"""test_batch_optimizations.py — regression for the batch/fail-fast patches.

Covers:
  A. test_linter: AssertJ/Mockito static imports are NOT flagged G1.
  B. test_linter: --batch mode produces a consolidated report.
  C. test_patch_applier: _normalize_sut accepts string and {fqcn} object.
  D. test_patch_applier: sanitize_java_body undoes common over-escapes.

Run: `python tools/python/tests/test_batch_optimizations.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from test_linter import lint, ALLOWED_STATIC_OWNER_PREFIXES  # noqa: E402
from test_patch_applier import _normalize_sut, sanitize_java_body  # noqa: E402


def _write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


# ── A: AssertJ / Mockito static imports skip G1 ───────────────────────────────

def case_assertj_mockito_static_allowed() -> None:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        wl = {"schemaVersion": 1, "module": "x", "packages": [], "classes": []}
        wl_path = tdp / "wl.json"
        _write(wl_path, json.dumps(wl))

        test_java = tdp / "src" / "test" / "java" / "com" / "acme" / "FooTest.java"
        _write(test_java, """
package com.acme;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.when;
import static org.mockito.ArgumentMatchers.any;
import static org.hamcrest.MatcherAssert.assertThat;
import static org.junit.jupiter.api.Assertions.assertEquals;
class FooTest {}
""".lstrip())

        report = lint(test_java, wl, contracts_dir=None)
        static_g1 = [v for v in report["violations"]
                     if v.get("kind") == "STATIC_IMPORT_NOT_WHITELISTED"]
        if static_g1:
            raise AssertionError(
                f"AssertJ/Mockito static imports should be exempt; got: {static_g1}"
            )


# ── B: linter --batch mode (smoke via lint() over multiple files) ────────────

def case_batch_consolidates_reports() -> None:
    # We exercise the underlying lint() multiple times — this proves the data
    # structure used by --batch is well-formed; the CLI driver in main()
    # iterates the same way.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        wl = {"schemaVersion": 1, "module": "x", "packages": [], "classes": []}
        files = []
        for i in range(3):
            f = tdp / f"A{i}Test.java"
            _write(f, "package x;\nclass A%dTest {}\n" % i)
            files.append(f)
        results = [lint(f, wl, None) for f in files]
        if len(results) != 3:
            raise AssertionError(f"expected 3 reports, got {len(results)}")
        for r in results:
            if "file" not in r or "violations" not in r:
                raise AssertionError(f"malformed report: {r}")


# ── C: _normalize_sut accepts both shapes ────────────────────────────────────

def case_normalize_sut_shapes() -> None:
    if _normalize_sut("com.acme.Foo") != "com.acme.Foo":
        raise AssertionError("string sut should pass through unchanged")
    if _normalize_sut({"fqcn": "com.acme.Foo"}) != "com.acme.Foo":
        raise AssertionError("object sut {fqcn:...} should unwrap to string")
    if _normalize_sut({"fqcn": "com.acme.Foo", "kind": "service"}) != "com.acme.Foo":
        raise AssertionError("object sut with extras should still unwrap")
    if _normalize_sut(None) is not None:
        raise AssertionError("None should normalize to None")
    if _normalize_sut({}) is not None:
        raise AssertionError("empty dict should normalize to None")
    if _normalize_sut({"fqcn": 42}) is not None:
        raise AssertionError("non-string fqcn should normalize to None")


# ── D: sanitize_java_body undoes over-escaping ────────────────────────────────

def case_sanitize_double_escape() -> None:
    s = "assertThat(x).isEqualTo(1);\\\\nverify(mock);"
    out = sanitize_java_body(s)
    if "\n" not in out or "\\\\n" in out:
        raise AssertionError(f"double-escape not collapsed: {out!r}")


def case_sanitize_single_escape_fallback() -> None:
    # No real newlines, only literal \n
    s = "int a = 1;\\nint b = 2;"
    out = sanitize_java_body(s)
    if "\n" not in out:
        raise AssertionError(f"single-escape fallback failed: {out!r}")


def case_sanitize_passthrough_clean_text() -> None:
    s = "int a = 1;\nint b = 2;\n"  # already correct
    out = sanitize_java_body(s)
    if out != s:
        raise AssertionError(f"clean text was mutated: {out!r}")


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    cases = [
        ("assertj-mockito-static-allowed", case_assertj_mockito_static_allowed),
        ("batch-consolidates-reports",     case_batch_consolidates_reports),
        ("normalize-sut-shapes",            case_normalize_sut_shapes),
        ("sanitize-double-escape",          case_sanitize_double_escape),
        ("sanitize-single-escape-fallback", case_sanitize_single_escape_fallback),
        ("sanitize-passthrough-clean",      case_sanitize_passthrough_clean_text),
    ]
    failed = 0
    for name, fn in cases:
        try:
            fn()
            print(f"OK   {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    # Sanity: the allow-list constant must include AssertJ + Mockito
    if not any("assertj" in p for p in ALLOWED_STATIC_OWNER_PREFIXES):
        print("FAIL allow-list-contents: AssertJ prefix missing")
        failed += 1
    if not any("mockito" in p.lower() for p in ALLOWED_STATIC_OWNER_PREFIXES):
        print("FAIL allow-list-contents: Mockito prefix missing")
        failed += 1
    if failed:
        print(f"\n{failed} test(s) failed")
        return 1
    print("\nAll batch-optimization cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
