"""test_token_budget.py — Step 1 of the audit reconnection: the cost/token half
of the Fundamental Rule is now enforced, not merely warned.

context_pack_builder.py writes state/_summaries/llm-budget.json with
`estimatedTokensIn` vs `maxTokensIn` per SUT, but nothing consumed it to block —
the cost/token budget was built-but-disconnected (only a `[WARN]`). This suite
proves the new consumer:

  - budget_enforcer.check_token_budget reads the file and refuses (exit 2) when a
    pack is over budget, passes (exit 0) when within budget or absent, and
    reports MALFORMED on a corrupt file.
  - cycle_loop refuses to dispatch the cycle command when a pack is over budget:
    the loop returns RC_BUDGET_EXCEEDED and the command NEVER runs (zero LLM
    dispatch, zero Java written) — finiteness of cost by construction.

Run: `python tools/python/tests/test_token_budget.py`  (exits non-zero on failure)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import budget_enforcer  # noqa: E402
import cycle_loop  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ OK ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        FAILURES.append(label)


def _write_budget(state_dir: Path, entries: list[dict]) -> None:
    """Write a minimal-but-schema-faithful llm-budget.json with the given entries."""
    summaries = state_dir / "_summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 2,
        "tokensPerByte": 0.25,
        "totals": {
            "suts": len(entries),
            "compactPackBytes": sum(e.get("compactPackBytes", 0) for e in entries),
            "estimatedTokensIn": sum(e.get("estimatedTokensIn", 0) for e in entries),
            "maxTokensIn": entries[0]["maxTokensIn"] if entries else 0,
            "overBudgetCount": sum(1 for e in entries if e.get("overBudget")),
        },
        "entries": entries,
    }
    (summaries / "llm-budget.json").write_text(json.dumps(payload), encoding="utf-8")


def _entry(sut: str, est: int, cap: int) -> dict:
    return {
        "sut": sut,
        "contextPackBytes": est * 4,
        "compactPackBytes": est * 4,
        "estimatedTokensIn": est,
        "maxTokensIn": cap,
        "overBudget": est > cap,
        "truncatedFields": [],
    }


def _seed_state(state_dir: Path, max_cycles: int = 10) -> Path:
    sp = state_dir / "execution-state.json"
    sp.write_text(
        json.dumps({"schemaVersion": 1, "cycle": 0, "phase": "generation",
                    "budget": {"maxCycles": max_cycles, "maxMinutesPerCycle": 10}}),
        encoding="utf-8",
    )
    return sp


def case_within_budget_passes() -> None:
    print("== check_token_budget: within budget → EXIT_OK ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        _write_budget(sd, [_entry("com.acme.A", est=100, cap=8000),
                           _entry("com.acme.B", est=7999, cap=8000)])
        rc, payload = budget_enforcer.check_token_budget(sd)
        _assert("rc == EXIT_OK", rc == budget_enforcer.EXIT_OK, f"rc={rc} {payload}")
        _assert("ok flag true", payload.get("ok") is True, str(payload))


def case_over_budget_blocks() -> None:
    print("== check_token_budget: a pack over maxTokensIn → EXIT_EXCEEDED ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        _write_budget(sd, [_entry("com.acme.A", est=100, cap=8000),
                           _entry("com.acme.Huge", est=12000, cap=8000)])
        rc, payload = budget_enforcer.check_token_budget(sd)
        _assert("rc == EXIT_EXCEEDED", rc == budget_enforcer.EXIT_EXCEEDED, f"rc={rc}")
        _assert("reason maxTokensIn", payload.get("reason") == "maxTokensIn", str(payload))
        suts = {s["sut"] for s in payload.get("overBudgetSuts", [])}
        _assert("names the offending SUT only", suts == {"com.acme.Huge"}, str(suts))


def case_absent_file_passes() -> None:
    print("== check_token_budget: no llm-budget.json → EXIT_OK (nothing to enforce) ==")
    with tempfile.TemporaryDirectory() as td:
        rc, payload = budget_enforcer.check_token_budget(Path(td))
        _assert("rc == EXIT_OK", rc == budget_enforcer.EXIT_OK, f"rc={rc}")
        _assert("reason noBudgetFile", payload.get("reason") == "noBudgetFile", str(payload))


def case_overbudget_flag_honoured_without_cap() -> None:
    print("== check_token_budget: trusts overBudget flag even if maxTokensIn absent ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        entry = {"sut": "com.acme.C", "contextPackBytes": 1, "compactPackBytes": 1,
                 "estimatedTokensIn": 1, "overBudget": True, "truncatedFields": []}
        summaries = sd / "_summaries"; summaries.mkdir(parents=True)
        (summaries / "llm-budget.json").write_text(
            json.dumps({"schemaVersion": 2, "tokensPerByte": 0.25,
                        "totals": {"suts": 1, "compactPackBytes": 1, "estimatedTokensIn": 1},
                        "entries": [entry]}),
            encoding="utf-8",
        )
        rc, payload = budget_enforcer.check_token_budget(sd)
        _assert("rc == EXIT_EXCEEDED", rc == budget_enforcer.EXIT_EXCEEDED, f"rc={rc} {payload}")


def case_loop_refuses_dispatch_over_budget() -> None:
    print("== cycle_loop: over-budget pack → RC_BUDGET_EXCEEDED, command never runs ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        sp = _seed_state(sd)
        _write_budget(sd, [_entry("com.acme.Huge", est=12000, cap=8000)])

        sentinel = sd / "command-ran.flag"
        # If the cycle command ever runs it touches the sentinel (stands in for an
        # LLM dispatch / a Java write). After an over-budget refusal it must NOT exist.
        cmd = [sys.executable, "-c",
               "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
               str(sentinel)]
        rc = cycle_loop.run_loop(sp, sd, cmd, done_exit_code=7, max_iterations=None)

        _assert("loop returns RC_BUDGET_EXCEEDED",
                rc == cycle_loop.RC_BUDGET_EXCEEDED, f"rc={rc}")
        _assert("cycle command NEVER ran (zero dispatch / zero Java)",
                not sentinel.exists(), "sentinel was written → command dispatched")
        # cycleStartedAt must be cleared on the refusal path.
        st = json.loads(sp.read_text(encoding="utf-8"))
        _assert("cycleStartedAt reset on refusal", "cycleStartedAt" not in st, str(st))


def case_loop_proceeds_within_budget() -> None:
    print("== cycle_loop: within budget → command runs (gate is not over-eager) ==")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        sp = _seed_state(sd)
        _write_budget(sd, [_entry("com.acme.A", est=100, cap=8000)])

        sentinel = sd / "command-ran.flag"
        # Touch the sentinel, then signal DONE (exit 7) so the loop stops cleanly.
        cmd = [sys.executable, "-c",
               "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran'); sys.exit(7)",
               str(sentinel)]
        rc = cycle_loop.run_loop(sp, sd, cmd, done_exit_code=7, max_iterations=None)

        _assert("loop returns RC_DONE", rc == cycle_loop.RC_DONE, f"rc={rc}")
        _assert("cycle command DID run", sentinel.exists(), "sentinel missing")


def main() -> int:
    case_within_budget_passes()
    case_over_budget_blocks()
    case_absent_file_passes()
    case_overbudget_flag_honoured_without_cap()
    case_loop_refuses_dispatch_over_budget()
    case_loop_proceeds_within_budget()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} case(s): {FAILURES}")
        return 1
    print("All token-budget cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
