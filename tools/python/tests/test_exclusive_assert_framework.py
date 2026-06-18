"""test_exclusive_assert_framework.py — exclusive assertion-framework strategy.

Root cause: a generated test could end up importing BOTH
``org.junit.jupiter.api.Assertions`` and ``org.assertj.core.api.Assertions``.
They share the simple name ``Assertions``, so any ``Assertions.assertEquals(...)``
becomes an ambiguous reference and the file no longer compiles.

This suite covers the three deterministic defences added for it:

  1. framework_imports.AssertionFramework — strict config coercion + the
     exclusivity rule in resolve_imports()/static_helper_registry() (the LLM never
     picks the dialect; ``stack.assertFramework`` does).
  2. ast_patcher._dedup_imports_by_simple_name — keeps the configured dialect's
     ``Assertions`` import, drops the loser, and FQN-rewrites the loser's calls.
  3. test_patch_applier._render_from_template — no unresolved placeholder, no
     redundant blank lines, ${ASSERT_IMPORTS} resolved from the exclusive dialect.

Run: `python tools/python/tests/test_exclusive_assert_framework.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ast_patcher import _dedup_imports_by_simple_name  # noqa: E402
from framework_imports import (  # noqa: E402
    OWNER_ASSERTJ,
    OWNER_HAMCREST,
    OWNER_JUNIT4_ASSERT,
    OWNER_JUNIT5_ASSERT,
    AssertionFramework,
    assertions_owner_for,
    resolve_imports,
    static_helper_registry,
)
from test_patch_applier import (  # noqa: E402
    _assert_imports_for,
    _assert_no_unresolved_placeholders,
    _inject_imports,
    _load_template,
    _render_from_template,
)

_TEMPLATES_DIR = HERE.parents[2] / "templates"


def _file(body: str, imports: str) -> str:
    return (
        "package com.acme;\n\n"
        "import org.junit.jupiter.api.Test;\n"
        f"{imports}"
        "\nclass FooTest {\n"
        "    @Test\n"
        "    void should_x_when_y() {\n"
        f"{body}\n"
        "    }\n"
        "}\n"
    )


# ── 1. AssertionFramework.coerce — strict, backwards compatible ────────────────

def case_coerce_canonical_and_aliases() -> None:
    if AssertionFramework.coerce("assertj") is not AssertionFramework.ASSERTJ:
        raise AssertionError("'assertj' must coerce to ASSERTJ")
    if AssertionFramework.coerce("hamcrest") is not AssertionFramework.HAMCREST:
        raise AssertionError("'hamcrest' must coerce to HAMCREST")
    if AssertionFramework.coerce("junit-builtin") is not AssertionFramework.JUNIT5:
        raise AssertionError("'junit-builtin' must coerce to JUNIT5")
    for alias in ("junit5", "JUnit", "jupiter"):
        if AssertionFramework.coerce(alias) is not AssertionFramework.JUNIT5:
            raise AssertionError(f"alias {alias!r} must coerce to JUNIT5")
    if AssertionFramework.coerce(AssertionFramework.HAMCREST) is not AssertionFramework.HAMCREST:
        raise AssertionError("an already-coerced enum must pass through")


def case_coerce_sentinels_default_to_assertj() -> None:
    for sentinel in (None, "", "  ", "unknown", "none", "DEFAULT", "Auto"):
        if AssertionFramework.coerce(sentinel) is not AssertionFramework.ASSERTJ:
            raise AssertionError(f"sentinel {sentinel!r} must default to ASSERTJ")


def case_coerce_unknown_fails_strictly() -> None:
    try:
        AssertionFramework.coerce("mockito")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown dialect must raise ValueError, not degrade silently")
    try:
        AssertionFramework.coerce(123)  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        raise AssertionError("a non-str, non-enum value must raise TypeError")


def case_assertions_owner_precedence() -> None:
    if assertions_owner_for("assertj") != OWNER_ASSERTJ:
        raise AssertionError("AssertJ stack must own the AssertJ Assertions class")
    if assertions_owner_for("junit-builtin") != OWNER_JUNIT5_ASSERT:
        raise AssertionError("junit-builtin stack must own the JUnit Assertions class")
    if assertions_owner_for("hamcrest") != OWNER_JUNIT5_ASSERT:
        raise AssertionError("hamcrest has no Assertions type → must fall back to JUnit")
    if assertions_owner_for("junit-builtin", "junit4") != OWNER_JUNIT4_ASSERT:
        raise AssertionError("junit4 stack must resolve to the legacy org.junit.Assert")


# ── 2. resolve_imports — never emits both Assertions ───────────────────────────

def case_resolve_single_dialect_is_method_accurate() -> None:
    # Regression: a body using ONLY JUnit's qualified Assertions must still get the
    # JUnit class, even under an AssertJ stack (no spurious dialect switch).
    types, _ = resolve_imports(
        _file("        Assertions.assertEquals(1, r);", ""), "junit5", "assertj"
    )
    if types != [OWNER_JUNIT5_ASSERT]:
        raise AssertionError(f"single JUnit dialect must yield only JUnit Assertions: {types}")


def case_resolve_mixed_dialects_obeys_config() -> None:
    body = (
        "        Assertions.assertThat(r).isEqualTo(1);\n"
        "        Assertions.assertEquals(1, r);"
    )
    types_aj, _ = resolve_imports(_file(body, ""), "junit5", "assertj")
    if OWNER_ASSERTJ not in types_aj or OWNER_JUNIT5_ASSERT in types_aj:
        raise AssertionError(f"assertj config must keep only AssertJ Assertions: {types_aj}")
    types_jb, _ = resolve_imports(_file(body, ""), "junit5", "junit-builtin")
    if OWNER_JUNIT5_ASSERT not in types_jb or OWNER_ASSERTJ in types_jb:
        raise AssertionError(f"junit-builtin config must keep only JUnit Assertions: {types_jb}")


def case_static_registry_is_single_owner_per_helper() -> None:
    reg_aj = static_helper_registry("junit5", "assertj")
    if reg_aj["assertThat"] != OWNER_ASSERTJ:
        raise AssertionError("assertj registry must own assertThat via AssertJ")
    if reg_aj["assertEquals"] != OWNER_JUNIT5_ASSERT:
        raise AssertionError("JUnit asserts always resolve to the JUnit owner")
    reg_h = static_helper_registry("junit5", "hamcrest")
    if reg_h["assertThat"] != OWNER_HAMCREST:
        raise AssertionError("hamcrest registry must own assertThat via MatcherAssert")
    if "assertThatThrownBy" in reg_h:
        raise AssertionError("hamcrest must not pull AssertJ-only helpers")
    reg_jb = static_helper_registry("junit5", "junit-builtin")
    if "assertThat" in reg_jb:
        raise AssertionError("junit-builtin has no assertThat static helper")


# ── 3. _dedup_imports_by_simple_name — collision / precedence / FQN fallback ───

_DUAL_IMPORTS = (
    "import org.junit.jupiter.api.Assertions;\n"
    "import org.assertj.core.api.Assertions;\n"
)
_DUAL_BODY = (
    "        // when\n"
    "        int r = 1;\n"
    "        // then\n"
    "        Assertions.assertEquals(1, r);\n"
    "        Assertions.assertThat(r).isEqualTo(1);"
)


def case_dedup_assertj_precedence_inlines_junit_fqn() -> None:
    out = _dedup_imports_by_simple_name(_file(_DUAL_BODY, _DUAL_IMPORTS), "assertj")
    if "import org.junit.jupiter.api.Assertions;" in out:
        raise AssertionError("AssertJ winner: the JUnit Assertions import must be dropped")
    if "import org.assertj.core.api.Assertions;" not in out:
        raise AssertionError("AssertJ winner: the AssertJ Assertions import must be kept")
    if "org.junit.jupiter.api.Assertions.assertEquals(1, r)" not in out:
        raise AssertionError("the losing JUnit call must be rewritten to its FQN")
    if "        Assertions.assertThat(r)" not in out:
        raise AssertionError("the winning AssertJ call must stay bare")


def case_dedup_junit_precedence_inlines_assertj_fqn() -> None:
    out = _dedup_imports_by_simple_name(_file(_DUAL_BODY, _DUAL_IMPORTS), "junit-builtin")
    if "import org.assertj.core.api.Assertions;" in out:
        raise AssertionError("JUnit winner: the AssertJ Assertions import must be dropped")
    if "import org.junit.jupiter.api.Assertions;" not in out:
        raise AssertionError("JUnit winner: the JUnit Assertions import must be kept")
    if "org.assertj.core.api.Assertions.assertThat(r)" not in out:
        raise AssertionError("the losing AssertJ call must be rewritten to its FQN")
    if "        Assertions.assertEquals(1, r)" not in out:
        raise AssertionError("the winning JUnit call must stay bare")


def case_dedup_is_idempotent() -> None:
    once = _dedup_imports_by_simple_name(_file(_DUAL_BODY, _DUAL_IMPORTS), "assertj")
    twice = _dedup_imports_by_simple_name(once, "assertj")
    if once != twice:
        raise AssertionError("a second de-dup pass must be a no-op")


def case_dedup_no_collision_is_unchanged() -> None:
    text = _file("        Assertions.assertEquals(1, 1);",
                 "import org.junit.jupiter.api.Assertions;\n")
    if _dedup_imports_by_simple_name(text, "assertj") != text:
        raise AssertionError("a single Assertions import must pass through untouched")


def case_dedup_collapses_exact_duplicate_imports() -> None:
    text = _file(
        "        assertThat(1).isEqualTo(1);",
        "import org.junit.jupiter.api.Test;\n"  # exact duplicate of the seeded import
        "import static org.assertj.core.api.Assertions.assertThat;\n",
    )
    out = _dedup_imports_by_simple_name(text, "assertj")
    if out.count("import org.junit.jupiter.api.Test;") != 1:
        raise AssertionError("exact-duplicate import lines must collapse to one")


def case_dedup_ignores_comments_and_strings() -> None:
    body = (
        "        Assertions.assertThat(r).isEqualTo(1);\n"
        "        // see Assertions.assertEquals for the legacy path\n"
        '        String hint = "use Assertions.assertEquals here";'
    )
    out = _dedup_imports_by_simple_name(_file(body, _DUAL_IMPORTS), "assertj")
    # assertEquals appears ONLY in a comment/string → it is not a real JUnit use,
    # so nothing is FQN-rewritten and the comment/string text is preserved verbatim.
    if "org.junit.jupiter.api.Assertions.assertEquals" in out:
        raise AssertionError("a commented/quoted Assertions.assertEquals must never be rewritten")
    if "// see Assertions.assertEquals for the legacy path" not in out:
        raise AssertionError("the comment text must be preserved verbatim")
    if '"use Assertions.assertEquals here"' not in out:
        raise AssertionError("the string literal must be preserved verbatim")
    # The unused JUnit Assertions import is still dropped (AssertJ is the winner).
    if "import org.junit.jupiter.api.Assertions;" in out:
        raise AssertionError("the unused colliding JUnit import must still be dropped")


def case_end_to_end_llm_double_import_resolved() -> None:
    # Simulate the real failure path: the LLM declared BOTH Assertions FQCNs in
    # allowedImports, so the applier injected both before the de-dup pass runs.
    tpl = _load_template("junit5-mockito", _TEMPLATES_DIR)
    rendered = _render_from_template(tpl, {
        "sut": "com.acme.Calc",
        "testPackage": "com.acme",
        "fields": [],
        "methods": [{
            "name": "should_add_when_two_ints",
            "annotations": ["@Test"],
            "body": "// given\nint a = 2;\n// when\nint r = sut.add(a, 3);\n"
                    "// then\nAssertions.assertEquals(5, r);\nAssertions.assertThat(r).isEqualTo(5);",
        }],
    }, stack={"testFramework": "junit5", "assertFramework": "assertj"})
    injected = _inject_imports(
        rendered,
        ["org.junit.jupiter.api.Assertions", "org.assertj.core.api.Assertions"],
    )
    final = _dedup_imports_by_simple_name(injected, "assertj")
    if final.count("api.Assertions;") != 1:
        raise AssertionError(f"exactly one Assertions import must survive:\n{final}")
    if "import org.assertj.core.api.Assertions;" not in final:
        raise AssertionError("AssertJ Assertions must be the surviving import")
    if "org.junit.jupiter.api.Assertions.assertEquals(5, r)" not in final:
        raise AssertionError("the JUnit call must be FQN-rewritten so it still compiles")


# ── 4. _render_from_template — clean output ────────────────────────────────────

def _render(stack: dict | None) -> str:
    tpl = _load_template("junit5-mockito", _TEMPLATES_DIR)
    return _render_from_template(tpl, {
        "sut": "com.acme.FooService",
        "testPackage": "com.acme",
        "fields": [],
        "methods": [],
    }, stack=stack)


def case_render_resolves_assert_imports_exclusively() -> None:
    out = _render({"assertFramework": "junit-builtin"})
    if "${ASSERT_IMPORTS}" in out:
        raise AssertionError("${ASSERT_IMPORTS} must be resolved, never left literal")
    if "assertj" in out:
        raise AssertionError("junit-builtin render must not seed any AssertJ import")
    # The exclusive dialect helper agrees with what the template emits.
    if _assert_imports_for("junit-builtin") != "":
        raise AssertionError("junit-builtin must seed no assert import (resolver adds on use)")


def case_render_has_no_redundant_blank_lines() -> None:
    # junit-builtin collapses ${ASSERT_IMPORTS} to "" — that must not leave a
    # double blank line (a SonarQube / IDE smell) in the import block.
    out = _render({"assertFramework": "junit-builtin"})
    if "\n\n\n" in out:
        raise AssertionError("rendered template must not contain 3+ consecutive newlines")


def case_render_passes_placeholder_guard() -> None:
    # A normal render must not raise (all known placeholders substituted), and a
    # doc-comment listing placeholders must stay inert.
    _render({"assertFramework": "assertj"})
    _assert_no_unresolved_placeholders(
        "// Placeholders: ${PACKAGE}, ${TEST_BODY}\nclass T {}"
    )


def case_render_guard_flags_code_leftover() -> None:
    try:
        _assert_no_unresolved_placeholders("class T { Object x = build(${TEST_BODY}); }")
    except ValueError:
        pass
    else:
        raise AssertionError("a placeholder surviving in code must raise loudly")


def main() -> int:
    cases = [
        ("coerce-canonical-and-aliases",            case_coerce_canonical_and_aliases),
        ("coerce-sentinels-default-assertj",        case_coerce_sentinels_default_to_assertj),
        ("coerce-unknown-fails-strictly",           case_coerce_unknown_fails_strictly),
        ("assertions-owner-precedence",             case_assertions_owner_precedence),
        ("resolve-single-dialect-method-accurate",  case_resolve_single_dialect_is_method_accurate),
        ("resolve-mixed-dialects-obeys-config",     case_resolve_mixed_dialects_obeys_config),
        ("static-registry-single-owner",            case_static_registry_is_single_owner_per_helper),
        ("dedup-assertj-precedence-fqn",            case_dedup_assertj_precedence_inlines_junit_fqn),
        ("dedup-junit-precedence-fqn",              case_dedup_junit_precedence_inlines_assertj_fqn),
        ("dedup-idempotent",                        case_dedup_is_idempotent),
        ("dedup-no-collision-unchanged",            case_dedup_no_collision_is_unchanged),
        ("dedup-collapses-duplicate-imports",       case_dedup_collapses_exact_duplicate_imports),
        ("dedup-ignores-comments-and-strings",      case_dedup_ignores_comments_and_strings),
        ("end-to-end-llm-double-import",            case_end_to_end_llm_double_import_resolved),
        ("render-assert-imports-exclusive",         case_render_resolves_assert_imports_exclusively),
        ("render-no-redundant-blank-lines",         case_render_has_no_redundant_blank_lines),
        ("render-passes-placeholder-guard",         case_render_passes_placeholder_guard),
        ("render-guard-flags-code-leftover",        case_render_guard_flags_code_leftover),
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
    print("\nAll exclusive-assert-framework cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
