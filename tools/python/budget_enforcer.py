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
        elapsed_min = (time.time() - float(started)) / 60.0
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
    atomic_write_json(state_path, state)
    return EXIT_OK, {"ok": True, "cycle": int(state.get("cycle", 0))}


_DISPATCH = {"check": check, "tick": tick, "reset": reset}


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
