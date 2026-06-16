"""coverage_planner.py — deterministic test-batch prioritisation.

Reads (produced by prior pipeline steps):
  - state/coverage-targets.json    — per-method coverage gaps (missedLines, missedBranches)
  - state/classification-index.json — SUT type → testabilityRisk + recommendedTemplate
  - state/dependency-graph.json    — fixture IDs available per SUT
  - state/failure-memory.json      — previous fix attempts per symbol (penalty)
  - state/incremental-map.json     — git-diff affected classes (boost)

Scoring formula (per coverage target / method)
----------------------------------------------

  score = (missedLines   * W_LINES)
        + (missedBranches * W_BRANCHES)
        + (missedMethod   * W_METHOD)   # 1 if any miss exists, 0 if fully covered
        - riskPenalty                   # high=30, medium=10, low=0
        - failurePenalty                # sum(attempts * 5) for FAILED entries in memory
        + incrementalBoost              # +20 if SUT is in incremental affectedClasses

  Constants: W_LINES=3, W_BRANCHES=5, W_METHOD=2  (per spec)

  Targets with score <= 0 (fully covered, high-penalty failures, zero miss) are excluded.

Batch plan output
-----------------
  Items are sorted descending by score and capped at `--plan-limit` (default 0 =
  no limit, rank ALL eligible targets). `--plan-limit` is the size of the PLAN;
  it does NOT set the LLM request size — orchestrator.batch_runner controls that
  with `--batch-size` (targets per request) and `--max-batches` (batches per run).
  `--batch-size` here is a DEPRECATED alias for `--plan-limit`.
  Each item carries:
    - targetId, sut, method         — from coverage-targets
    - score                          — computed above (informational)
    - template                       — recommendedTemplate from classification-index
    - fixtureIds                     — fixture IDs from fixture-catalog that match the SUT
    - branchId / mutationId          — set only in branch-coverage / mutation-hardening modes

  The cycle counter is read from the existing batch-plan.json and incremented by 1
  (or starts at 1 on first run).

CLI:
    python tools/python/coverage_planner.py --out state                       # all targets
    python tools/python/coverage_planner.py --out state --plan-limit 50        # top 50
    python tools/python/coverage_planner.py --out state --mode branch-coverage
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from common import SCHEMAS_DIR, atomic_write_json, load_json, validate

# Single source of truth for generated-code matching (same matcher the classifier
# uses). Lets the planner drop generated SUTs directly from generated-code-index.json
# even when they have no contract/classification (bytecode_scanner skips them).
from classification_analyzer import _build_exclusion_matchers, _is_excluded  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Scoring constants
# ─────────────────────────────────────────────────────────────────────────────

W_LINES    = 3
W_BRANCHES = 5
W_METHOD   = 2

_RISK_PENALTY: dict[str, int] = {
    "high":   30,
    "medium": 10,
    "low":    0,
}

_INCREMENTAL_BOOST = 20
_FAILURE_PENALTY_PER_ATTEMPT = 5

_SYNTHETIC_LAMBDA_RE = re.compile(r"^lambda\$(?P<parent>[A-Za-z_$][A-Za-z0-9_$]*)\$\d+\(")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_load(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return load_json(path)
    except Exception as exc:
        print(f"[WARN] cannot load {path}: {exc}", file=sys.stderr)
        return default


def _build_failure_penalties(failure_memory: dict) -> dict[str, int]:
    """Return {symbolFQN_prefix: total_penalty} for FAILED entries."""
    penalties: dict[str, int] = {}
    for entry in failure_memory.get("entries", []):
        if entry.get("lastResult") != "FAILED":
            continue
        symbol = entry.get("symbolFQN", "")
        # symbolFQN may be "com.acme.FooService#methodName" — use the class part
        fqcn = symbol.split("#")[0] if "#" in symbol else symbol
        attempts = int(entry.get("attempts", 0))
        penalties[fqcn] = penalties.get(fqcn, 0) + attempts * _FAILURE_PENALTY_PER_ATTEMPT
    return penalties


def _build_incremental_set(incremental_map: dict) -> frozenset[str]:
    """Return the set of FQCNs that are in the incremental affectedClasses list."""
    return frozenset(incremental_map.get("affectedClasses", []))


def _build_risk_map(classification_index: dict) -> dict[str, tuple[str, str | None]]:
    """Return {fqcn: (testabilityRisk, recommendedTemplate)}."""
    result: dict[str, tuple[str, str | None]] = {}
    for cls in classification_index.get("classes", []):
        fqcn = cls.get("fqcn", "")
        risk = cls.get("testabilityRisk", "medium")
        template = cls.get("recommendedTemplate")
        result[fqcn] = (risk, template)
    return result


# Classification `type` values that are never valid coverage targets. The
# classifier (classification_analyzer.py, Rule 1) is the single source of truth
# for what is generated/excluded; the planner only enforces it.
_EXCLUDED_TYPES: frozenset[str] = frozenset({"generated/excluded", "generated"})


def _build_excluded_set(classification_index: dict) -> frozenset[str]:
    """Return the FQCNs the classifier flagged as generated/excluded.

    These are OpenAPI/CXF/annotation-processor artifacts (e.g. the autogenerated
    ``equals()``/``hashCode()`` on DTO models). Their high missed-branch counts
    would otherwise let them out-score real business logic and dominate the
    batch plan — the exact failure mode where the agent burned cycles writing
    tests for generated OpenAPI models instead of the actual domain code.
    """
    excluded: set[str] = set()
    for cls in classification_index.get("classes", []):
        fqcn = cls.get("fqcn")
        if fqcn and cls.get("type") in _EXCLUDED_TYPES:
            excluded.add(fqcn)
    return frozenset(excluded)


def _build_fixture_ids_map(fixture_catalog: dict) -> dict[str, list[str]]:
    """Return {fqcn: [fixture_id, ...]} grouping fixtures by their type FQCN."""
    result: dict[str, list[str]] = {}
    for f in fixture_catalog.get("fixtures", []):
        ftype = f.get("type", f.get("id", ""))
        fid   = f.get("id", "")
        if ftype:
            result.setdefault(ftype, []).append(fid)
    return result


def _compute_score(
    target: dict,
    risk: str,
    failure_penalty: int,
    incremental_boost: int,
) -> int:
    missed_lines    = int(target.get("missedLines",    0) or 0)
    missed_branches = int(target.get("missedBranches", 0) or 0)
    # Synthetic lambda bodies are not useful test targets by themselves. When a
    # lambda$parent$N target is collapsed onto parent(...), keep its missed
    # coverage in the parent's score so the public method rises in the batch.
    missed_lines += int(target.get("_syntheticMissedLines", 0) or 0)
    missed_branches += int(target.get("_syntheticMissedBranches", 0) or 0)
    missed_method   = 1 if (missed_lines > 0 or missed_branches > 0) else 0

    raw = (
        missed_lines    * W_LINES
        + missed_branches * W_BRANCHES
        + missed_method   * W_METHOD
    )
    return raw - _RISK_PENALTY.get(risk, 10) - failure_penalty + incremental_boost


def _next_cycle(existing_plan: dict) -> int:
    return int(existing_plan.get("cycle", 0)) + 1


def _method_name(method_descriptor: str) -> str:
    return str(method_descriptor or "").split("(", 1)[0]


def _synthetic_lambda_parent(method_descriptor: str) -> str | None:
    match = _SYNTHETIC_LAMBDA_RE.match(str(method_descriptor or ""))
    return match.group("parent") if match else None


def _collapse_synthetic_lambdas(targets: list[dict]) -> list[dict]:
    """Move lambda$parent$N coverage to parent(...) targets when possible.

    JaCoCo reports lambda bodies as methods. They are implementation details and
    the generator correctly avoids testing them directly. The useful target is
    the public/package method that owns the lambda, e.g.:

      lambda$requireConfiguredCluster$0 -> requireConfiguredCluster()

    When that parent method exists in the same SUT, drop the synthetic target and
    annotate/boost the parent target with the lambda coverage gap.
    """
    by_sut_and_method: dict[tuple[str, str], dict] = {}
    for target in targets:
        parent = _synthetic_lambda_parent(target.get("method", ""))
        if parent:
            continue
        method_name = _method_name(target.get("method", ""))
        if method_name:
            by_sut_and_method[(target.get("sut", ""), method_name)] = target

    result: list[dict] = []
    collapsed = 0
    for target in targets:
        parent_name = _synthetic_lambda_parent(target.get("method", ""))
        if not parent_name:
            result.append(target)
            continue

        parent = by_sut_and_method.get((target.get("sut", ""), parent_name))
        if parent is None:
            parent = dict(target)
            parent["method"] = f"{parent_name}()"
            parent["_syntheticParentFallback"] = True
            parent["_syntheticTargets"] = [{
                "targetId": target.get("id", ""),
                "method": target.get("method", ""),
                "missedLines": int(target.get("missedLines", 0) or 0),
                "missedBranches": int(target.get("missedBranches", 0) or 0),
            }]
            result.append(parent)
            collapsed += 1
            continue

        synthetic_targets = parent.setdefault("_syntheticTargets", [])
        synthetic_targets.append({
            "targetId": target.get("id", ""),
            "method": target.get("method", ""),
            "missedLines": int(target.get("missedLines", 0) or 0),
            "missedBranches": int(target.get("missedBranches", 0) or 0),
        })
        parent["_syntheticMissedLines"] = int(parent.get("_syntheticMissedLines", 0) or 0) + int(target.get("missedLines", 0) or 0)
        parent["_syntheticMissedBranches"] = int(parent.get("_syntheticMissedBranches", 0) or 0) + int(target.get("missedBranches", 0) or 0)
        collapsed += 1

    if collapsed:
        print(
            f"[INFO] synthetic lambda target collapse active: "
            f"collapsed {collapsed} lambda target(s) into parent method targets"
        )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Planner
# ─────────────────────────────────────────────────────────────────────────────

def plan(
    state_dir: Path,
    batch_size: int = 10,
    mode: str = "coverage",
    sut_filter: list[str] | None = None,
    incremental_only: bool = False,
    plan_limit: int | None = None,
) -> dict:
    """Compute and return the batch-plan dict.

    When ``sut_filter`` is supplied, only targets whose ``sut`` field matches
    one of the listed FQCNs are scored — keeps batch-plan.json scoped to the
    user-requested subset when --sut is propagated from run_pipeline.

    ``plan_limit`` controls how many ranked targets are written to the plan:
      * ``0``  → no limit, rank ALL eligible targets (the recommended default;
                 the runner then controls per-batch size via --batch-size and how
                 many batches run via --max-batches).
      * ``N>0``→ keep only the top N targets by score.
      * ``None``→ fall back to ``batch_size`` (backward-compat for callers that
                 still pass the legacy ``batch_size`` argument).

    ``plan_limit`` and ``batch_size`` are DISTINCT concepts: ``plan_limit`` is the
    size of the *plan*; ``batch_size`` (used by orchestrator.batch_runner) is the
    *operational* size of each LLM request. The planner no longer decides the
    batch's operational size.
    """
    effective_limit = plan_limit if plan_limit is not None else batch_size

    cov_targets     = _safe_load(state_dir / "coverage-targets.json",     {"targets": []})
    classification  = _safe_load(state_dir / "classification-index.json", {"classes": []})
    dep_graph       = _safe_load(state_dir / "dependency-graph.json",     {"graphs":  []})
    failure_memory  = _safe_load(state_dir / "failure-memory.json",       {"entries": []})
    incremental_map = _safe_load(state_dir / "incremental-map.json",      {"affectedClasses": []})
    fixture_catalog = _safe_load(state_dir / "fixture-catalog.json",      {"fixtures": []})
    existing_plan   = _safe_load(state_dir / "batch-plan.json",           {"cycle":    0})

    penalties     = _build_failure_penalties(failure_memory)
    incremental   = _build_incremental_set(incremental_map)
    risk_map      = _build_risk_map(classification)
    excluded      = _build_excluded_set(classification)
    fixture_ids   = _build_fixture_ids_map(fixture_catalog)

    targets: list[dict] = cov_targets.get("targets", [])

    # Drop targets whose SUT the classifier flagged generated/excluded
    # (OpenAPI/codegen DTOs etc.). riskPenalty alone (max 30) cannot stop a
    # generated equals() with many missed branches from out-scoring real logic,
    # so these must be removed from the plan entirely — they never reach the LLM.
    if excluded:
        before = len(targets)
        targets = [t for t in targets if t.get("sut") not in excluded]
        dropped = before - len(targets)
        if dropped:
            print(
                f"[INFO] generated/excluded filter active: dropped {dropped} "
                f"target(s) on {len(excluded)} excluded class(es) "
                f"(classification-index.json type=generated/excluded)"
            )

    # Belt-and-suspenders: also drop targets on classes the generated-code detector
    # flagged (generated-code-index.json), independent of classification. Generated
    # classes no longer get a symbol-contract (bytecode_scanner skips them), so they
    # never reach classification and would otherwise leak back into the plan here
    # (e.g. an OpenAPI/CXF DTO's equals() out-scoring real logic).
    gen_index = _safe_load(state_dir / "generated-code-index.json", {})
    gen_fqcns, gen_pkg_patterns = _build_exclusion_matchers(gen_index)
    if gen_fqcns or gen_pkg_patterns:
        before = len(targets)
        targets = [
            t for t in targets
            if not _is_excluded(t.get("sut", ""), gen_fqcns, gen_pkg_patterns)
        ]
        dropped = before - len(targets)
        if dropped:
            print(
                f"[INFO] generated-code-index filter active: dropped {dropped} "
                f"target(s) (generated-code-index.json excludedFqcns/excludedPackages)"
            )

    if sut_filter:
        allow = set(sut_filter)
        before = len(targets)
        targets = [t for t in targets if t.get("sut") in allow]
        print(f"[INFO] --sut filter active: {len(targets)}/{before} targets retained for {sorted(allow)}")
    # Post-audit 2026-05-28: when --since was passed and --incremental-only is
    # set, narrow the batch to SUTs flagged as affected by the git diff. The
    # boost-only policy was leaking unaffected SUTs into the LLM context.
    if incremental_only and incremental:
        before = len(targets)
        targets = [t for t in targets if t.get("sut") in incremental]
        print(
            f"[INFO] --incremental-only filter active: "
            f"{len(targets)}/{before} targets retained "
            f"({len(incremental)} affected SUTs from incremental-map.json)"
        )
    if not targets:
        print("[INFO] no coverage targets; batch-plan will be empty")
    else:
        targets = _collapse_synthetic_lambdas(targets)

    # ── Score every target ────────────────────────────────────────────────────
    scored: list[tuple[int, dict]] = []
    for tgt in targets:
        sut    = tgt.get("sut", "")
        risk, template = risk_map.get(sut, ("medium", None))
        penalty = penalties.get(sut, 0)
        boost   = _INCREMENTAL_BOOST if sut in incremental else 0
        score   = _compute_score(tgt, risk, penalty, boost)

        # Skip targets with no missed coverage (already fully covered)
        if score <= 0 and int(tgt.get("missedLines", 0) or 0) == 0 and int(tgt.get("missedBranches", 0) or 0) == 0:
            continue

        scored.append((score, tgt, template, sut))

    # Sort descending by score, then alphabetically for determinism
    scored.sort(key=lambda x: (-x[0], x[3]))

    # ── Apply the plan limit (NOT the operational batch size) ───────────────────
    total_eligible = len(scored)
    if effective_limit and effective_limit > 0:
        selected = scored[:effective_limit]
        is_limited = effective_limit < total_eligible
        plan_limit_value = effective_limit
    else:
        # 0 (or negative) → no cap: rank every eligible target.
        selected = scored
        is_limited = False
        plan_limit_value = 0

    # ── Build batch items ─────────────────────────────────────────────────────
    items: list[dict] = []
    for score, tgt, template, sut in selected:
        target_id = tgt.get("id", "")
        method    = tgt.get("method", "")

        # Fixture IDs: prioritise fixtures for the SUT itself, then its dependencies
        fids: list[str] = list(fixture_ids.get(sut, []))

        item: dict = {
            "targetId":   target_id,
            "sut":        sut,
            "method":     method,
            "score":      score,
            "template":   template,
            "fixtureIds": fids,
        }
        if tgt.get("_syntheticTargets"):
            item["context"] = {
                "syntheticCoverageTargets": tgt["_syntheticTargets"],
                "generationHint": (
                    "Generate tests for this real parent method and cover the "
                    "internal lambda branch(es). Do not generate or skip a "
                    "lambda$... method as a standalone SUT."
                ),
            }
            if tgt.get("_syntheticParentFallback"):
                item["context"]["syntheticParentFallback"] = True
                item["context"]["generationHint"] += (
                    " The parent method was inferred from the lambda name because "
                    "JaCoCo did not report it as a separate uncovered target."
                )

        # Mode-specific fields
        if mode == "branch-coverage" and tgt.get("missedBranches", 0):
            item["branchId"] = f"{target_id}:branch"
        elif mode == "mutation-hardening":
            item["mutationId"] = f"{target_id}:mutation"

        items.append(item)

    cycle = _next_cycle(existing_plan)
    size  = len(items)

    ranking_strategy = (
        f"missedLines×{W_LINES} + missedBranches×{W_BRANCHES} + missedMethod×{W_METHOD} "
        f"- riskPenalty - failurePenalty + incrementalBoost({_INCREMENTAL_BOOST})"
    )
    if size == 0:
        reason = "no uncovered targets found"
    elif is_limited:
        reason = (
            f"limited to top {size} of {total_eligible} eligible targets ranked by "
            f"{ranking_strategy}"
        )
    else:
        reason = (
            f"full plan: all {total_eligible} eligible target(s) ranked by "
            f"{ranking_strategy}"
        )

    return {
        "schemaVersion":        1,
        "cycle":                cycle,
        "mode":                 mode,
        "sizeChosen":           size,
        "totalEligibleTargets": total_eligible,
        "planLimit":            plan_limit_value,
        "rankingStrategy":      ranking_strategy,
        "note": (
            "planLimit=0 ranks every eligible target. The plan size does NOT set "
            "the LLM request size: orchestrator.batch_runner uses --batch-size for "
            "targets per request and --max-batches for how many batches a run "
            "processes."
        ),
        "generatedAt":          datetime.now(timezone.utc).isoformat(),
        "reason":               reason,
        "items":                items,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Schema update — add optional `score` and `template` to batch-plan items
# ─────────────────────────────────────────────────────────────────────────────

def _update_schema(schema_path: Path) -> None:
    if not schema_path.exists():
        return
    try:
        schema = load_json(schema_path)
    except Exception as exc:
        print(f"[WARN] cannot load schema for update: {exc}", file=sys.stderr)
        return

    item_props: dict = (
        schema.get("properties", {})
        .get("items", {})
        .get("items", {})
        .get("properties", {})
    )
    changed = False

    for field, defn in [
        ("score",      {"type": "integer", "description": "Computed coverage priority score"}),
        ("template",   {"type": ["string", "null"], "description": "Recommended test template path"}),
        ("context",    {"type": "object", "description": "Planner hints for generation"}),
        ("generatedAt",{"type": "string"}),
    ]:
        if field not in item_props:
            item_props[field] = defn
            changed = True

    # Also add top-level optional fields. The schema has additionalProperties:false,
    # so any new top-level key the planner emits MUST be declared here or validation
    # fails. All are optional (not added to `required`) for backward compatibility.
    top_props: dict = schema.get("properties", {})
    for field, defn in [
        ("generatedAt",          {"type": "string"}),
        ("totalEligibleTargets", {"type": "integer",
                                  "description": "Eligible targets after scoring, before any plan limit"}),
        ("planLimit",            {"type": "integer",
                                  "description": "Max targets in the plan; 0 = no limit (all eligible)"}),
        ("rankingStrategy",      {"type": "string",
                                  "description": "Human-readable scoring formula used to rank targets"}),
        ("note",                 {"type": "string",
                                  "description": "Operator note clarifying plan size vs operational batch size"}),
    ]:
        if field not in top_props:
            top_props[field] = defn
            changed = True

    if changed:
        atomic_write_json(schema_path, schema)
        print(f"[INFO] updated schema: {schema_path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_plan_limit(plan_limit: int | None, batch_size: int | None) -> tuple[int, bool]:
    """Resolve the effective plan limit and whether to warn about deprecation.

    Precedence (testable, pure):
      * --plan-limit given        → it wins (warn=False), --batch-size ignored.
      * only --batch-size given   → use it as the plan limit (warn=True).
      * neither given             → 0 (no limit, all eligible targets).
    """
    if plan_limit is not None:
        return plan_limit, False
    if batch_size is not None:
        return batch_size, True
    return 0, False


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Compute a prioritised test-batch plan from coverage gaps.\n\n"
            "Scoring formula per target:\n"
            f"  score = (missedLines × {W_LINES})\n"
            f"        + (missedBranches × {W_BRANCHES})\n"
            f"        + (missedMethod × {W_METHOD})   # 1 if any miss, 0 if covered\n"
            "        - riskPenalty   (high=30, medium=10, low=0)\n"
            f"        - failurePenalty  (FAILED attempts × {_FAILURE_PENALTY_PER_ATTEMPT})\n"
            f"        + incrementalBoost (affectedClasses: +{_INCREMENTAL_BOOST})\n\n"
            "Writes state/batch-plan.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out", required=True, help="State directory (e.g. state/)")
    ap.add_argument(
        "--plan-limit",
        type=int,
        default=None,
        help="How many ranked targets to write to batch-plan.json. "
             "0 = no limit (rank ALL eligible targets, recommended default). "
             "N>0 = keep only the top N. This is the PLAN size, NOT the LLM "
             "request size — orchestrator.batch_runner controls that with "
             "--batch-size / --max-batches.",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="DEPRECATED — use --plan-limit instead. When given (and --plan-limit "
             "is not), it is interpreted as the plan limit, not the operational "
             "batch size, and a deprecation warning is emitted.",
    )
    ap.add_argument(
        "--mode",
        default="coverage",
        choices=["coverage", "branch-coverage", "mutation-hardening"],
        help="Coverage mode (default: coverage)",
    )
    ap.add_argument(
        "--sut",
        action="append",
        default=None,
        metavar="FQCN",
        help=(
            "P3.a: restrict planning to one or more SUT FQCNs. Repeat for "
            "multiple. Targets whose `sut` field is not in this list are "
            "dropped before scoring."
        ),
    )
    ap.add_argument(
        "--incremental-only",
        action="store_true",
        help=(
            "Post-audit 2026-05-28: when state/incremental-map.json is "
            "non-empty, restrict the batch-plan to SUTs in affectedClasses "
            "(instead of merely boosting them). Use this when run_pipeline "
            "was invoked with --since to keep the LLM context narrow."
        ),
    )
    args = ap.parse_args()

    state_dir = Path(args.out).resolve()

    _update_schema(SCHEMAS_DIR / "batch-plan.schema.json")

    plan_limit, warn_deprecated = _resolve_plan_limit(args.plan_limit, args.batch_size)
    if warn_deprecated:
        print(
            "--batch-size in coverage_planner is deprecated; use --plan-limit instead.",
            file=sys.stderr,
        )

    result = plan(
        state_dir,
        plan_limit=plan_limit,
        mode=args.mode,
        sut_filter=args.sut,
        incremental_only=args.incremental_only,
    )
    validate("batch-plan", result)
    atomic_write_json(state_dir / "batch-plan.json", result)

    n = result["sizeChosen"]
    cycle = result["cycle"]
    total = result["totalEligibleTargets"]
    limit_str = "all" if result["planLimit"] == 0 else str(result["planLimit"])
    scores = [it.get("score", 0) for it in result["items"]]
    score_range = f"scores=[{min(scores)}..{max(scores)}]" if scores else "scores=[]"
    print(
        f"[OK] state/batch-plan.json  cycle={cycle}  items={n}/{total} eligible  "
        f"planLimit={limit_str}  {score_range}  mode={args.mode}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
