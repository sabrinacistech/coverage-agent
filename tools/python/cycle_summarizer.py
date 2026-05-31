"""cycle_summarizer.py — compress cycle state into state/_summaries/cycle-N.json.

Called by the Coverage Orchestrator at the END of each cycle, after the Reporting
Agent has produced the coverage delta. Compresses all per-cycle state into a
compact summary that fits in the LLM context budget for subsequent cycles.

Usage:
    python cycle_summarizer.py --state state/ --cycle 3

Effect:
    Writes state/_summaries/cycle-3.json with a compact digest.
    Cleans up stale patches in state/_patches/ (successful patches older than
    the two most recent cycles are archived/removed).

Context budget rule:
    Orchestrator loads summaries for the last 2 completed cycles only.
    Full state files (generated-tests.json, compile-error-index.json, etc.)
    are NOT re-loaded in subsequent cycles; only the summary is used.

Anti-patterns prevented:
    - Accumulating full per-cycle JSON in the Orchestrator's context.
    - Re-reading JaCoCo XML or compile errors from previous cycles.
    - Growing context budget O(cycles) instead of O(1).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import atomic_write_json, load_json, sha256_file

MAX_EVIDENCE_IDS = 50  # cap to avoid bloating summary


def _load_safe(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return load_json(path)
    except Exception:
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Data extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_coverage_delta(state_dir: Path) -> dict:
    """Extract delta totals from coverage-delta.json.

    jacoco_parser.py writes the nested format:
      {"totals": {"lines": {"before": N, "after": N, "delta": N}, ...}}
    We expose the per-counter delta for the cycle summary.
    """
    raw = _load_safe(state_dir / "coverage-delta.json", {})
    totals = raw.get("totals", {})
    return {
        "lines": totals.get("lines", {}).get("delta", 0),
        "branches": totals.get("branches", {}).get("delta", 0),
        "instructions": totals.get("instructions", {}).get("delta", 0),
    }


def _extract_generated_tests(state_dir: Path) -> tuple[int, int, list[str], list[str]]:
    """Returns (generated_count, discarded_count, targets, evidence_ids)."""
    gt = _load_safe(state_dir / "generated-tests.json", [])
    tests = gt if isinstance(gt, list) else gt.get("tests", [])
    generated = [t for t in tests if t.get("status") != "DISCARDED"]
    discarded = [t for t in tests if t.get("status") == "DISCARDED"]
    targets = sorted({t.get("sut", "") for t in generated if t.get("sut")})
    evidence_ids: list[str] = []
    for t in generated:
        evidence_ids.extend(t.get("evidenceIds", []))
    # Deduplicate + cap
    evidence_ids = sorted(set(evidence_ids))[:MAX_EVIDENCE_IDS]
    return len(generated), len(discarded), targets, evidence_ids


def _extract_discard_reasons(state_dir: Path) -> dict[str, int]:
    gt = _load_safe(state_dir / "generated-tests.json", [])
    tests = gt if isinstance(gt, list) else gt.get("tests", [])
    reasons: dict[str, int] = {}
    for t in tests:
        if t.get("status") == "DISCARDED":
            r = t.get("reason", "UNKNOWN")
            reasons[r] = reasons.get(r, 0) + 1
    return reasons


def _extract_repair_stats(state_dir: Path) -> tuple[int, int]:
    fm = _load_safe(state_dir / "failure-memory.json", {})
    entries = fm.get("entries", []) if isinstance(fm, dict) else []
    attempts = len(entries)
    succeeded = sum(1 for e in entries if e.get("status") == "FIXED")
    return attempts, succeeded


def _extract_gate_status(state_dir: Path) -> dict:
    es = _load_safe(state_dir / "execution-state.json", {})
    return {
        "G8_triggered": es.get("G8_triggered", False),
        "consecutiveZeroDelta": es.get("consecutiveZeroDeltaCycles", 0),
        "compileFailRate": es.get("compileFailRateWindow", 0.0),
    }


def _extract_stack_profile_hash(state_dir: Path) -> str:
    sp = state_dir / "stack-profile.json"
    if sp.exists():
        return "sha256:" + sha256_file(sp)[:16]
    return "unknown"


def _collect_cycle_patches(state_dir: Path, cycle: int) -> list[str]:
    patches_dir = state_dir / "_patches"
    if not patches_dir.exists():
        return []
    prefix = f"{cycle:03d}-"
    return sorted(p.name for p in patches_dir.glob(f"{prefix}*.diff"))


def _cleanup_old_patches(state_dir: Path, keep_cycles: int = 2) -> None:
    """Remove successful patch diffs older than `keep_cycles` cycles."""
    patches_dir = state_dir / "_patches"
    if not patches_dir.exists():
        return
    summaries_dir = state_dir / "_summaries"
    # Find the latest N cycles from summaries
    if summaries_dir.exists():
        summary_cycles = sorted(
            int(f.stem.replace("cycle-", ""))
            for f in summaries_dir.glob("cycle-*.json")
            if f.stem.replace("cycle-", "").isdigit()
        )
        if len(summary_cycles) > keep_cycles:
            cutoff = summary_cycles[-(keep_cycles + 1)]
            for diff_file in patches_dir.glob("*.diff"):
                # Patch name: NNN-slug-pXXXX.diff; NNN is cycle number
                parts = diff_file.stem.split("-")
                if parts and parts[0].isdigit():
                    patch_cycle = int(parts[0])
                    if patch_cycle <= cutoff:
                        diff_file.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compress completed cycle state into a compact summary (Phase 5)."
    )
    ap.add_argument("--state", required=True, help="State directory")
    ap.add_argument("--cycle", required=True, type=int, help="Cycle number just completed")
    ap.add_argument("--mode", default=None,
                    help="Coverage mode (coverage|branch-coverage|mutation-hardening)")
    args = ap.parse_args()

    state_dir = Path(args.state).resolve()
    summaries_dir = state_dir / "_summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)

    # Derive mode from execution-state if not passed
    mode = args.mode
    if not mode:
        es = _load_safe(state_dir / "execution-state.json", {})
        mode = es.get("mode", "coverage")

    # ── Collect data ─────────────────────────────────────────────────────────
    coverage_delta = _extract_coverage_delta(state_dir)
    tests_gen, tests_disc, targets, evidence_ids = _extract_generated_tests(state_dir)
    discard_reasons = _extract_discard_reasons(state_dir)
    repair_attempts, repairs_succeeded = _extract_repair_stats(state_dir)
    gate_status = _extract_gate_status(state_dir)
    stack_profile_hash = _extract_stack_profile_hash(state_dir)
    patch_files = _collect_cycle_patches(state_dir, args.cycle)

    # ── Build summary ─────────────────────────────────────────────────────────
    summary = {
        "cycle": args.cycle,
        "mode": mode,
        "completedAt": _now(),
        "stackProfileHash": stack_profile_hash,
        "coverageDelta": coverage_delta,
        "testsGenerated": tests_gen,
        "testsDiscarded": tests_disc,
        "repairAttempts": repair_attempts,
        "repairsSucceeded": repairs_succeeded,
        "targets": targets,
        "discardReasons": discard_reasons,
        "gates": gate_status,
        "patchFiles": patch_files,
        "evidenceIds": evidence_ids,
        "_meta": {
            "note": "Compact cycle summary. Full state files for this cycle are NOT "
                    "reloaded in subsequent LLM prompts; only this summary is used.",
            "maxEvidenceIdsCap": MAX_EVIDENCE_IDS,
        },
    }

    out_path = summaries_dir / f"cycle-{args.cycle}.json"
    atomic_write_json(out_path, summary)

    print(f"[OK] cycle-{args.cycle}.json → Δlines={coverage_delta['lines']}, "
          f"gen={tests_gen}, disc={tests_disc}, repairs={repair_attempts}/{repairs_succeeded}")

    # ── Cleanup old patches ───────────────────────────────────────────────────
    _cleanup_old_patches(state_dir, keep_cycles=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
