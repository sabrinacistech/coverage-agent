"""test_whitelist_loading.py — exercise _authorized_imports_from_whitelist().

Runs as a plain script: `python tools/python/tests/test_whitelist_loading.py`.
No external test runner required. Exits non-zero on any failure.

Cases:
  1. Schema-conformant: packages/classes are arrays of dicts ({name}/{fqcn}).
  2. Legacy: packages/classes are plain strings.
  3. Mixed: dicts and strings coexist in the same list.
  4. Empty / missing keys: yields an empty set, no exception.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from test_patch_applier import _authorized_imports_from_whitelist  # noqa: E402


def _eq(label: str, got: set[str], want: set[str]) -> None:
    if got != want:
        raise AssertionError(f"{label}: got={sorted(got)} want={sorted(want)}")


def case_schema_conformant() -> None:
    wl = {
        "schemaVersion": 1,
        "module": "core",
        "packages": [
            {"name": "java.util", "origin": "jdk"},
            {"name": "org.junit.jupiter.api", "origin": "dep"},
        ],
        "classes": [
            {"fqcn": "java.lang.String", "origin": "jdk"},
            {"fqcn": "org.assertj.core.api.Assertions", "origin": "dep"},
        ],
    }
    _eq(
        "schema-conformant",
        _authorized_imports_from_whitelist(wl),
        {
            "java.util",
            "org.junit.jupiter.api",
            "java.lang.String",
            "org.assertj.core.api.Assertions",
        },
    )


def case_legacy_strings() -> None:
    wl = {
        "schemaVersion": 1,
        "module": "core",
        "packages": ["java.util", "org.junit.jupiter.api"],
        "classes": ["java.lang.String", "org.assertj.core.api.Assertions"],
    }
    _eq(
        "legacy-strings",
        _authorized_imports_from_whitelist(wl),
        {
            "java.util",
            "org.junit.jupiter.api",
            "java.lang.String",
            "org.assertj.core.api.Assertions",
        },
    )


def case_mixed() -> None:
    wl = {
        "packages": [
            {"name": "java.util", "origin": "jdk"},
            "org.junit.jupiter.api",
            {"origin": "dep"},
            42,
        ],
        "classes": [
            "java.lang.String",
            {"fqcn": "com.acme.Foo", "origin": "source"},
            None,
        ],
    }
    _eq(
        "mixed",
        _authorized_imports_from_whitelist(wl),
        {
            "java.util",
            "org.junit.jupiter.api",
            "java.lang.String",
            "com.acme.Foo",
        },
    )


def case_empty() -> None:
    _eq("empty-dict", _authorized_imports_from_whitelist({}), set())
    _eq(
        "explicit-empty",
        _authorized_imports_from_whitelist({"packages": [], "classes": []}),
        set(),
    )
    _eq(
        "null-fields",
        _authorized_imports_from_whitelist({"packages": None, "classes": None}),
        set(),
    )


def main() -> int:
    cases = [
        ("schema-conformant", case_schema_conformant),
        ("legacy-strings", case_legacy_strings),
        ("mixed", case_mixed),
        ("empty", case_empty),
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
    print("\nAll whitelist-loading cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
