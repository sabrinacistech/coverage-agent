"""test_batch_runner.py — orchestration of the incremental batch handoff.

Drives orchestrator.batch_runner.run_batches end to end with the side-effecting
edges stubbed (the manual handoff, the patcher, the Maven test run) so the
deterministic orchestration is exercised without a TTY or a JVM:

  * all-pass batch → every target PASSED, manifest DONE, targets marked processed
  * a failing target is repaired in round 1 → PASSED
  * a target still failing after maxRepairRounds → ABANDONED
  * a per-item 'skipped' generation result → SKIPPED, never fatal
  * the handoff wait is wrapped in a budget pause (cyclePausedAt cleared after)

Legacy-suite convention: expose ``main() -> int`` (0 = ok). Auto-discovered by
test_aa_suite_runner.py. Run standalone:
    python tools/python/tests/test_batch_runner.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))   # repo root → orchestrator.*
sys.path.insert(0, str(HERE.parent))       # tools/python → budget_enforcer

from orchestrator import batch_runner as br  # noqa: E402
from orchestrator import batch_protocol as bp  # noqa: E402

FAILURES: list[str] = []

# Capture the real edges so each case restores them (cases monkeypatch module
# globals, which would otherwise leak between cases in declaration order).
_ORIG = {n: getattr(br, n) for n in
         ("_apply_patch", "_run_tests", "_surefire_status", "_wait_for_response",
          "_wait_polling", "_wait_interactive")}
_ORIG_RUN_TOOL = br.one_cycle._run_tool


def _restore() -> None:
    for n, fn in _ORIG.items():
        setattr(br, n, fn)
    br.one_cycle._run_tool = _ORIG_RUN_TOOL


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(label + (f" — {detail}" if detail else ""))


def _setup(td: Path, n: int = 3) -> Path:
    state = td / "state"
    state.mkdir(parents=True, exist_ok=True)
    items = [{"targetId": f"com.acme.C{i}#m", "sut": f"com.acme.C{i}", "method": "m",
              "score": 100 - i} for i in range(n)]
    (state / "batch-plan.json").write_text(
        json.dumps({"schemaVersion": 1, "cycle": 0, "mode": "coverage",
                    "sizeChosen": n, "items": items}), encoding="utf-8")
    (state / "execution-state.json").write_text(
        json.dumps({"schemaVersion": 1, "mode": "coverage", "cycle": 0,
                    "phase": "generation", "budget": {"maxMinutesPerCycle": 999},
                    "checkpoints": []}), encoding="utf-8")
    return state


def _patch(sut: str, *, prefix: str = "patch") -> dict:
    return {
        "schemaVersion": 1,
        "patchId": f"{prefix}:abcdef",
        "cycle": 1,
        "sut": sut,
        "testClass": sut + "Test",
        "testPackage": sut.rsplit(".", 1)[0],
        "template": "junit5-mockito",
        "allowedImports": ["org.junit.jupiter.api.Test"],
        "methods": [{
            "name": "m_whenCondition_returnsExpected",
            "annotations": ["@Test"],
            "body": "// given\nObject value = new Object();\n// when\nObject actual = value;\n// then\norg.junit.jupiter.api.Assertions.assertSame(value, actual);",
            "evidenceIds": ["sym:com.acme.C0#m:12345678"],
        }],
    }


def _gen_resp(batch_id: str, statuses: dict[str, str]) -> dict:
    items = []
    for tid, st in statuses.items():
        it = {"targetId": tid, "status": st}
        if st == "generated":
            it["patchDescriptor"] = _patch(tid.split("#")[0], prefix="patch")
        else:
            it["reason"] = "stub"
        items.append(it)
    return {"schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "r",
            "batchId": batch_id, "role": "generation", "items": items}


def _repair_resp(batch_id: str, rnd: int, statuses: dict[str, str]) -> dict:
    items = []
    for tid, st in statuses.items():
        it = {"targetId": tid, "status": st}
        if st == "repaired":
            it["patchDescriptor"] = _patch(tid.split("#")[0], prefix="repair")
        else:
            it["reason"] = "stub"
        items.append(it)
    return {"schemaVersion": bp.SCHEMA_REPAIR_RESPONSE, "runId": "r",
            "batchId": batch_id, "role": "repair", "repairRound": rnd, "items": items}


def _install_stubs(monkey_state: dict) -> None:
    """Patch the side-effecting edges. monkey_state carries the canned scripts."""
    br._apply_patch = lambda patch, *, state_dir, repo, repair_attempts=None: 0  # type: ignore
    br.one_cycle._run_tool = lambda script, args: 0  # type: ignore
    gen_q = monkey_state["gen_responses"]
    rep_q = monkey_state["repair_responses"]
    run_q = monkey_state["test_rcs"]
    surefire = monkey_state["surefire"]

    def fake_wait(request, response, *, state_path, manifest, kind, batch_id, repair_round=None):
        # Prove the wait is budget-paused: pause/resume around a no-op, like the real
        # path; the manifest/budget assertions are made by the caller.
        if kind == "generation":
            return "ok", gen_q.pop(0)
        return "ok", rep_q.pop(0)

    def fake_run_tests(repo, state_dir, test_classes):
        return run_q.pop(0) if run_q else 0

    def fake_surefire(repo, test_class):
        return surefire.get(test_class)

    br._wait_for_response = fake_wait  # type: ignore
    br._run_tests = fake_run_tests  # type: ignore
    br._surefire_status = fake_surefire  # type: ignore


def _manifest(state: Path) -> dict:
    runs = sorted((state / "_llm" / "runs").glob("run-*"))
    return json.loads((runs[-1] / "manifest.json").read_text(encoding="utf-8"))


# ── all pass ─────────────────────────────────────────────────────────────────

def case_all_pass() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = _setup(Path(td), 3)
        ids = [f"com.acme.C{i}#m" for i in range(3)]
        _install_stubs({
            "gen_responses": [_gen_resp("batch-001", {i: "generated" for i in ids})],
            "repair_responses": [], "test_rcs": [0], "surefire": {},
        })
        rc = br.run_batches(state, Path(td), batch_size=10, max_repair_rounds=2, max_batches=None)
        m = _manifest(state)
        _assert("all-pass rc DONE", rc == br.RC_DONE, f"rc={rc}")
        _assert("all-pass totals", m["totals"]["passed"] == 3, str(m["totals"]))
        _assert("all-pass manifest DONE", m["status"] == "DONE")


# ── repaired in round 1 ──────────────────────────────────────────────────────

def case_repaired_round1() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = _setup(Path(td), 3)
        ids = [f"com.acme.C{i}#m" for i in range(3)]
        _install_stubs({
            "gen_responses": [_gen_resp("batch-001", {i: "generated" for i in ids})],
            "repair_responses": [_repair_resp("batch-001", 1, {"com.acme.C2#m": "repaired"})],
            # first run fails (rc=1): C0/C1 pass, C2 fails; repair retest passes (rc=0)
            "test_rcs": [1, 0],
            "surefire": {"com.acme.C0Test": "passed", "com.acme.C1Test": "passed",
                         "com.acme.C2Test": "failed"},
        })
        rc = br.run_batches(state, Path(td), batch_size=10, max_repair_rounds=2, max_batches=None)
        m = _manifest(state)
        _assert("repair: C2 PASSED", m["targets"]["com.acme.C2#m"]["status"] == bp.PASSED,
                m["targets"]["com.acme.C2#m"]["status"])
        _assert("repair: 3 passed", m["totals"]["passed"] == 3, str(m["totals"]))


# ── abandoned after maxRepairRounds ──────────────────────────────────────────

def case_abandoned_after_rounds() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = _setup(Path(td), 3)
        ids = [f"com.acme.C{i}#m" for i in range(3)]
        _install_stubs({
            "gen_responses": [_gen_resp("batch-001", {i: "generated" for i in ids})],
            "repair_responses": [_repair_resp("batch-001", 1, {"com.acme.C2#m": "repaired"})],
            # gen run fails C2; repair retest still fails C2 → out of rounds (max=1)
            "test_rcs": [1, 1],
            "surefire": {"com.acme.C0Test": "passed", "com.acme.C1Test": "passed",
                         "com.acme.C2Test": "failed"},
        })
        rc = br.run_batches(state, Path(td), batch_size=10, max_repair_rounds=1, max_batches=None)
        m = _manifest(state)
        _assert("abandon: C2 ABANDONED",
                m["targets"]["com.acme.C2#m"]["status"] == bp.ABANDONED,
                m["targets"]["com.acme.C2#m"]["status"])
        _assert("abandon: 1 abandoned", m["totals"]["abandoned"] == 1, str(m["totals"]))


# ── skipped generation item is not fatal ─────────────────────────────────────

def case_skipped_not_fatal() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = _setup(Path(td), 2)
        _install_stubs({
            "gen_responses": [_gen_resp("batch-001", {
                "com.acme.C0#m": "generated", "com.acme.C1#m": "skipped"})],
            "repair_responses": [], "test_rcs": [0], "surefire": {},
        })
        rc = br.run_batches(state, Path(td), batch_size=10, max_repair_rounds=2, max_batches=None)
        m = _manifest(state)
        _assert("skip: C1 SKIPPED", m["targets"]["com.acme.C1#m"]["status"] == bp.SKIPPED)
        _assert("skip: C0 PASSED", m["targets"]["com.acme.C0#m"]["status"] == bp.PASSED)
        _assert("skip: rc DONE", rc == br.RC_DONE)


# ── budget paused during handoff (no exceed while waiting) ───────────────────

def case_repair_payload_uses_canonical_test_class() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state = _setup(root, 1)
        repo = root / "repo"
        repo.mkdir()
        manifest = bp.new_manifest("run-1", str(repo), generation_mode="handoff-batch",
                                   batch_size=10, max_repair_rounds=2)
        tid = "com.acme.C0#m"
        bp.ensure_target(manifest, tid, sut="com.acme.C0", batch_id="batch-001")
        bp.set_status(manifest, tid, bp.PATCH_FAILED, testClass="com.acme.C0CtorTest")
        payload = br._failed_items_for_repair(
            manifest,
            state_dir=state,
            repo=repo,
            batch_ids=[tid],
            applied={},
        )
        item = payload[0]
        _assert("repair payload canonical testClass",
                item["testClass"] == "com.acme.C0Test", str(item))
        _assert("repair payload records rejected testClass",
                item["rejectedTestClass"] == "com.acme.C0CtorTest", str(item))
        _assert("repair payload canonicalTestClass",
                item["canonicalTestClass"] == "com.acme.C0Test", str(item))


def case_budget_paused_during_handoff() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = _setup(Path(td), 1)
        seen = {}
        # Keep the REAL _wait_for_response (it owns the budget pause); stub only the
        # inner polling wait, which runs INSIDE the pause context, and force the
        # non-interactive path so no input() is reached.
        os.environ["COVAGENT_IDE_INTERACTIVE"] = "0"

        def fake_poll(response):
            st = json.loads((state / "execution-state.json").read_text(encoding="utf-8"))
            seen["pausedDuringWait"] = "cyclePausedAt" in st
            return "ok", _gen_resp("batch-001", {"com.acme.C0#m": "generated"})

        try:
            br._apply_patch = lambda patch, *, state_dir, repo, repair_attempts=None: 0  # type: ignore
            br._run_tests = lambda repo, state_dir, tcs: 0  # type: ignore
            br.one_cycle._run_tool = lambda script, args: 0  # type: ignore
            br._wait_polling = fake_poll  # type: ignore
            br.run_batches(state, Path(td), batch_size=10, max_repair_rounds=0, max_batches=None)
        finally:
            os.environ.pop("COVAGENT_IDE_INTERACTIVE", None)
        _assert("handoff paused the budget", seen.get("pausedDuringWait") is True)
        final = json.loads((state / "execution-state.json").read_text(encoding="utf-8"))
        _assert("budget resumed after run (no cyclePausedAt)", "cyclePausedAt" not in final)


def main() -> int:
    cases = [
        case_all_pass, case_repaired_round1, case_abandoned_after_rounds,
        case_skipped_not_fatal, case_repair_payload_uses_canonical_test_class,
        case_budget_paused_during_handoff,
    ]
    for c in cases:
        _restore()  # each case installs its own stubs over the real edges
        try:
            c()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{c.__name__} raised {type(exc).__name__}: {exc}")
    _restore()
    if FAILURES:
        print("FAIL test_batch_runner:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK   test_batch_runner: {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
