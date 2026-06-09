"""test_budget_pause.py — the minute budget must not charge manual-handoff time.

Root cause being locked down: the per-cycle minute budget
(maxMinutesPerCycle) measured wall-clock from cycleStartedAt, so the interactive
IDE handoff — Claude Code generating the JSON, the user pressing ENTER — counted
against it. A low value (default 10) tripped BUDGET_EXCEEDED while the human was
still thinking. budget_enforcer.pause/resume (and the paused() context manager)
freeze the clock during the wait so only the runner's AUTOMATIC work is charged.

Covers:
  * pause freezes elapsed at the pause instant (work within budget stays OK)
  * pause does NOT rescue pre-pause overrun (freeze is at the instant, not a reset)
  * without pause, real automatic overrun still trips BUDGET_EXCEEDED (exit 2)
  * resume shifts cycleStartedAt forward by the paused span
  * the paused() context manager sets/clears cyclePausedAt and keeps check() OK
  * pause is a no-op when no cycle is in progress

Legacy-suite convention: expose ``main() -> int`` (0 = ok). Auto-discovered by
test_aa_suite_runner.py. Run standalone:
    python tools/python/tests/test_budget_pause.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import budget_enforcer  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(f"{label}" + (f" — {detail}" if detail else ""))


def _state(path: Path, **fields) -> None:
    base = {"schemaVersion": 1, "cycle": 1, "budget": {"maxMinutesPerCycle": 10}}
    base.update(fields)
    path.write_text(json.dumps(base), encoding="utf-8")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── frozen clock ──────────────────────────────────────────────────────────────

def case_paused_within_budget_is_ok() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "execution-state.json"
        # 300s of pre-pause work (< 600s = 10 min budget); paused since.
        _state(p, cycleStartedAt=1000.0, cyclePausedAt=1300.0)
        rc, payload = budget_enforcer.check(p)
        _assert("paused-within-budget OK", rc == 0, f"rc={rc} payload={payload}")


def case_paused_does_not_rescue_pre_pause_overrun() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "execution-state.json"
        # 700s of AUTOMATIC work elapsed BEFORE pausing (> 600s budget): pausing
        # now must not magically reset it — the freeze is at the pause instant.
        _state(p, cycleStartedAt=1000.0, cyclePausedAt=1700.0)
        rc, _ = budget_enforcer.check(p)
        _assert("paused-does-not-rescue-overrun", rc == budget_enforcer.EXIT_EXCEEDED)


# ── automatic work still bounded ───────────────────────────────────────────────

def case_automatic_overrun_trips_exceeded() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "execution-state.json"
        # No pause: 700s of real elapsed automatic work > 600s budget → exit 2.
        _state(p, cycleStartedAt=time.time() - 700.0)
        rc, _ = budget_enforcer.check(p)
        _assert("automatic-overrun → EXCEEDED", rc == budget_enforcer.EXIT_EXCEEDED)


def case_automatic_within_budget_is_ok() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "execution-state.json"
        _state(p, cycleStartedAt=time.time() - 60.0)  # 1 min of work < 10 min
        rc, _ = budget_enforcer.check(p)
        _assert("automatic-within-budget OK", rc == 0)


# ── resume shifts the start forward ─────────────────────────────────────────────

def case_resume_shifts_started_forward() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "execution-state.json"
        _state(p, cycleStartedAt=5000.0, cyclePausedAt=time.time() - 50.0)
        budget_enforcer.resume(p)
        st = _load(p)
        _assert("resume cleared cyclePausedAt", "cyclePausedAt" not in st)
        _assert(
            "resume shifted cycleStartedAt by ~paused span",
            st.get("cycleStartedAt", 0) >= 5000.0 + 49.0,
            f"cycleStartedAt={st.get('cycleStartedAt')}",
        )


def case_resume_without_pause_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "execution-state.json"
        _state(p, cycleStartedAt=5000.0)
        rc, payload = budget_enforcer.resume(p)
        _assert("resume-without-pause no-op", rc == 0 and payload.get("resumed") is False)
        _assert("resume-without-pause keeps start", _load(p).get("cycleStartedAt") == 5000.0)


# ── context manager ─────────────────────────────────────────────────────────────

def case_paused_context_manager_freezes_and_clears() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "execution-state.json"
        # Cycle started "now"; a long handoff would otherwise accrue against the
        # budget. Inside paused(), check() must stay OK and cyclePausedAt be set.
        _state(p, cycleStartedAt=time.time())
        with budget_enforcer.paused(p, "waiting for manual Claude Code handoff"):
            mid = _load(p)
            _assert("inside-paused stamps cyclePausedAt", "cyclePausedAt" in mid)
            rc, _ = budget_enforcer.check(p)
            _assert("inside-paused check OK", rc == 0)
        after = _load(p)
        _assert("after-paused clears cyclePausedAt", "cyclePausedAt" not in after)


# ── no-op when no cycle in progress ──────────────────────────────────────────────

def case_pause_noop_without_cycle() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "execution-state.json"
        _state(p)  # no cycleStartedAt
        rc, payload = budget_enforcer.pause(p)
        _assert("pause no-op without cycle", rc == 0 and payload.get("paused") is False)
        _assert("pause wrote no cyclePausedAt", "cyclePausedAt" not in _load(p))


def main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("case_")]
    for c in cases:
        try:
            c()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{c.__name__} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print("FAIL test_budget_pause:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK   test_budget_pause: {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
