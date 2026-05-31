"""test_cycle_loop.py — C1/C2 fix: finiteness by construction.

Proves the two things the audit found broken:
  C1  cycle_loop.record_outcome writes the exact fields gate_g8 reads
      (consecutiveZeroDeltaCycles, compileFailRateWindow), so G8 can fire
      without the LLM populating state.
  C2  the loop actually halts — on budget (maxCycles), on a G8 stall, and on
      the cycle command's DONE signal — incrementing the cycle counter so the
      patcher's budget check is meaningful.

Run: `python tools/python/tests/test_cycle_loop.py`  (exits non-zero on failure)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cycle_loop  # noqa: E402
from gate_runner import gate_g8  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ OK ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        FAILURES.append(label)


def _seed(sd: Path, max_cycles: int) -> Path:
    sp = sd / "execution-state.json"
    sp.write_text(
        json.dumps({"schemaVersion": 1, "cycle": 0, "phase": "generation",
                    "budget": {"maxCycles": max_cycles, "maxMinutesPerCycle": 10}}),
        encoding="utf-8",
    )
    return sp


def _delta_cmd(sd: Path, lines: int, branches: int) -> list[str]:
    """A cycle command that writes coverage-delta.json with the given deltas."""
    code = (
        "import json,sys,pathlib;"
        "p=pathlib.Path(sys.argv[1])/'coverage-delta.json';"
        "p.write_text(json.dumps({'schemaVersion':1,'cycle':0,'mode':'coverage',"
        "'totals':{'lines':{'before':0,'after':%d,'delta':%d},"
        "'branches':{'before':0,'after':%d,'delta':%d}}}))"
        % (lines, lines, branches, branches)
    )
    return [sys.executable, "-c", code, str(sd)]


def case_g8_fields_written() -> None:
    print("== C1: record_outcome writes the fields gate_g8 reads ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td); sp = _seed(sd, 10)

        cycle_loop.record_outcome(sp, zero_delta=True, compile_failed=False)
        st = json.loads(sp.read_text(encoding="utf-8"))
        _assert("zero-delta cycle 1 → consecutiveZeroDeltaCycles=1",
                st.get("consecutiveZeroDeltaCycles") == 1, str(st))
        _assert("gate_g8 PASS after 1 stall", gate_g8(sd).get("status") == "PASS")

        cycle_loop.record_outcome(sp, zero_delta=True, compile_failed=False)
        st = json.loads(sp.read_text(encoding="utf-8"))
        _assert("zero-delta cycle 2 → consecutiveZeroDeltaCycles=2",
                st.get("consecutiveZeroDeltaCycles") == 2, str(st))
        g8 = gate_g8(sd)
        _assert("gate_g8 FAIL G8_NO_DELTA after 2 stalls",
                g8.get("status") == "FAIL" and g8.get("blockedReason") == "G8_NO_DELTA", str(g8))

        # Real progress must reset the stall counter.
        cycle_loop.record_outcome(sp, zero_delta=False, compile_failed=False)
        st = json.loads(sp.read_text(encoding="utf-8"))
        _assert("progress resets consecutiveZeroDeltaCycles=0",
                st.get("consecutiveZeroDeltaCycles") == 0, str(st))
        _assert("gate_g8 PASS again after progress", gate_g8(sd).get("status") == "PASS")


def case_compile_fail_rate() -> None:
    print("== C1: compile failure feeds compileFailRateWindow → gate_g8 ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td); sp = _seed(sd, 10)
        cycle_loop.record_outcome(sp, zero_delta=False, compile_failed=True)
        st = json.loads(sp.read_text(encoding="utf-8"))
        _assert("compileFailRateWindow[-1] == 1.0",
                (st.get("compileFailRateWindow") or [None])[-1] == 1.0, str(st))
        g8 = gate_g8(sd)
        _assert("gate_g8 FAIL G8_COMPILE_FAIL_RATE",
                g8.get("status") == "FAIL" and g8.get("blockedReason") == "G8_COMPILE_FAIL_RATE", str(g8))


def case_loop_halts_on_budget() -> None:
    print("== C2: loop runs exactly maxCycles then refuses the next ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td); sp = _seed(sd, 2)
        # Progress every cycle (delta 5/0) so G8 never trips → only budget stops it.
        rc = cycle_loop.run_loop(sp, sd, _delta_cmd(sd, 5, 0), done_exit_code=7, max_iterations=None)
        st = json.loads(sp.read_text(encoding="utf-8"))
        _assert("loop returns RC_BUDGET_EXCEEDED",
                rc == cycle_loop.RC_BUDGET_EXCEEDED, f"rc={rc}")
        _assert("ran 2 cycles, blocked the 3rd (cycle==3 at exit)",
                st.get("cycle") == 3, f"cycle={st.get('cycle')}")


def case_loop_halts_on_g8_stall() -> None:
    print("== C2: loop stops on G8 stall before exhausting maxCycles ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td); sp = _seed(sd, 10)
        # Zero delta every cycle → G8 stall after 2 cycles, well before maxCycles=10.
        rc = cycle_loop.run_loop(sp, sd, _delta_cmd(sd, 0, 0), done_exit_code=7, max_iterations=None)
        st = json.loads(sp.read_text(encoding="utf-8"))
        _assert("loop returns RC_CONVERGENCE_STALL",
                rc == cycle_loop.RC_CONVERGENCE_STALL, f"rc={rc}")
        _assert("stopped at cycle 2 (not 10)", st.get("cycle") == 2, f"cycle={st.get('cycle')}")


def case_loop_done_signal() -> None:
    print("== C2: loop stops cleanly when the cycle command signals DONE ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td); sp = _seed(sd, 10)
        done_cmd = [sys.executable, "-c", "import sys; sys.exit(7)"]
        rc = cycle_loop.run_loop(sp, sd, done_cmd, done_exit_code=7, max_iterations=None)
        st = json.loads(sp.read_text(encoding="utf-8"))
        _assert("loop returns RC_DONE", rc == cycle_loop.RC_DONE, f"rc={rc}")
        _assert("stopped after 1 cycle", st.get("cycle") == 1, f"cycle={st.get('cycle')}")


def main() -> int:
    case_g8_fields_written()
    case_compile_fail_rate()
    case_loop_halts_on_budget()
    case_loop_halts_on_g8_stall()
    case_loop_done_signal()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} case(s): {FAILURES}")
        return 1
    print("All cycle_loop cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
