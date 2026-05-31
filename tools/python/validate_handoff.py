"""validate_handoff.py — deterministic gate between Phase 0 and Phase 1 (LLM).

Post-audit 2026-05-28: phases 1-7 of the orchestrator (Discovery, Stack,
Classification, Symbol Contract, Dependency Graph, Fixtures, Planning) used
to be advertised as "LLM phases" but they only read JSONs already produced by
the deterministic pipeline. Running them as LLM turns wasted ~6-8K tokens per
cycle without any decision being made.

This tool replaces those seven turns with a single Python pass that:

  1. Verifies the seven mandatory state files exist;
  2. Asserts each one validates against its schema (via state_validator
     --scope where applicable);
  3. Emits a compact JSON handoff summary at
     state/_summaries/handoff-summary.json containing the minimal facts the
     LLM needs to know (stack versions, batch size, mode, top SUTs);
  4. Returns rc=0 if the LLM may proceed to Phase 8 (Generation), rc=2 if
     any required artefact is missing/invalid (BLOCKED_PRE_STAGE_MISSING).

Usage:
    python tools/python/validate_handoff.py --state state/
    python tools/python/validate_handoff.py --state state/ --print

The LLM should consume only the handoff-summary.json output, not the seven
underlying JSONs. That keeps Phase 1 input cost O(handoff) instead of
O(sum of pre-stage artefacts).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, atomic_write_json, load_json, validate  # noqa: E402


# Required artefacts produced by Phase 0. Missing any of these blocks the
# handoff with status="BLOCKED_PRE_STAGE_MISSING"; an invalid one blocks with
# status="BLOCKED_PRE_STAGE_INVALID". The schema name is the file stem.
_REQUIRED_FILES: tuple[tuple[str, str], ...] = (
    ("build-tool-contract.json", "build-tool-contract"),
    ("archetype-profile.json",   "archetype-profile"),
    ("generated-code-index.json", "generated-code-index"),
    ("import-whitelist.json",    "import-whitelist"),
    ("stack-profile.json",       "stack-profile"),
    ("classification-index.json", "classification-index"),
    ("dependency-graph.json",    "dependency-graph"),
    ("fixture-catalog.json",     "fixture-catalog"),
    ("batch-plan.json",          "batch-plan"),
)

# Required directories with at least one entry.
_REQUIRED_DIRS: tuple[str, ...] = (
    "symbol-contracts",
    "context-packs-compact",
)


def _check_required(state_dir: Path) -> list[str]:
    """Return a list of missing artefact descriptions (empty = all present)."""
    missing: list[str] = []
    for name, _schema in _REQUIRED_FILES:
        p = state_dir / name
        if not p.exists() or p.stat().st_size == 0:
            missing.append(f"file: {name}")
    for dname in _REQUIRED_DIRS:
        d = state_dir / dname
        if not d.exists():
            missing.append(f"dir: {dname}/ (not created)")
            continue
        if not any(d.glob("*.json")):
            missing.append(f"dir: {dname}/ (empty)")
    return missing


def _validate_required(state_dir: Path) -> list[str]:
    """Run each Phase-0 artefact through its JSON Schema. Returns a list of
    error strings (empty = all valid). A handoff that loads but does not
    validate is just as broken as a missing file — block it with the same
    rigour."""
    errors: list[str] = []
    for fname, schema_name in _REQUIRED_FILES:
        p = state_dir / fname
        try:
            doc = load_json(p)
        except Exception as e:
            errors.append(f"{fname}: cannot parse JSON ({e.__class__.__name__})")
            continue
        try:
            validate(schema_name, doc)
        except Exception as e:
            # jsonschema.ValidationError has a useful str() — keep it short.
            msg = str(e).splitlines()[0][:200]
            errors.append(f"{fname}: schema violation: {msg}")
    # Spot-check a sample of per-SUT artefacts so a malformed contract does
    # not slip through. Validate the first contract and the first compact pack;
    # if either is broken every downstream consumer is broken too.
    contracts_dir = state_dir / "symbol-contracts"
    if contracts_dir.exists():
        sample = next(iter(sorted(contracts_dir.glob("*.json"))), None)
        if sample is not None:
            try:
                validate("symbol-contract", load_json(sample))
            except Exception as e:
                errors.append(f"symbol-contracts/{sample.name}: {str(e).splitlines()[0][:200]}")
    packs_dir = state_dir / "context-packs-compact"
    if packs_dir.exists():
        sample = next(iter(sorted(packs_dir.glob("*.json"))), None)
        if sample is not None:
            try:
                validate("protocols/context-pack-compact", load_json(sample))
            except Exception as e:
                errors.append(f"context-packs-compact/{sample.name}: {str(e).splitlines()[0][:200]}")
    return errors


def _safe_load(p: Path) -> dict:
    try:
        return load_json(p)
    except Exception:
        return {}


def _top_suts(batch_plan: dict, limit: int = 10) -> list[dict]:
    items = batch_plan.get("items", []) or []
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        out.append({
            "sut": it.get("sut", ""),
            "method": it.get("method", ""),
            "score": it.get("score", 0),
            "targetId": it.get("targetId", ""),
        })
    return out


def _first_module(doc: dict) -> dict:
    """Return modules[0] if it's a non-empty list of dicts, else {}.

    Phase 0 emits the per-module shape `{ "modules": [ { ... } ] }` for the
    contract / archetype / stack JSONs. For mono-module repos we surface the
    first module; multi-module summaries are out of scope (the LLM consumes
    one batch-plan at a time anyway).
    """
    mods = doc.get("modules")
    if isinstance(mods, list) and mods and isinstance(mods[0], dict):
        return mods[0]
    return {}


def build_summary(state_dir: Path) -> dict:
    """Build the handoff summary the LLM will consume instead of the seven
    raw JSONs of phases 1-7."""
    build_tool = _safe_load(state_dir / "build-tool-contract.json")
    archetype = _safe_load(state_dir / "archetype-profile.json")
    stack = _safe_load(state_dir / "stack-profile.json")
    classification = _safe_load(state_dir / "classification-index.json")
    dep_graph = _safe_load(state_dir / "dependency-graph.json")
    fixtures = _safe_load(state_dir / "fixture-catalog.json")
    batch_plan = _safe_load(state_dir / "batch-plan.json")

    contracts_dir = state_dir / "symbol-contracts"
    contracts_count = sum(1 for _ in contracts_dir.glob("*.json")) if contracts_dir.exists() else 0
    packs_dir = state_dir / "context-packs-compact"
    packs_count = sum(1 for _ in packs_dir.glob("*.json")) if packs_dir.exists() else 0

    # Count per-classification bucket (tiny — no risk of bloat).
    class_buckets: dict[str, int] = {}
    for c in classification.get("classes", []) or []:
        if not isinstance(c, dict):
            continue
        t = str(c.get("type", "unknown"))
        class_buckets[t] = class_buckets.get(t, 0) + 1

    archetype_mod = _first_module(archetype)
    stack_mod = _first_module(stack)
    parent = archetype_mod.get("parent", {}) if isinstance(archetype_mod.get("parent"), dict) else {}
    implies = archetype_mod.get("implies", {}) if isinstance(archetype_mod.get("implies"), dict) else {}
    test_blk = stack_mod.get("test", {}) if isinstance(stack_mod.get("test"), dict) else {}
    mock_blk = stack_mod.get("mock", {}) if isinstance(stack_mod.get("mock"), dict) else {}
    assert_blk = stack_mod.get("assert", {}) if isinstance(stack_mod.get("assert"), dict) else {}
    di_blk = stack_mod.get("di", {}) if isinstance(stack_mod.get("di"), dict) else {}

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase": "PRE_GENERATION",
        "status": "READY",
        "buildTool": {
            "type": build_tool.get("tool", "") or stack.get("buildTool", ""),
            "groupId": parent.get("groupId", ""),
            "javaVersion": build_tool.get("java", "") or stack.get("java", ""),
        },
        "archetype": {
            "parent": parent.get("artifactId", ""),
            "namespace": stack_mod.get("namespace", "") or implies.get("namespace", ""),
        },
        "stack": {
            "testFramework": test_blk.get("framework", ""),
            "mockingLib": mock_blk.get("framework", ""),
            "assertionLib": assert_blk.get("framework", ""),
            "diFramework": "spring" if di_blk.get("spring") else "",
            "springBoot": di_blk.get("springBoot", ""),
            "blocked": bool(stack.get("blocked", False)),
        },
        "counts": {
            "symbolContracts": contracts_count,
            "contextPacks": packs_count,
            "fixtures": len(fixtures.get("fixtures", []) or []),
            "dependencyGraphs": len(dep_graph.get("graphs", []) or []),
            "classes": sum(class_buckets.values()),
        },
        "classification": class_buckets,
        "batchPlan": {
            "cycle": batch_plan.get("cycle", 0),
            "mode": batch_plan.get("mode", ""),
            "size": batch_plan.get("sizeChosen", 0),
            "topSuts": _top_suts(batch_plan, limit=10),
        },
        "llmInstructions": [
            "Phase 0 + phases 1-7 already validated by validate_handoff.py.",
            "DO NOT re-read build-tool-contract.json, archetype-profile.json, "
            "generated-code-index.json, import-whitelist.json, stack-profile.json, "
            "classification-index.json, dependency-graph.json, fixture-catalog.json "
            "or batch-plan.json — every fact you need is in this summary.",
            "Proceed directly to Phase 8 (Generation): consume "
            "state/context-packs-compact/<safe_fqcn>.json for each SUT in batchPlan.topSuts.",
            "Token budget: load ONLY the skills for the active phase + mode "
            f"(mode={batch_plan.get('mode', '') or 'coverage'}); do NOT load all "
            "skills/11-quality/*.md at once (~15K tokens). branch-coverage → add the "
            "boundary/null-value fixture skills; mutation-hardening → the "
            "assertion-strengthening skills. Per-SUT estimatedTokensIn / overBudget "
            "live in state/_summaries/llm-budget.json.",
        ],
    }


def _emit_summary(out_path: Path, payload: dict) -> None:
    """Validate the handoff summary against its schema, then write atomically.

    A schema violation means build_summary() drifted from
    protocols/handoff-summary.schema.json. We log it loudly but still emit the
    summary so the orchestrator is never left without a handoff to read.
    """
    try:
        validate("protocols/handoff-summary", payload)
    except Exception as e:
        print(
            "[WARN] handoff-summary schema violation (emitting anyway): "
            f"{str(e).splitlines()[0][:200]}",
            file=sys.stderr,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_path, payload)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Validate that Phase 0 + phases 1-7 (Discovery → Planning) are "
            "complete and emit a compact handoff summary for the LLM to "
            "consume in lieu of those seven phases."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--state",
        required=True,
        help="State directory produced by run_pipeline.py (e.g. state/)",
    )
    ap.add_argument(
        "--print",
        action="store_true",
        dest="print_summary",
        help="Print the summary JSON to stdout (default: write to state/_summaries/handoff-summary.json)",
    )
    args = ap.parse_args()

    state_dir = Path(args.state).resolve()
    if not state_dir.exists():
        print(f"[FAIL] state directory not found: {state_dir}", file=sys.stderr)
        return 2

    # This gate's entire job is schema validation. common.validate() silently
    # no-ops when jsonschema is not importable, which would make
    # BLOCKED_PRE_STAGE_INVALID impossible to raise and certify a handoff that
    # was never actually validated (audit M-1). Fail loudly instead of passing
    # by omission. jsonschema is pinned in tools/python/requirements.txt.
    try:
        import jsonschema  # noqa: F401
    except Exception:
        print(
            "[FAIL] jsonschema is not importable — schema validation would be a "
            "silent no-op, so the handoff cannot be certified. Install deps: "
            "pip install -r tools/python/requirements.txt",
            file=sys.stderr,
        )
        return 2

    missing = _check_required(state_dir)
    if missing:
        print("[BLOCKED] BLOCKED_PRE_STAGE_MISSING", file=sys.stderr)
        for m in missing:
            print(f"  - missing {m}", file=sys.stderr)
        # Persist the failure too so the orchestrator can surface it.
        payload = {
            "schemaVersion": 1,
            "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": "PRE_GENERATION",
            "status": "BLOCKED_PRE_STAGE_MISSING",
            "missing": missing,
        }
        _emit_summary(state_dir / "_summaries" / "handoff-summary.json", payload)
        return 2

    invalid = _validate_required(state_dir)
    if invalid:
        print("[BLOCKED] BLOCKED_PRE_STAGE_INVALID", file=sys.stderr)
        for m in invalid:
            print(f"  - {m}", file=sys.stderr)
        payload = {
            "schemaVersion": 1,
            "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": "PRE_GENERATION",
            "status": "BLOCKED_PRE_STAGE_INVALID",
            "invalid": invalid,
        }
        _emit_summary(state_dir / "_summaries" / "handoff-summary.json", payload)
        return 2

    summary = build_summary(state_dir)
    out_path = state_dir / "_summaries" / "handoff-summary.json"
    _emit_summary(out_path, summary)

    if args.print_summary:
        json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")

    counts = summary["counts"]
    bp = summary["batchPlan"]
    print(
        f"[OK] handoff ready: {counts['symbolContracts']} contracts, "
        f"{counts['contextPacks']} packs, batch={bp['size']} (mode={bp['mode']})"
    )
    return 0


if __name__ == "__main__":
    with _TimedRun("validate_handoff") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
