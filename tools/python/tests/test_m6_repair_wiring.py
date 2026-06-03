"""test_m6_repair_wiring.py — regression lock for the M6 reconnection.

Problem #2 / audit H-2: repair_dispatch.py (the deterministic repair driver)
was built but never invoked from the only code path that writes Java, so a
fixable lint violation went straight to a rollback. M6 reconnects it: the
patcher's post-write G6 now runs evaluate_gates(auto_repair=True), which drives
gate_g6 → _try_auto_repair → repair_dispatch.py → re-lint.

These are structural/tripwire checks (no Maven): they assert the wiring is
present end to end so it cannot silently regress again — a full repair is
exercised when the real linter/Maven run.

Run: `python tools/python/tests/test_m6_repair_wiring.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import gate_runner  # noqa: E402
import test_patch_applier as tpa  # noqa: E402


def case_patcher_post_write_requests_auto_repair() -> None:
    src = inspect.getsource(tpa.main)
    if "auto_repair=True" not in src:
        raise AssertionError(
            "patcher post-write G6 must call evaluate_gates(auto_repair=True) "
            "(M6 reconnection of repair_dispatch)"
        )
    if "evaluate_gates" not in src:
        raise AssertionError("patcher must drive the post-write check through evaluate_gates")


def case_evaluate_gates_drives_repair_dispatch() -> None:
    src = inspect.getsource(gate_runner.evaluate_gates)
    if "auto_repair" not in src or "_try_auto_repair(" not in src:
        raise AssertionError(
            "evaluate_gates must invoke _try_auto_repair when auto_repair is set"
        )
    disp = inspect.getsource(gate_runner._try_auto_repair)
    if "repair_dispatch.py" not in disp:
        raise AssertionError("_try_auto_repair must invoke repair_dispatch.py")


def case_repair_dispatch_present() -> None:
    p = Path(gate_runner.__file__).resolve().parent / "repair_dispatch.py"
    if not p.exists():
        raise AssertionError("repair_dispatch.py (the deterministic repair driver) must exist")


def main() -> int:
    cases = [
        ("patcher-post-write-requests-auto-repair", case_patcher_post_write_requests_auto_repair),
        ("evaluate-gates-drives-repair-dispatch",   case_evaluate_gates_drives_repair_dispatch),
        ("repair-dispatch-present",                 case_repair_dispatch_present),
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
    print("\nAll M6 cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
