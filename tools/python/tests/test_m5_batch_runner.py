"""test_m5_batch_runner.py — regression for the M5 batching fix.

M5 (narrow_test_runner): several test classes can run in ONE Maven invocation
    (`-Dtest=FooTest,BarTest`) so N classes pay a single Maven/JVM startup
    instead of N — amortising the cold-JVM-per-cycle cost. Covers the two pure
    helpers that build the command deterministically.

Run: `python tools/python/tests/test_m5_batch_runner.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from narrow_test_runner import _build_test_command, _collect_simple_names  # noqa: E402


# ── _collect_simple_names ─────────────────────────────────────────────────────

def case_single_fqcn_to_simple() -> None:
    if _collect_simple_names(["com.acme.FooTest"]) != ["FooTest"]:
        raise AssertionError("single FQCN should reduce to its simple name")


def case_repeated_flags_batched() -> None:
    got = _collect_simple_names(["com.acme.FooTest", "com.acme.sub.BarTest"])
    if got != ["FooTest", "BarTest"]:
        raise AssertionError(f"repeated --test-class should batch in order: {got}")


def case_comma_separated_in_one_value() -> None:
    got = _collect_simple_names(["com.acme.FooTest,com.acme.BarTest"])
    if got != ["FooTest", "BarTest"]:
        raise AssertionError(f"comma-separated value should split: {got}")


def case_dedup_and_order_preserved() -> None:
    got = _collect_simple_names(["com.acme.FooTest", "com.other.FooTest", " BarTest "])
    if got != ["FooTest", "BarTest"]:
        raise AssertionError(f"dedup (by simple name) + trim, order-preserving: {got}")


def case_empty_input() -> None:
    if _collect_simple_names([]) != [] or _collect_simple_names(["", " , "]) != []:
        raise AssertionError("empty/blank input should yield no names")


# ── _build_test_command ───────────────────────────────────────────────────────

def case_command_batches_dtest() -> None:
    jacoco = Path("/repo/target/jacoco-narrow.exec")
    cmd = _build_test_command("mvn", ["FooTest", "BarTest"], jacoco, "core")
    joined = " ".join(cmd)
    if "-Dtest=FooTest,BarTest" not in joined:
        raise AssertionError(f"batch -Dtest not built: {cmd}")
    for needed in ("-o", "-DfailIfNoTests=false", "test", "-pl", "core", "-am"):
        if needed not in cmd:
            raise AssertionError(f"missing required token {needed!r}: {cmd}")
    if f"-Djacoco.destFile={jacoco}" not in cmd:
        raise AssertionError(f"jacoco destFile missing: {cmd}")
    if "clean" in cmd or "install" in cmd or "verify" in cmd:
        raise AssertionError(f"narrow runner must never clean/install/verify: {cmd}")


def case_command_omits_module_flags_when_root() -> None:
    cmd = _build_test_command("mvnd", ["FooTest"], Path("/repo/target/j.exec"), None)
    if "-pl" in cmd or "-am" in cmd:
        raise AssertionError(f"no -pl/-am when module is None: {cmd}")
    if cmd[0] != "mvnd":
        raise AssertionError("tool must be the first token")


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    cases = [
        ("single-fqcn-to-simple",            case_single_fqcn_to_simple),
        ("repeated-flags-batched",           case_repeated_flags_batched),
        ("comma-separated-in-one-value",     case_comma_separated_in_one_value),
        ("dedup-and-order-preserved",        case_dedup_and_order_preserved),
        ("empty-input",                      case_empty_input),
        ("command-batches-dtest",            case_command_batches_dtest),
        ("command-omits-module-flags-root",  case_command_omits_module_flags_when_root),
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
    print("\nAll M5 cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
