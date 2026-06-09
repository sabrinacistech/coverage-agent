"""batch_protocol.py — pure logic for the incremental batch handoff (no I/O).

The batch handoff turns the old "1 target → 1 request → wait → 1 response →
apply" loop into "up to N targets → 1 batch request → 1 batch response → apply
all → repair only the failures". This module owns the *deterministic* pieces so
they are unit-testable in isolation, free of disk/Maven/handoff side effects:

  * target selection (first N still-pending plan items)
  * the generation request envelope (schema test-generation-batch-v1)
  * validation of the generation response (unknown target ⇒ reject; a per-item
    `skipped`/`failed` never fails the whole batch)
  * the per-target state machine + run manifest with rolled-up totals
  * the between-batches advance decision (the 80% / 50% rules)
  * the repair request envelope (only the failed items) + abandon-after-N rounds

batch_runner.py is the thin I/O shell that wires these to the file handoff, the
test_patch_applier and the test runner. Everything here is a pure function over
plain dicts.
"""
from __future__ import annotations

from typing import Any

# ── Schema version tags (the file protocol's contract) ─────────────────────────
SCHEMA_GENERATION_REQUEST = "test-generation-batch-v1"
SCHEMA_GENERATION_RESPONSE = "test-generation-batch-response-v1"
SCHEMA_REPAIR_REQUEST = "test-repair-batch-v1"
SCHEMA_REPAIR_RESPONSE = "test-repair-batch-response-v1"
SCHEMA_MANIFEST = "test-batch-manifest-v1"

# ── Per-target lifecycle states (manifest.targets[id].status) ──────────────────
PENDING = "PENDING"
GENERATION_REQUESTED = "GENERATION_REQUESTED"
GENERATED = "GENERATED"
SKIPPED = "SKIPPED"
GENERATION_FAILED = "GENERATION_FAILED"
APPLIED = "APPLIED"
PATCH_FAILED = "PATCH_FAILED"
COMPILE_FAILED = "COMPILE_FAILED"
TEST_FAILED = "TEST_FAILED"
REPAIR_REQUESTED = "REPAIR_REQUESTED"
REPAIRED = "REPAIRED"
PASSED = "PASSED"
ABANDONED = "ABANDONED"

# Terminal states never get re-selected or repaired again.
TERMINAL_STATES = frozenset({PASSED, SKIPPED, ABANDONED})
# States that still count as "failed and repairable" (a repair round may fix them).
REPAIRABLE_STATES = frozenset({COMPILE_FAILED, TEST_FAILED, PATCH_FAILED})

# Item-level statuses inside the LLM responses.
_GEN_ITEM_STATUSES = frozenset({"generated", "skipped", "failed"})
_REPAIR_ITEM_STATUSES = frozenset({"repaired", "skipped", "abandoned", "failed"})

# ── Generation / repair rules shipped to Claude Code in every request ──────────
# The QUALITY_GATE_RULES mirror the deterministic G6 linter (tools/python/
# test_linter.py, skills/11-quality). A test that breaks any of these is ROLLED
# BACK by the patcher (G6_LINTER_FAIL) and bounced to a repair round — so stating
# them up front is what makes a batch pass on the first handoff instead of after
# one or two repair rounds. Keep this list in sync with test_linter's TQG_* kinds.
QUALITY_GATE_RULES = [
    # TQG_02_NO_AAA — every @Test body must contain the three AAA marker comments.
    "Every @Test method body MUST contain the three Arrange/Act/Assert marker "
    "comments, literally: `// given`, `// when`, `// then`.",
    # TQG_11_EAGER_TEST — exactly one `// when` per test.
    "Use exactly ONE `// when` marker per test method (one action under test); "
    "split multiple actions into separate @Test methods.",
    # TQG_03_NAMING — method names must match one of the two accepted forms.
    "Name every @Test method as either `shouldX_whenY` (e.g. "
    "`shouldReturnUnknown_whenInputBlank`) OR three lowercase-led snake_case parts "
    "`method_condition_expected` (e.g. `getId_afterSetId_returnsValue`). No other "
    "shape passes the naming gate.",
    # TQG_11_NON_DETERMINISTIC — no wall-clock / randomness / sleeps.
    "Never call non-deterministic APIs in a test: no Instant.now(), "
    "LocalDate/LocalDateTime.now(), System.currentTimeMillis()/nanoTime(), "
    "Math.random(), UUID.randomUUID(), or Thread.sleep(...). Use fixed values "
    "(e.g. Instant.parse(\"2026-01-01T00:00:00Z\")).",
    # TQG_12_OVER_MOCK / _SUT — never mock the class under test or value objects.
    "Never mock the system under test (no @Mock/@Spy on the SUT type) nor value "
    "objects/DTOs/enums/collections; construct them directly.",
    # TQG_12_TAUTOLOGY — assert real behaviour.
    "Assert the real behaviour; never assertTrue(true)/assertFalse(false) or "
    "assert a value against itself.",
    # TQG_09_LOGIC_IN_TEST — no control flow in test bodies.
    "No control flow inside a test body (no if/for/while/switch); keep tests "
    "straight-line Arrange/Act/Assert.",
]

GENERATION_RULES = [
    "Generate Java tests that compile.",
    "Do not modify production code.",
    "Do not add dependencies unless explicitly authorized.",
    "Use Arrange / Act / Assert.",
    "Use the JUnit/Mockito/assertion framework already used by the project.",
    "Do not invent expected outputs; derive them from source behaviour, existing "
    "tests, the symbol contract, execution feedback, or explicit evidence.",
    "Prefer small deterministic unit tests; mock external dependencies.",
    "Avoid starting a full Spring context unless strictly necessary.",
    "Escape Java string literals correctly: \\n \\r \\t \\\\ \\\" — never a raw "
    "newline/tab inside a normal String literal. If you need a control character "
    "in test input, prefer building it explicitly (e.g. \"a\" + (char) 10 + \"b\").",
    "For sanitizers/encoders/maskers/normalizers/parsers include edge cases when "
    "applicable: null, blank, newline, tab, CR, quotes, backslash, angle brackets, "
    "unicode/non-ASCII, already-sanitized input, very long input.",
    *QUALITY_GATE_RULES,
]
REPAIR_RULES = [
    "Do not modify production code; fix only the generated tests.",
    "Keep the original test intent.",
    "Prefer minimal changes.",
    "If an expected value is wrong, infer it from the source behaviour.",
    "If a Java string literal is invalid, escape it (\\n \\r \\t \\\\ \\\").",
    *QUALITY_GATE_RULES,
]


# ── Target selection ────────────────────────────────────────────────────────────

def select_batch(plan_items: list[dict], processed_ids: set[str], batch_size: int) -> list[dict]:
    """First ``batch_size`` plan items whose targetId is not already processed.

    Order is preserved (the planner already sorted by descending coverage score),
    so the highest-value, lowest-risk targets fill the early batches.
    """
    out: list[dict] = []
    for item in plan_items:
        tid = item.get("targetId")
        if tid and tid not in processed_ids:
            out.append(item)
            if len(out) >= batch_size:
                break
    return out


# ── Generation request envelope ─────────────────────────────────────────────────

def _suggested_test_file(sut: str) -> str:
    """Conventional test path for a SUT FQCN (src/test/java/<pkg>/<Name>Test.java)."""
    return "src/test/java/" + sut.replace(".", "/") + "Test.java"


def _production_file(sut: str) -> str:
    return "src/main/java/" + sut.replace(".", "/") + ".java"


def build_generation_request(
    run_id: str,
    batch_id: str,
    targets: list[dict],
    *,
    batch_size: int,
    mode: str = "coverage",
) -> dict:
    """Build the batch generation request (schema test-generation-batch-v1).

    One request carries up to ``batch_size`` targets. Each target projects the
    plan item into the fields the generator needs; ``context`` stays a thin dict
    the runner can enrich with snippets/evidence without changing this shape.
    """
    out_targets = []
    for i, item in enumerate(targets, start=1):
        sut = item.get("sut", "")
        out_targets.append({
            "targetId": item.get("targetId", sut),
            "sut": sut,
            "method": item.get("method", ""),
            "productionFile": _production_file(sut) if sut else "",
            "suggestedTestFile": _suggested_test_file(sut) if sut else "",
            "template": item.get("template"),
            "priority": item.get("score", i),
            "fixtureIds": item.get("fixtureIds", []),
            "context": item.get("context", {}),
        })
    return {
        "schemaVersion": SCHEMA_GENERATION_REQUEST,
        "runId": run_id,
        "batchId": batch_id,
        "role": "generation",
        "mode": mode,
        "batchSize": batch_size,
        "targets": out_targets,
        "rules": list(GENERATION_RULES),
        "expectedResponse": {
            "schemaVersion": SCHEMA_GENERATION_RESPONSE,
            "runId": run_id,
            "batchId": batch_id,
            "role": "generation",
            "items": [
                {"targetId": t["targetId"], "status": "generated|skipped|failed",
                 "patchDescriptor": {}}
                for t in out_targets
            ],
        },
    }


# ── Generation response validation ──────────────────────────────────────────────

class BatchResponseError(ValueError):
    """The batch response does not satisfy the minimal protocol contract."""


def validate_generation_response(resp: dict, batch_targets: list[dict], *, batch_id: str) -> list[dict]:
    """Validate a generation response against the batch it answers.

    Minimal-schema checks (raise BatchResponseError on a structural breach):
      * schemaVersion / role / batchId match the request,
      * ``items`` is a list,
      * every item names a target that belongs to THIS batch (unknown ⇒ reject),
      * each item.status ∈ {generated, skipped, failed},
      * a ``generated`` item carries a non-empty patchDescriptor.

    A per-item ``skipped``/``failed`` is VALID — it must not fail the batch; the
    caller maps it to SKIPPED / GENERATION_FAILED. Returns the validated items.
    """
    if not isinstance(resp, dict):
        raise BatchResponseError("response is not a JSON object")
    if resp.get("schemaVersion") != SCHEMA_GENERATION_RESPONSE:
        raise BatchResponseError(
            f"schemaVersion must be {SCHEMA_GENERATION_RESPONSE!r}, got {resp.get('schemaVersion')!r}")
    if resp.get("role") != "generation":
        raise BatchResponseError(f"role must be 'generation', got {resp.get('role')!r}")
    if resp.get("batchId") != batch_id:
        raise BatchResponseError(f"batchId mismatch: expected {batch_id!r}, got {resp.get('batchId')!r}")
    items = resp.get("items")
    if not isinstance(items, list):
        raise BatchResponseError("items must be a list")

    known = {t.get("targetId") for t in batch_targets}
    for it in items:
        if not isinstance(it, dict):
            raise BatchResponseError("each item must be an object")
        tid = it.get("targetId")
        if tid not in known:
            raise BatchResponseError(f"unknown targetId not in this batch: {tid!r}")
        status = it.get("status")
        if status not in _GEN_ITEM_STATUSES:
            raise BatchResponseError(f"invalid item status {status!r} for {tid!r}")
        if status == "generated" and not it.get("patchDescriptor"):
            raise BatchResponseError(f"'generated' item {tid!r} lacks a patchDescriptor")
    return items


# ── Repair request envelope ──────────────────────────────────────────────────────

def build_repair_request(
    run_id: str,
    batch_id: str,
    repair_round: int,
    failed_items: list[dict],
) -> dict:
    """Build a repair request (schema test-repair-batch-v1) for the FAILED items only.

    ``failed_items`` are pre-shaped dicts: targetId, failureKind, testFile, line,
    errorSummary, buildOutput, currentTestSource. Empty ⇒ caller must not request
    repair (kept caller-side so this stays a pure builder).
    """
    return {
        "schemaVersion": SCHEMA_REPAIR_REQUEST,
        "runId": run_id,
        "batchId": batch_id,
        "role": "repair",
        "repairRound": repair_round,
        "failedItems": list(failed_items),
        "rules": list(REPAIR_RULES),
        "expectedResponse": {
            "schemaVersion": SCHEMA_REPAIR_RESPONSE,
            "runId": run_id,
            "batchId": batch_id,
            "role": "repair",
            "repairRound": repair_round,
            "items": [
                {"targetId": f.get("targetId"), "status": "repaired|skipped|abandoned|failed",
                 "patchDescriptor": {}}
                for f in failed_items
            ],
        },
    }


def validate_repair_response(resp: dict, requested_ids: set[str], *, batch_id: str, repair_round: int) -> list[dict]:
    """Validate a repair response. Same contract as generation, with repair statuses."""
    if not isinstance(resp, dict):
        raise BatchResponseError("response is not a JSON object")
    if resp.get("schemaVersion") != SCHEMA_REPAIR_RESPONSE:
        raise BatchResponseError(
            f"schemaVersion must be {SCHEMA_REPAIR_RESPONSE!r}, got {resp.get('schemaVersion')!r}")
    if resp.get("role") != "repair":
        raise BatchResponseError(f"role must be 'repair', got {resp.get('role')!r}")
    if resp.get("batchId") != batch_id:
        raise BatchResponseError(f"batchId mismatch: expected {batch_id!r}, got {resp.get('batchId')!r}")
    items = resp.get("items")
    if not isinstance(items, list):
        raise BatchResponseError("items must be a list")
    for it in items:
        if not isinstance(it, dict):
            raise BatchResponseError("each item must be an object")
        tid = it.get("targetId")
        if tid not in requested_ids:
            raise BatchResponseError(f"repair item for a target not requested: {tid!r}")
        status = it.get("status")
        if status not in _REPAIR_ITEM_STATUSES:
            raise BatchResponseError(f"invalid repair status {status!r} for {tid!r}")
        if status == "repaired" and not it.get("patchDescriptor"):
            raise BatchResponseError(f"'repaired' item {tid!r} lacks a patchDescriptor")
    return items


# ── Manifest + per-target state machine ─────────────────────────────────────────

def new_manifest(run_id: str, repo: str, *, generation_mode: str, batch_size: int, max_repair_rounds: int) -> dict:
    return {
        "schemaVersion": SCHEMA_MANIFEST,
        "runId": run_id,
        "repo": repo,
        "generationMode": generation_mode,
        "batchSize": batch_size,
        "maxRepairRounds": max_repair_rounds,
        "status": "RUNNING",
        "batches": [],
        "targets": {},
        "totals": {},
    }


def ensure_target(manifest: dict, target_id: str, *, sut: str = "", batch_id: str | None = None) -> dict:
    """Get (creating if needed) the per-target record, defaulting to PENDING."""
    rec = manifest.setdefault("targets", {}).get(target_id)
    if rec is None:
        rec = {"status": PENDING, "sut": sut, "batchId": batch_id, "repairRounds": 0}
        manifest["targets"][target_id] = rec
    if sut and not rec.get("sut"):
        rec["sut"] = sut
    if batch_id and not rec.get("batchId"):
        rec["batchId"] = batch_id
    return rec


def set_status(manifest: dict, target_id: str, status: str, **fields: Any) -> dict:
    """Set a target's status (and optional fields), then recompute totals."""
    rec = ensure_target(manifest, target_id)
    rec["status"] = status
    for k, v in fields.items():
        if v is not None:
            rec[k] = v
    recompute_totals(manifest)
    return rec


def bump_repair_round(manifest: dict, target_id: str) -> int:
    rec = ensure_target(manifest, target_id)
    rec["repairRounds"] = int(rec.get("repairRounds", 0)) + 1
    return rec["repairRounds"]


def should_abandon(manifest: dict, target_id: str, max_repair_rounds: int) -> bool:
    """True once a target has consumed all its repair rounds and is still failing."""
    rec = ensure_target(manifest, target_id)
    return int(rec.get("repairRounds", 0)) >= max_repair_rounds


# How each lifecycle state rolls up into the manifest totals buckets.
_TOTAL_BUCKET = {
    PENDING: "pending", GENERATION_REQUESTED: "pending",
    GENERATED: "generated", APPLIED: "generated",
    REPAIR_REQUESTED: "generated", REPAIRED: "generated",
    PASSED: "passed",
    COMPILE_FAILED: "failed", TEST_FAILED: "failed",
    PATCH_FAILED: "failed", GENERATION_FAILED: "failed",
    SKIPPED: "skipped",
    ABANDONED: "abandoned",
}


def recompute_totals(manifest: dict) -> dict:
    totals = {"pending": 0, "generated": 0, "passed": 0, "failed": 0,
              "skipped": 0, "abandoned": 0}
    for rec in manifest.get("targets", {}).values():
        bucket = _TOTAL_BUCKET.get(rec.get("status"), "pending")
        totals[bucket] += 1
    manifest["totals"] = totals
    return totals


def failing_target_ids(manifest: dict, batch_target_ids: list[str]) -> list[str]:
    """IDs in this batch currently in a repairable failed state, order-preserving."""
    targets = manifest.get("targets", {})
    return [tid for tid in batch_target_ids
            if targets.get(tid, {}).get("status") in REPAIRABLE_STATES]


# ── Between-batches advance decision (the 80% / 50% rules) ───────────────────────

# action ∈ these; the runner reads it to decide what to do after a batch.
ADVANCE_CONTINUE = "continue"          # batch healthy, go to the next batch
ADVANCE_REPAIR_THEN_CONTINUE = "repair-then-continue"
ADVANCE_STOP = "stop"                  # too many failures / global compile error


def advance_decision(passed: int, total: int, *, had_global_compile_error: bool = False) -> dict:
    """Decide what to do after applying+testing a batch.

    Rules (section 7):
      * global compile error → never advance until a repair round runs.
      * pass rate ≥ 80%      → repair the failures, then continue.
      * 50% ≤ pass rate < 80% → repair before continuing.
      * pass rate < 50%      → STOP and recommend a smaller --batch-size.
    A 100%-pass batch needs no repair → continue.
    """
    rate = (passed / total) if total else 1.0
    if had_global_compile_error:
        return {"action": ADVANCE_REPAIR_THEN_CONTINUE, "rate": rate,
                "reason": "global compilation error — repair before advancing"}
    if passed == total:
        return {"action": ADVANCE_CONTINUE, "rate": rate, "reason": "all targets passed"}
    if rate >= 0.80:
        return {"action": ADVANCE_REPAIR_THEN_CONTINUE, "rate": rate,
                "reason": "pass rate ≥ 80% — repair failures, then continue"}
    if rate >= 0.50:
        return {"action": ADVANCE_REPAIR_THEN_CONTINUE, "rate": rate,
                "reason": "pass rate 50–80% — repair before continuing"}
    return {"action": ADVANCE_STOP, "rate": rate,
            "reason": "pass rate < 50% — stopping; consider a smaller --batch-size"}
