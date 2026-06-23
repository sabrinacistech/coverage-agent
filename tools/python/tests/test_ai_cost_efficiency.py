"""test_ai_cost_efficiency.py — FinOps "AI Cost Efficiency" indicator of the final report.

Covers tools/python/batch_final_report.ai_cost_efficiency(): the state-dir audit that
quantifies how curated context packaging cut tokens-per-cycle versus a traditional
full-repo scan, plus the aggregation of the agents' executionMetadata self-assessment.

Convención legacy: expone ``main() -> int`` (0 = ok). Standalone:
    python tools/python/tests/test_ai_cost_efficiency.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # tools/python → import batch_final_report

import batch_final_report as bfr  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(f"{label}: {detail}")


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _scaffold(root: Path) -> tuple[Path, Path, Path]:
    """A fake repo (big-ish source) + a run-dir with one batch and telemetry."""
    repo = root / "repo"
    # ~12 KB of "source" the traditional scan would ingest every cycle.
    (repo / "src" / "main" / "java").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "main" / "java" / "Big.java").write_text("// src\n" + ("x" * 12000), encoding="utf-8")
    # build artifacts that MUST be excluded from the baseline.
    (repo / "target").mkdir(parents=True, exist_ok=True)
    (repo / "target" / "junk.class").write_text("y" * 999999, encoding="utf-8")

    state = root / "state"
    run_dir = state / "_llm" / "runs" / "run-test"
    bd = run_dir / "batches" / "batch-001"
    _write(bd / "request-generation.json", {"targets": [{"targetId": "t1"}]})  # small curated pack
    _write(bd / "response-generation.json", {
        "schemaVersion": "test-generation-batch-response-v2",
        "role": "generation", "batchId": "batch-001",
        "executionMetadata": {"agentName": "test-body-agent",
                              "promptContextSizeEstimate": "COMPACT_PACK_UNDER_10K",
                              "generationIntent": "x"},
        "targets": [{"targetId": "t1", "status": "generated"}],
    })
    _write(run_dir / "costs-telemetry.json", {
        "schemaVersion": 1, "runId": "run-test",
        "total_accumulated_usd": 0.10, "total_prompt_tokens": 500,
        "total_completion_tokens": 100, "total_duration_seconds": 1.0,
        "interactions": [{"targetId": "t1", "estimated": True, "source": "size_estimate"}],
    })
    return state, repo, run_dir


def case_efficiency_indicator_shape_and_savings() -> None:
    with tempfile.TemporaryDirectory() as td:
        state, repo, run_dir = _scaffold(Path(td))
        eff = bfr.ai_cost_efficiency(state, repo, run_dir)

        # Baseline excludes target/ → ~12 KB, not the 1 MB of build junk.
        _assert("baseline excludes build dirs",
                eff["baseline"]["repoSourceBytes"] < 50_000,
                eff["baseline"]["repoSourceBytes"])
        _assert("baseline has prompt-token estimate",
                eff["baseline"]["estimatedPromptTokens"] > 0)
        # Actual prompt tokens come from telemetry (measured/estimated).
        _assert("actual prompt tokens from telemetry",
                eff["actual"]["promptTokens"] == 500, eff["actual"])
        _assert("tokens source flagged estimated",
                eff["actual"]["tokensSource"] == "estimated", eff["actual"]["tokensSource"])
        # The curated pack must be a large reduction vs the full scan.
        _assert("token reduction factor > 1",
                eff["tokenReductionFactor"] > 1.0, eff["tokenReductionFactor"])
        _assert("tokens saved positive", eff["tokensSavedPerRun"] > 0, eff["tokensSavedPerRun"])
        _assert("savings pct in (0,100]", 0 < eff["savingsPct"] <= 100, eff["savingsPct"])
        # Agent self-assessment aggregated from response executionMetadata.
        _assert("self-assessment counts the bucket",
                eff["agentSelfAssessment"].get("COMPACT_PACK_UNDER_10K") == 1,
                eff["agentSelfAssessment"])


def case_efficiency_tolerates_missing_run_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        (repo / "A.java").write_text("class A {}", encoding="utf-8")
        eff = bfr.ai_cost_efficiency(Path(td) / "state", repo, None)
        _assert("no run-dir → zero actual tokens", eff["actual"]["promptTokens"] == 0, eff["actual"])
        _assert("no run-dir → tokens unavailable",
                eff["actual"]["tokensSource"] == "unavailable", eff["actual"]["tokensSource"])
        _assert("no run-dir → no crash, dict returned", isinstance(eff, dict))


def case_report_embeds_indicator_and_markdown() -> None:
    with tempfile.TemporaryDirectory() as td:
        state, repo, run_dir = _scaffold(Path(td))
        report = bfr.build_report(
            state_dir=state, repo=repo, run_dir=run_dir,
            jacoco_status="SKIPPED", jacoco_detail="x",
            delta_status="SKIPPED", delta_detail="x",
        )
        _assert("report schemaVersion bumped to 2", report["schemaVersion"] == 2, report["schemaVersion"])
        _assert("report carries aiCostEfficiency", "aiCostEfficiency" in report)
        md = bfr.render_markdown(report)
        _assert("markdown has AI Cost Efficiency section", "## AI Cost Efficiency" in md)
        _assert("markdown shows reduction factor", "Reducción de tokens" in md)


def main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("case_")]
    for c in cases:
        try:
            c()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{c.__name__} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print("FAIL test_ai_cost_efficiency:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK   test_ai_cost_efficiency: {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
