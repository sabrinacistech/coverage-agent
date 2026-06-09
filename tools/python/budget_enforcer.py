"""budget_enforcer.py — runtime enforcement of execution-state budget.

`execution-state.json` declares `budget.maxCycles` and `budget.maxMinutesPerCycle`
but until now no tool checked them at runtime. This module closes that gap so
runaway cycles abort by construction, not by LLM convention.

Subcommands
-----------
  check        → verify current state is within cycle/minute budget; exits 0 or 2
  check-tokens → verify no SUT pack exceeds its input-token ceiling; exits 0 or 2
  tick         → increment cycle counter and stamp cycleStartedAt
  reset        → clear cycleStartedAt (used at end of cycle)
  pause        → freeze the per-cycle minute clock while the loop waits on a
                 MANUAL handoff (Claude Code generating JSON, the user pressing
                 ENTER). Stamps cyclePausedAt.
  resume       → unfreeze: shift cycleStartedAt forward by the paused span so the
                 human wait never counts against maxMinutesPerCycle. Clears
                 cyclePausedAt.

The minute budget must measure the runner's AUTOMATIC work (target selection,
request/response I/O, patch application, test runs, error analysis), never human
time. Without pause/resume the interactive IDE handoff blocks inside a cycle with
cycleStartedAt already stamped, so the test_patch_applier backstop trips
BUDGET_EXCEEDED while the user is still thinking. `pause`/`resume` (and the
`paused(...)` context manager) close that gap: the wait is wrapped so only
automatic work accrues against the budget.

The cost/token half of the budget lives in state/_summaries/llm-budget.json,
which context_pack_builder.py already writes (estimatedTokensIn vs maxTokensIn
per SUT). Until now that file only produced a `[WARN]`; nothing consumed it to
block, so the cost/token budget was built but disconnected. `check-tokens` is
that missing consumer — it turns the warning into a blocking gate so an
over-budget pack never reaches the LLM. cycle_loop calls it before dispatch.

Usage
-----
  python tools/python/budget_enforcer.py check        --state state/execution-state.json
  python tools/python/budget_enforcer.py check-tokens --state-dir state/
  python tools/python/budget_enforcer.py tick         --state state/execution-state.json
  python tools/python/budget_enforcer.py reset        --state state/execution-state.json

Exit codes
----------
  0  within budget
  2  budget exceeded (caller MUST abort current cycle)
  3  malformed state (missing required fields)
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import atomic_write_json, emit_tool_summary  # noqa: E402

EXIT_OK = 0
EXIT_EXCEEDED = 2
EXIT_MALFORMED = 3

DEFAULT_MAX_CYCLES = 20
DEFAULT_MAX_MINUTES_PER_CYCLE = 10


def _load(state_path: Path) -> dict:
    if not state_path.exists():
        return {"schemaVersion": 1, "cycle": 0, "budget": {}}
    return json.loads(state_path.read_text(encoding="utf-8"))


def check(state_path: Path) -> tuple[int, dict]:
    state = _load(state_path)
    budget = state.get("budget", {}) or {}
    cycle = int(state.get("cycle", 0))
    max_cycles = int(budget.get("maxCycles", DEFAULT_MAX_CYCLES))
    max_minutes = float(budget.get("maxMinutesPerCycle", DEFAULT_MAX_MINUTES_PER_CYCLE))

    # `cycle` is the 1-based number of the cycle currently in progress: cycle_loop
    # ticks it at cycle entry (tick BEFORE check), and test_patch_applier reads it
    # mid-cycle. Blocking on strictly-greater-than means cycles 1..maxCycles run and
    # the (maxCycles+1)th is refused — no off-by-one provided callers tick first.
    if cycle > max_cycles:
        return EXIT_EXCEEDED, {
            "ok": False, "reason": "maxCycles", "cycle": cycle, "maxCycles": max_cycles,
        }

    started = state.get("cycleStartedAt")
    if started is not None:
        # While a manual handoff is in progress (cyclePausedAt set) the minute clock
        # is FROZEN: elapsed is measured up to the instant the pause began, not to
        # now, so human wait never trips the budget. resume() later shifts
        # cycleStartedAt forward so the same exclusion holds after the wait ends.
        paused_at = state.get("cyclePausedAt")
        ref = float(paused_at) if paused_at is not None else time.time()
        elapsed_min = (ref - float(started)) / 60.0
        if elapsed_min > max_minutes:
            return EXIT_EXCEEDED, {
                "ok": False, "reason": "maxMinutesPerCycle",
                "elapsedMinutes": round(elapsed_min, 2), "maxMinutesPerCycle": max_minutes,
                "cycle": cycle,
            }

    return EXIT_OK, {
        "ok": True, "cycle": cycle, "maxCycles": max_cycles,
        "maxMinutesPerCycle": max_minutes,
    }


def check_token_budget(state_dir: Path) -> tuple[int, dict]:
    """Enforce the per-SUT input-token ceiling recorded in
    state/_summaries/llm-budget.json.

    The ceiling is computed deterministically by context_pack_builder.py
    (`estimatedTokensIn` vs `maxTokensIn`, `overBudget` flag). This is the
    consumer that makes it blocking: if any SUT pack is over budget the cycle is
    refused (exit 2) so the over-budget pack is never dispatched to the LLM and
    no Java is written.

    Absent file ⇒ EXIT_OK: no packs have been built yet, so there is nothing to
    enforce here (the cycle/minute budget in `check` still bounds the loop). An
    unreadable/malformed file ⇒ EXIT_MALFORMED so a corrupt budget never reads
    as "within budget".
    """
    budget_path = state_dir / "_summaries" / "llm-budget.json"
    if not budget_path.exists():
        return EXIT_OK, {"ok": True, "reason": "noBudgetFile"}

    data = json.loads(budget_path.read_text(encoding="utf-8"))
    entries = data.get("entries", []) or []
    over = []
    for e in entries:
        est = int(e.get("estimatedTokensIn", 0) or 0)
        cap = e.get("maxTokensIn")
        if e.get("overBudget") or (cap is not None and est > int(cap)):
            over.append({"sut": e.get("sut"), "estimatedTokensIn": est, "maxTokensIn": cap})

    if over:
        return EXIT_EXCEEDED, {
            "ok": False, "reason": "maxTokensIn",
            "count": len(over), "overBudgetSuts": over,
        }
    return EXIT_OK, {"ok": True, "suts": len(entries)}


def tick(state_path: Path) -> tuple[int, dict]:
    state = _load(state_path)
    state["cycle"] = int(state.get("cycle", 0)) + 1
    state["cycleStartedAt"] = time.time()
    atomic_write_json(state_path, state)
    return EXIT_OK, {"ok": True, "cycle": state["cycle"], "cycleStartedAt": state["cycleStartedAt"]}


def reset(state_path: Path) -> tuple[int, dict]:
    state = _load(state_path)
    state.pop("cycleStartedAt", None)
    state.pop("cyclePausedAt", None)
    atomic_write_json(state_path, state)
    return EXIT_OK, {"ok": True, "cycle": int(state.get("cycle", 0))}


def pause(state_path: Path) -> tuple[int, dict]:
    """Freeze the per-cycle minute clock for a manual handoff.

    Stamps ``cyclePausedAt`` (idempotent: a second pause keeps the first stamp, so
    a nested/duplicate pause never loses paused time). If no cycle is in progress
    (no ``cycleStartedAt``) this is a no-op — there is nothing to freeze.
    """
    state = _load(state_path)
    if state.get("cycleStartedAt") is None:
        return EXIT_OK, {"ok": True, "paused": False, "reason": "noCycleInProgress"}
    if state.get("cyclePausedAt") is None:
        state["cyclePausedAt"] = time.time()
        atomic_write_json(state_path, state)
    return EXIT_OK, {"ok": True, "paused": True, "cyclePausedAt": state.get("cyclePausedAt")}


def resume(state_path: Path) -> tuple[int, dict]:
    """Unfreeze the minute clock after a manual handoff.

    Shifts ``cycleStartedAt`` forward by the paused span ``now - cyclePausedAt`` so
    the wait is excluded from elapsed, then clears ``cyclePausedAt``. A resume with
    no matching pause is a no-op.
    """
    state = _load(state_path)
    paused_at = state.get("cyclePausedAt")
    if paused_at is None:
        return EXIT_OK, {"ok": True, "resumed": False}
    started = state.get("cycleStartedAt")
    if started is not None:
        span = time.time() - float(paused_at)
        state["cycleStartedAt"] = float(started) + max(0.0, span)
    state.pop("cyclePausedAt", None)
    atomic_write_json(state_path, state)
    return EXIT_OK, {"ok": True, "resumed": True, "cycleStartedAt": state.get("cycleStartedAt")}


@contextlib.contextmanager
def paused(state_path: Path, reason: str = ""):
    """Context manager that pauses the budget around a manual-handoff wait.

    Emits the canonical ``[budget] paused: <reason>`` / ``[budget] resumed`` log
    lines so the console makes clear that human time is NOT being charged. resume
    always runs (finally), even if the wait raises (timeout, Ctrl+C)::

        with budget_enforcer.paused(state_path, "waiting for manual Claude Code handoff"):
            wait_for_user_or_response_json()
    """
    pause(state_path)
    print(f"[budget] paused: {reason}" if reason else "[budget] paused", flush=True)
    try:
        yield
    finally:
        resume(state_path)
        print("[budget] resumed", flush=True)


_DISPATCH = {"check": check, "tick": tick, "reset": reset, "pause": pause, "resume": resume}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Enforce execution-state.json budget at runtime.")
    p.add_argument("action", choices=[*_DISPATCH, "check-tokens"])
    p.add_argument("--state", type=Path,
                   help="Path to state/execution-state.json (check/tick/reset)")
    p.add_argument("--state-dir", type=Path,
                   help="State directory holding _summaries/llm-budget.json (check-tokens)")
    args = p.parse_args(argv)

    try:
        if args.action == "check-tokens":
            if args.state_dir is None:
                p.error("check-tokens requires --state-dir")
            rc, payload = check_token_budget(args.state_dir)
        else:
            if args.state is None:
                p.error(f"{args.action} requires --state")
            rc, payload = _DISPATCH[args.action](args.state)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        emit_tool_summary("budget_enforcer", "MALFORMED", error=str(e))
        return EXIT_MALFORMED

    status = "OK" if rc == EXIT_OK else "EXCEEDED"
    emit_tool_summary("budget_enforcer", status, **payload)
    return rc


if __name__ == "__main__":
    sys.exit(main())
