"""cycle_loop.py — the single deterministic owner of the generation/repair loop.

Closes audit C1/C2 (2026-05-29). Two "by-construction" guarantees were actually
by-convention, because the only code that ticked the cycle counter and wrote the
G8 signals was never wired into an executable path:

  C2  budget_enforcer enforced maxCycles/maxMinutesPerCycle, but nothing in the
      real run-path ticked the cycle counter, so `cycle` stayed 0 and neither the
      loop budget nor the test_patch_applier backstop ever tripped.
  C1  gate_runner.gate_g8 reads `consecutiveZeroDeltaCycles` and
      `compileFailRateWindow`, but NO deterministic code wrote those fields, so
      the finiteness gate could never fire unless the LLM populated them.

This module is now the ONE sanctioned way to run a cycle. The previous
`cycle_runner.py` — which ticked the budget but never wrote the G8 fields nor
evaluated G8 — was removed so there is a single owner with no competing,
weaker path. It ticks the budget AND writes the G8 signals AND evaluates G8, so
finiteness holds regardless of who drives generation. Each cycle:

  1. tick           — budget_enforcer increments `cycle` (1-based) + stamps start.
  2. budget check   — abort (rc 2) if this cycle exceeds maxCycles / minutes.
  2b token check    — abort (rc 2) if any SUT context pack exceeds maxTokensIn
                      (llm-budget.json). Runs before dispatch so an over-budget
                      pack never reaches the LLM. This is the cost/token half of
                      the Fundamental Rule, previously built-but-disconnected.
  3. run command    — the per-cycle work (generation + patch + validation); it is
                      expected to (re)write coverage-delta.json for the cycle.
  4. record outcome — write the TWO fields G8 reads, derived deterministically:
                        - consecutiveZeroDeltaCycles: +1 if the cycle made no
                          line/branch progress (or produced no coverage-delta),
                          else reset to 0.
                        - compileFailRateWindow: append 1.0 if the command failed
                          to compile/pass, else 0.0 (bounded window).
  5. evaluate G8    — reuse gate_runner.gate_g8; stop (rc 5) on a stall.
  6. reset          — clear cycleStartedAt for the next cycle's elapsed check.

The loop stops on: budget exceeded (2), G8 stall (5), the command signalling DONE
via --done-exit-code (0), or an absolute safety cap (0). A *failing* cycle
command does not stop the loop by itself — that is the repair loop, bounded by
budget + G7/G8, not by instruction.

Thresholds and budget live in gate_runner / budget_enforcer respectively; this
file re-implements neither (audit H2/A5 — there must be ONE definition of each).

Usage
-----
  python tools/python/cycle_loop.py \\
      --state     ../.agent-state/execution-state.json \\
      --state-dir ../.agent-state \\
      -- python tools/python/run_one_cycle.py ...   # the per-cycle command
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import budget_enforcer  # noqa: E402  (tick/check/reset + atomic state I/O)
from common import _TimedRun, atomic_write_json  # noqa: E402
from gate_runner import gate_g8  # noqa: E402  (single source of G8 thresholds)

# Exit codes (distinct so callers/tests can tell WHY the loop stopped).
RC_DONE = 0
RC_BUDGET_EXCEEDED = 2
RC_STATE_MALFORMED = 3
RC_CONVERGENCE_STALL = 5

_COMPILE_FAIL_WINDOW = 4   # how many recent cycles gate_g8 can see; it reads [-1].
_ABSOLUTE_SAFETY_CAP = 1000  # defends against a malformed/unbounded budget.


def _read_cycle_delta(state_dir: Path) -> tuple[int, int] | None:
    """Return (linesDelta, branchesDelta) from coverage-delta.json, or None if it
    is absent/unreadable. Shape is produced by jacoco_parser.py --mode delta:
    ``{"totals": {"lines": {"delta": N}, "branches": {"delta": M}}}``.
    """
    path = state_dir / "coverage-delta.json"
    if not path.exists():
        return None
    try:
        import json
        totals = (json.loads(path.read_text(encoding="utf-8")) or {}).get("totals", {}) or {}
        lines = int((totals.get("lines", {}) or {}).get("delta", 0) or 0)
        branches = int((totals.get("branches", {}) or {}).get("delta", 0) or 0)
        return lines, branches
    except Exception:
        return None


def record_outcome(
    state_path: Path,
    *,
    zero_delta: bool,
    compile_failed: bool,
    window_size: int = _COMPILE_FAIL_WINDOW,
) -> dict:
    """Write the two fields gate_g8 reads. Atomic (reuses budget_enforcer's loader
    and common.atomic_write_json so all execution-state writes share one path).
    """
    state = budget_enforcer._load(state_path)
    prev = int(state.get("consecutiveZeroDeltaCycles", 0) or 0)
    state["consecutiveZeroDeltaCycles"] = prev + 1 if zero_delta else 0
    window = list(state.get("compileFailRateWindow") or [])
    window.append(1.0 if compile_failed else 0.0)
    state["compileFailRateWindow"] = window[-window_size:]
    atomic_write_json(state_path, state)
    return state


def run_loop(
    state_path: Path,
    state_dir: Path,
    command: list[str],
    done_exit_code: int,
    max_iterations: int | None,
) -> int:
    cap = min(max_iterations or _ABSOLUTE_SAFETY_CAP, _ABSOLUTE_SAFETY_CAP)
    iterations = 0
    while iterations < cap:
        iterations += 1

        trc, _ = budget_enforcer.tick(state_path)
        if trc != 0:
            budget_enforcer.reset(state_path)
            print(f"[FAIL] budget tick failed (rc={trc}); state malformed.", file=sys.stderr)
            return RC_STATE_MALFORMED

        crc, payload = budget_enforcer.check(state_path)
        if crc != 0:
            budget_enforcer.reset(state_path)
            print(f"[STOP] budget exceeded: {payload.get('reason')} "
                  f"(cycle={payload.get('cycle')}).", file=sys.stderr)
            return RC_BUDGET_EXCEEDED

        # Cost/token half of the budget: refuse to dispatch when any SUT's
        # context pack exceeds its input-token ceiling (llm-budget.json). This
        # gate runs BEFORE the cycle command so an over-budget pack never
        # reaches the LLM and no Java is written.
        tcrc, tpayload = budget_enforcer.check_token_budget(state_dir)
        if tcrc != 0:
            budget_enforcer.reset(state_path)
            print(f"[STOP] token budget exceeded: {tpayload.get('count')} SUT(s) "
                  f"over maxTokensIn: {tpayload.get('overBudgetSuts')}.", file=sys.stderr)
            return RC_BUDGET_EXCEEDED

        cmd_rc = subprocess.run(command, check=False).returncode

        delta = _read_cycle_delta(state_dir)
        zero_delta = (delta is None) or (delta[0] == 0 and delta[1] == 0)
        compile_failed = cmd_rc not in (0, done_exit_code)
        record_outcome(state_path, zero_delta=zero_delta, compile_failed=compile_failed)

        budget_enforcer.reset(state_path)

        g8 = gate_g8(state_dir)
        if g8.get("status") == "FAIL":
            print(f"[STOP] G8 convergence gate: {g8.get('blockedReason')} {g8}", file=sys.stderr)
            return RC_CONVERGENCE_STALL

        if cmd_rc == done_exit_code:
            print(f"[DONE] cycle command signalled completion (rc={done_exit_code}) "
                  f"after {iterations} cycle(s).")
            return RC_DONE

    print(f"[STOP] absolute safety cap reached ({cap} iterations).", file=sys.stderr)
    return RC_DONE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Own the cycle loop with budget + G8 enforced by construction.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--state", required=True, type=Path,
                    help="Path to execution-state.json (budget + G8 fields).")
    ap.add_argument("--state-dir", required=True, type=Path,
                    help="State directory holding coverage-delta.json (read each cycle).")
    ap.add_argument("--done-exit-code", type=int, default=7,
                    help="Exit code the cycle command uses to signal 'no more targets'. Default 7.")
    ap.add_argument("--max-iterations", type=int, default=None,
                    help="Optional hard cap on iterations (budget normally bounds the loop).")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="The per-cycle command. Prefix with `--`.")
    args = ap.parse_args(argv)

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("[FAIL] no per-cycle command supplied after --", file=sys.stderr)
        return RC_BUDGET_EXCEEDED

    return run_loop(
        args.state.resolve(),
        args.state_dir.resolve(),
        cmd,
        args.done_exit_code,
        args.max_iterations,
    )


if __name__ == "__main__":
    with _TimedRun("cycle_loop") as _tr:
        _rc = main()
        if _rc not in (RC_DONE,):
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
