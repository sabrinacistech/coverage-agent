"""test_assert_dialect_and_version_diag.py — fixes #2 and #5.

#2  Template assert dialect is chosen from stack.assertFramework instead of being
    hardcoded to AssertJ. junit-builtin / hamcrest projects no longer get an
    AssertJ `assertThat` seeded (which fails to compile when AssertJ is absent).

#5  Framework-version resolution degradations are surfaced loudly:
    stack_profile_detector._unresolved_framework_versions flags "unknown"
    test/mock versions (and whether cp.txt was present), and
    classpath_resolver._classpath_degraded detects a failed build-classpath.

Run: `python tools/python/tests/test_assert_dialect_and_version_diag.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from classpath_resolver import _classpath_degraded  # noqa: E402
from stack_profile_detector import _unresolved_framework_versions  # noqa: E402
from test_patch_applier import (  # noqa: E402
    _assert_imports_for,
    _assert_not_null_for,
    _ensure_required_imports,
    _load_template,
    _prune_unused_imports,
    _render_from_template,
)

_TEMPLATES_DIR = HERE.parents[2] / "templates"


# ── #2: assert dialect helpers ────────────────────────────────────────────────

def case_assert_imports_per_framework() -> None:
    if "org.assertj.core.api.Assertions.assertThat" not in _assert_imports_for("assertj"):
        raise AssertionError("assertj must seed AssertJ assertThat")
    if "org.hamcrest.MatcherAssert.assertThat" not in _assert_imports_for("hamcrest"):
        raise AssertionError("hamcrest must seed MatcherAssert")
    if _assert_imports_for("junit-builtin") != "":
        raise AssertionError("junit-builtin must seed nothing (resolver adds on use)")
    # unknown/none default to AssertJ (historical Spring starter-test behaviour).
    if "assertj" not in _assert_imports_for("none"):
        raise AssertionError("unknown assert framework must default to AssertJ")


def case_assert_not_null_per_framework() -> None:
    if _assert_not_null_for("assertj") != "assertThat(sut).isNotNull();":
        raise AssertionError("assertj not-null wrong")
    if _assert_not_null_for("junit-builtin") != "assertNotNull(sut);":
        raise AssertionError("junit-builtin not-null wrong")
    if "notNullValue" not in _assert_not_null_for("hamcrest"):
        raise AssertionError("hamcrest not-null wrong")


# ── #2: template rendering respects the stack ─────────────────────────────────

def _render(stack: dict | None) -> str:
    tpl = _load_template("junit5-mockito", _TEMPLATES_DIR)
    return _render_from_template(tpl, {
        "sut": "com.acme.FooService",
        "testPackage": "com.acme",
        "fields": [],
        "methods": [],
    }, stack=stack)


def case_template_assertj_default() -> None:
    out = _render({"assertFramework": "assertj"})
    if "import static org.assertj.core.api.Assertions.assertThat;" not in out:
        raise AssertionError("assertj stack must render AssertJ import")


def case_template_junit_builtin_has_no_assertj() -> None:
    out = _render({"assertFramework": "junit-builtin"})
    if "assertj" in out:
        raise AssertionError("junit-builtin stack must NOT render any AssertJ import")


def case_template_hamcrest() -> None:
    out = _render({"assertFramework": "hamcrest"})
    if "org.hamcrest.MatcherAssert.assertThat" not in out:
        raise AssertionError("hamcrest stack must render MatcherAssert import")
    if "assertj" in out:
        raise AssertionError("hamcrest stack must NOT pull AssertJ")


def case_springboot_context_loads_dialect() -> None:
    tpl = _load_template("springboot-test", _TEMPLATES_DIR)
    patch = {"sut": "com.acme.AppIT", "testPackage": "com.acme", "fields": [], "methods": []}
    out_assertj = _render_from_template(tpl, patch, stack={"assertFramework": "assertj"})
    if "assertThat(sut).isNotNull();" not in out_assertj:
        raise AssertionError("springboot assertj contextLoads wrong")
    out_junit = _render_from_template(tpl, patch, stack={"assertFramework": "junit-builtin"})
    if "assertNotNull(sut);" not in out_junit:
        raise AssertionError("springboot junit-builtin contextLoads wrong")
    if "assertThat" in out_junit:
        raise AssertionError("springboot junit-builtin must not use assertThat")


def case_junit_builtin_end_to_end_gets_correct_import() -> None:
    # junit-builtin SUT whose body uses assertEquals: template seeds no AssertJ,
    # the reverse resolver (#1) supplies the JUnit static import → compiles.
    tpl = _load_template("junit5-mockito", _TEMPLATES_DIR)
    rendered = _render_from_template(tpl, {
        "sut": "com.acme.Calc",
        "testPackage": "com.acme",
        "fields": [],
        "methods": [{
            "name": "should_add_when_two_ints",
            "annotations": ["@Test"],
            "body": "// given\nint a = 2;\n// when\nint r = sut.add(a, 3);\n// then\nassertEquals(5, r);",
        }],
    }, stack={"testFramework": "junit5", "assertFramework": "junit-builtin"})
    final = _ensure_required_imports(_prune_unused_imports(rendered),
                                     {"testFramework": "junit5", "assertFramework": "junit-builtin"})
    if "import static org.junit.jupiter.api.Assertions.assertEquals;" not in final:
        raise AssertionError("junit-builtin assertEquals did not get its JUnit static import")
    if "assertj" in final:
        raise AssertionError("junit-builtin test must not contain any AssertJ import")


# ── #5: version-diagnostic helpers ────────────────────────────────────────────

def _module(test_v: str, mock_v: str, path: str) -> dict:
    return {"path": path, "test": {"version": test_v}, "mock": {"version": mock_v}}


def case_unresolved_versions_flags_unknown() -> None:
    with tempfile.TemporaryDirectory() as td:
        # cp.txt present for this module → cpPresent True
        mod = Path(td)
        (mod / "target").mkdir()
        (mod / "target" / "cp.txt").write_text("x", encoding="utf-8")
        rows = _unresolved_framework_versions([_module("unknown", "unknown", str(mod))])
        if len(rows) != 1:
            raise AssertionError(f"expected one unresolved row: {rows}")
        r = rows[0]
        if set(r["unresolved"]) != {"test", "mock"} or r["cpPresent"] is not True:
            raise AssertionError(f"row content wrong: {r}")


def case_unresolved_versions_detects_missing_cp() -> None:
    rows = _unresolved_framework_versions([_module("unknown", "5.11.0", "/nope/mod")])
    if len(rows) != 1 or rows[0]["unresolved"] != ["test"]:
        raise AssertionError(f"only test should be unresolved: {rows}")
    if rows[0]["cpPresent"] is not False:
        raise AssertionError("missing cp.txt must report cpPresent=False")


def case_resolved_versions_yield_no_rows() -> None:
    if _unresolved_framework_versions([_module("5.10.5", "5.11.0", "/m")]):
        raise AssertionError("fully resolved module must produce no diagnostic rows")


def case_classpath_degraded_predicate() -> None:
    if not _classpath_degraded(1, "/a/b.jar"):
        raise AssertionError("non-zero mvn exit must be degraded")
    if not _classpath_degraded(0, "   "):
        raise AssertionError("empty cp.txt must be degraded")
    if _classpath_degraded(0, "/a/b.jar:/c/d.jar"):
        raise AssertionError("exit 0 + non-empty cp.txt must NOT be degraded")


def main() -> int:
    cases = [
        ("assert-imports-per-framework",            case_assert_imports_per_framework),
        ("assert-not-null-per-framework",           case_assert_not_null_per_framework),
        ("template-assertj-default",                case_template_assertj_default),
        ("template-junit-builtin-no-assertj",       case_template_junit_builtin_has_no_assertj),
        ("template-hamcrest",                       case_template_hamcrest),
        ("springboot-context-loads-dialect",        case_springboot_context_loads_dialect),
        ("junit-builtin-end-to-end-import",         case_junit_builtin_end_to_end_gets_correct_import),
        ("unresolved-versions-flags-unknown",       case_unresolved_versions_flags_unknown),
        ("unresolved-versions-detects-missing-cp",  case_unresolved_versions_detects_missing_cp),
        ("resolved-versions-no-rows",               case_resolved_versions_yield_no_rows),
        ("classpath-degraded-predicate",            case_classpath_degraded_predicate),
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
    print("\nAll assert-dialect / version-diagnostic cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
