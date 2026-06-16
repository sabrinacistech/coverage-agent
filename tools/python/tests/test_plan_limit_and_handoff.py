"""test_plan_limit_and_handoff.py — plan-limit / batch-size separation + handoff prompt.

Covers the architecture change that decouples two concepts that used to share the
name ``--batch-size``:

  * ``--plan-limit`` (coverage_planner): how many ranked targets the PLAN contains.
    0 = no limit (rank ALL eligible targets).
  * ``--batch-size`` (orchestrator.batch_runner): how many targets go in each LLM
    request, consumed from the (possibly larger) plan; ``--max-batches`` bounds how
    many batches a run processes.

And the handoff UX fix: the runner prints / writes a ready-to-paste prompt with the
REAL resolved run/batch paths (never the ``run-YYYYMMDD-HHMMSS`` placeholder), so the
human can't paste a wrong folder name and break the agent.

Legacy-suite convention: expose ``main() -> int`` (0 = ok). Auto-discovered by
test_aa_suite_runner.py. Run standalone:
    python tools/python/tests/test_plan_limit_and_handoff.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))   # repo root → orchestrator.*
sys.path.insert(0, str(HERE.parent))       # tools/python → coverage_planner, run_pipeline

from coverage_planner import plan, _resolve_plan_limit  # noqa: E402
import run_pipeline  # noqa: E402
from orchestrator import batch_runner as br  # noqa: E402
from orchestrator import batch_protocol as bp  # noqa: E402
from orchestrator import one_cycle  # noqa: E402


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _state_with_n_targets(state: Path, n: int) -> None:
    """Write coverage-targets.json with n distinct, low-risk, scoreable targets."""
    targets = []
    for i in range(n):
        targets.append({
            "id": f"tgt:{i:04d}",
            "sut": f"com.acme.Svc{i:04d}",
            "method": f"doX{i}()V",
            "missedLines": 3 + (i % 5),
            "missedBranches": i % 3,
        })
    _write(state / "coverage-targets.json", {"targets": targets})
    # All low-risk so none get penalised out of the plan.
    _write(state / "classification-index.json", {
        "schemaVersion": 1,
        "classes": [
            {"fqcn": f"com.acme.Svc{i:04d}", "type": "service", "testabilityRisk": "low"}
            for i in range(n)
        ],
    })


# ── 1-3: planner plan-limit semantics ─────────────────────────────────────────

def case_plan_limit_zero_keeps_all() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = Path(td)
        _state_with_n_targets(state, 105)
        r = plan(state, plan_limit=0)
        if r["sizeChosen"] != 105:
            raise AssertionError(f"plan_limit=0 should keep all 105, got {r['sizeChosen']}")
        if r["totalEligibleTargets"] != 105:
            raise AssertionError(f"totalEligibleTargets should be 105, got {r['totalEligibleTargets']}")
        if r["planLimit"] != 0:
            raise AssertionError(f"planLimit should be 0, got {r['planLimit']}")
        if "full plan" not in r["reason"]:
            raise AssertionError(f"reason should say 'full plan': {r['reason']!r}")


def case_plan_limit_50_keeps_top_50() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = Path(td)
        _state_with_n_targets(state, 105)
        full = plan(state, plan_limit=0)
        r = plan(state, plan_limit=50)
        if r["sizeChosen"] != 50:
            raise AssertionError(f"plan_limit=50 should keep 50, got {r['sizeChosen']}")
        if r["totalEligibleTargets"] != 105:
            raise AssertionError("totalEligibleTargets should still report 105")
        if "limited to top 50 of 105" not in r["reason"]:
            raise AssertionError(f"reason should say limited: {r['reason']!r}")
        # The 50 kept must be the 50 highest-scoring of the full plan (determinism).
        expected = [it["targetId"] for it in full["items"][:50]]
        got = [it["targetId"] for it in r["items"]]
        if got != expected:
            raise AssertionError("plan_limit=50 did not keep the top-50 by score")


def case_default_does_not_truncate_to_10() -> None:
    # plan_limit=None falls back to the legacy batch_size default (10), so callers
    # that pass NOTHING get the historic 10; but the CLI default is 0 (see case 5),
    # and an explicit 0 keeps all — proving the old hard 10-cap is gone.
    with tempfile.TemporaryDirectory() as td:
        state = Path(td)
        _state_with_n_targets(state, 105)
        r = plan(state, plan_limit=0)
        if r["sizeChosen"] == 10:
            raise AssertionError("plan_limit=0 must NOT truncate to the old default of 10")
        if r["sizeChosen"] != 105:
            raise AssertionError(f"expected all 105 with explicit plan_limit=0, got {r['sizeChosen']}")


# ── 4-5: deprecated --batch-size precedence ───────────────────────────────────

def case_legacy_batch_size_resolves_with_warning() -> None:
    # Only --batch-size given → used as plan limit, warn=True.
    limit, warn = _resolve_plan_limit(None, 10)
    if limit != 10 or warn is not True:
        raise AssertionError(f"only --batch-size should resolve to (10, True), got {(limit, warn)}")
    # Neither given → (0, False) = all.
    if _resolve_plan_limit(None, None) != (0, False):
        raise AssertionError("neither flag should resolve to (0, False)")
    # And the planner still honours the legacy value end to end.
    with tempfile.TemporaryDirectory() as td:
        state = Path(td)
        _state_with_n_targets(state, 105)
        r = plan(state, batch_size=10)  # plan_limit defaults to None → fallback 10
        if r["sizeChosen"] != 10:
            raise AssertionError(f"legacy batch_size=10 should keep 10, got {r['sizeChosen']}")


def case_plan_limit_wins_over_batch_size() -> None:
    limit, warn = _resolve_plan_limit(50, 10)
    if limit != 50 or warn is not False:
        raise AssertionError(f"--plan-limit must win over --batch-size: got {(limit, warn)}")


# ── 6-7: CLI wiring (run_pipeline + run_all_deterministic pass --plan-limit) ───

def case_run_pipeline_passes_plan_limit_to_planner() -> None:
    captured: list[list[str]] = []

    def fake_run_step(args):
        captured.append([str(a) for a in args])
        return 0

    orig = run_pipeline.run_step
    orig_argv = sys.argv[:]
    run_pipeline.run_step = fake_run_step
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            # Skip every step except planning so main() reaches the planner cmd
            # without running real subprocesses (run_step is stubbed anyway).
            skip = ["pom", "archetype", "generated", "jacoco", "classpath", "stack",
                    "bytecode", "source", "index", "classification", "deps",
                    "fixtures", "incremental", "validate", "context"]
            sys.argv = ["run_pipeline.py", "--repo", str(out), "--out", str(out),
                        "--plan-limit", "37", "--skip", *skip]
            try:
                run_pipeline.main()
            except SystemExit:
                pass
    finally:
        run_pipeline.run_step = orig
        sys.argv = orig_argv

    planning = [c for c in captured if any("coverage_planner.py" in a for a in c)]
    if not planning:
        raise AssertionError(f"planner step never ran; captured={captured}")
    cmd = planning[0]
    if "--plan-limit" not in cmd or cmd[cmd.index("--plan-limit") + 1] != "37":
        raise AssertionError(f"run_pipeline did not pass --plan-limit 37 to planner: {cmd}")


def case_run_all_help_exposes_plan_limit() -> None:
    # run_all_deterministic is an orchestration script; assert its CLI surfaces
    # --plan-limit (the pass-through to run_pipeline is exercised by the help text
    # contract + the run_pipeline test above).
    import subprocess
    script = HERE.parent / "run_all_deterministic.py"
    out = subprocess.run([sys.executable, str(script), "--help"],
                         capture_output=True, text=True)
    if "--plan-limit" not in out.stdout:
        raise AssertionError("run_all_deterministic --help does not expose --plan-limit")


# ── 8-9: select_batch over the full plan + processed-targets persistence ───────

def case_select_batch_takes_first_n_unprocessed() -> None:
    items = [{"targetId": f"tgt:{i:04d}"} for i in range(105)]
    first = bp.select_batch(items, set(), 3)
    if [t["targetId"] for t in first] != ["tgt:0000", "tgt:0001", "tgt:0002"]:
        raise AssertionError(f"first batch of 3 wrong: {first}")
    # With --max-batches 1 only this batch is processed; the other 102 stay
    # available — proven by selecting again with the first 3 marked processed.
    processed = {t["targetId"] for t in first}
    second = bp.select_batch(items, processed, 3)
    if [t["targetId"] for t in second] != ["tgt:0003", "tgt:0004", "tgt:0005"]:
        raise AssertionError(f"next batch did not skip processed ids: {second}")


def case_processed_targets_json_skips_done() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = Path(td)
        one_cycle.mark_processed(state, "tgt:0000")
        one_cycle.mark_processed(state, "tgt:0001")
        done = one_cycle._processed_ids(state)
        if done != {"tgt:0000", "tgt:0001"}:
            raise AssertionError(f"processed-targets.json round-trip failed: {done}")
        if not (state / "_summaries" / "processed-targets.json").exists():
            raise AssertionError("processed-targets.json was not persisted")
        items = [{"targetId": f"tgt:{i:04d}"} for i in range(105)]
        nxt = bp.select_batch(items, done, 3)
        if [t["targetId"] for t in nxt] != ["tgt:0002", "tgt:0003", "tgt:0004"]:
            raise AssertionError(f"select_batch ignored processed-targets.json: {nxt}")


# ── 10: handoff prompt carries the REAL resolved path (no placeholder) ─────────

def case_handoff_prompt_has_real_paths_no_placeholder() -> None:
    run_id = "run-20260616-164748"
    batch_id = "batch-001"
    paths = br.RunPaths(Path(tempfile.gettempdir()) / "st", run_id)
    req = paths.request_generation(batch_id)
    resp = paths.response_generation(batch_id)

    prompt = br._build_handoff_prompt("generation", req, resp)
    if "run-YYYYMMDD-HHMMSS" in prompt:
        raise AssertionError("handoff prompt leaked the run-YYYYMMDD-HHMMSS placeholder")
    if run_id not in prompt or batch_id not in prompt:
        raise AssertionError("handoff prompt is missing the real run_id/batch_id")
    if bp.SCHEMA_GENERATION_RESPONSE not in prompt:
        raise AssertionError("generation prompt missing the generation response schema")
    if str(req) not in prompt or str(resp) not in prompt:
        raise AssertionError("handoff prompt missing the absolute request/response paths")

    # Repair variant: correct round, file names and schema.
    rreq = paths.request_repair(batch_id, 2)
    rresp = paths.response_repair(batch_id, 2)
    rprompt = br._build_handoff_prompt("repair", rreq, rresp, 2)
    if "run-YYYYMMDD-HHMMSS" in rprompt:
        raise AssertionError("repair prompt leaked the placeholder")
    for needed in (bp.SCHEMA_REPAIR_RESPONSE, "round 2", "request-repair-r2.json",
                   "response-repair-r2.json"):
        if needed not in rprompt:
            raise AssertionError(f"repair prompt missing {needed!r}")

    # RunPaths exposes the on-disk prompt path and assert_consistent accepts it.
    if paths.handoff_prompt(batch_id).name != br.HANDOFF_PROMPT_NAME:
        raise AssertionError("RunPaths.handoff_prompt has the wrong filename")
    paths.assert_consistent(batch_id)


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    cases = [
        ("plan-limit-zero-keeps-all",              case_plan_limit_zero_keeps_all),
        ("plan-limit-50-keeps-top-50",             case_plan_limit_50_keeps_top_50),
        ("default-does-not-truncate-to-10",        case_default_does_not_truncate_to_10),
        ("legacy-batch-size-resolves-with-warning", case_legacy_batch_size_resolves_with_warning),
        ("plan-limit-wins-over-batch-size",        case_plan_limit_wins_over_batch_size),
        ("run-pipeline-passes-plan-limit",         case_run_pipeline_passes_plan_limit_to_planner),
        ("run-all-help-exposes-plan-limit",        case_run_all_help_exposes_plan_limit),
        ("select-batch-takes-first-n-unprocessed", case_select_batch_takes_first_n_unprocessed),
        ("processed-targets-json-skips-done",      case_processed_targets_json_skips_done),
        ("handoff-prompt-real-paths-no-placeholder", case_handoff_prompt_has_real_paths_no_placeholder),
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
    print("\nAll plan-limit / handoff cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
