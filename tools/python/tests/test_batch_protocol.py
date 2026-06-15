"""test_batch_protocol.py — pure logic of the incremental batch handoff.

Locks the deterministic contract of orchestrator/batch_protocol.py:
  * select_batch caps at batch_size and skips already-processed targets
  * the generation request carries at most batch_size targets + the rules
  * response validation: a skipped/failed item does NOT break the batch, but an
    unknown targetId (not in this batch) is rejected
  * the per-target state machine + manifest totals roll up correctly
  * the repair request includes ONLY the failed items
  * a target that exhausts maxRepairRounds is flagged for ABANDON
  * advance_decision applies the 80% / 50% pass-rate rules

Legacy-suite convention: expose ``main() -> int`` (0 = ok). Auto-discovered by
test_aa_suite_runner.py. Run standalone:
    python tools/python/tests/test_batch_protocol.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # repo root → import orchestrator.*

from orchestrator import batch_protocol as bp  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(label + (f" — {detail}" if detail else ""))


def _plan(n: int) -> list[dict]:
    return [{"targetId": f"com.acme.C{i}#m", "sut": f"com.acme.C{i}", "method": "m",
             "score": 100 - i} for i in range(n)]


# ── select_batch ────────────────────────────────────────────────────────────────

def _patch(sut: str, *, prefix: str = "patch") -> dict:
    return {
        "schemaVersion": 1,
        "patchId": f"{prefix}:abcdef",
        "cycle": 1,
        "sut": sut,
        "testClass": f"{sut}Test",
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


def case_select_caps_at_batch_size() -> None:
    got = bp.select_batch(_plan(25), set(), 10)
    _assert("select caps at batch_size", len(got) == 10, f"len={len(got)}")


def case_select_skips_processed() -> None:
    plan = _plan(5)
    processed = {"com.acme.C0#m", "com.acme.C1#m"}
    got = [i["targetId"] for i in bp.select_batch(plan, processed, 10)]
    _assert("select skips processed", got == ["com.acme.C2#m", "com.acme.C3#m", "com.acme.C4#m"], str(got))


# ── build_generation_request ────────────────────────────────────────────────────

def case_request_has_at_most_batch_size_targets() -> None:
    targets = bp.select_batch(_plan(25), set(), 10)
    req = bp.build_generation_request("run-1", "batch-001", targets, batch_size=10)
    _assert("request schemaVersion", req["schemaVersion"] == bp.SCHEMA_GENERATION_REQUEST)
    _assert("request role generation", req["role"] == "generation")
    _assert("request ≤ batch_size targets", len(req["targets"]) == 10, str(len(req["targets"])))
    t0 = req["targets"][0]
    _assert("target has productionFile", t0["productionFile"].startswith("src/main/java/"))
    _assert("target has suggestedTestFile", t0["suggestedTestFile"].endswith("Test.java"))
    _assert("target has canonicalTestClass", t0["canonicalTestClass"] == "com.acme.C0Test")
    _assert("request ships rules", isinstance(req["rules"], list) and len(req["rules"]) >= 5)


# ── validate_generation_response ─────────────────────────────────────────────────

def case_response_skipped_item_does_not_break_batch() -> None:
    targets = bp.select_batch(_plan(2), set(), 10)
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [
            {"targetId": "com.acme.C0#m", "status": "generated",
             "patchDescriptor": _patch("com.acme.C0")},
            {"targetId": "com.acme.C1#m", "status": "skipped",
             "reason": "requires external service"},
        ],
    }
    try:
        items = bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("skipped item validates", len(items) == 2)
    except bp.BatchResponseError as e:
        _assert("skipped item validates", False, str(e))


def case_response_unknown_target_rejected() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.NOT_IN_BATCH#m", "status": "skipped"}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("unknown target rejected", False, "did not raise")
    except bp.BatchResponseError:
        _assert("unknown target rejected", True)


def case_response_generated_without_patch_rejected() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.C0#m", "status": "generated"}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("generated-without-patch rejected", False, "did not raise")
    except bp.BatchResponseError:
        _assert("generated-without-patch rejected", True)


# ── manifest + state machine ─────────────────────────────────────────────────────

def case_response_full_file_patch_rejected_before_patcher() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{
            "targetId": "com.acme.C0#m",
            "status": "generated",
            "patchDescriptor": {
                "operation": "create",
                "targetFile": "src/test/java/com/acme/C0Test.java",
                "language": "java",
                "content": "class C0Test {}",
            },
        }],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("full-file patch rejected before patcher", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("full-file patch rejected before patcher", "full-file patch keys" in str(exc), str(exc))


def case_response_patch_missing_methods_rejected() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    bad_patch = _patch("com.acme.C0")
    bad_patch.pop("methods")
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                   "patchDescriptor": bad_patch}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("patch missing methods rejected", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("patch missing methods rejected", "missing required keys" in str(exc), str(exc))


def case_response_noncanonical_test_class_rejected() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    bad_patch = _patch("com.acme.C0")
    bad_patch["testClass"] = "com.acme.C0CtorTest"
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                   "patchDescriptor": bad_patch}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("noncanonical generation testClass rejected", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("noncanonical generation testClass rejected", "must be canonical" in str(exc), str(exc))


def case_repair_patch_must_use_repair_prefix() -> None:
    resp = {
        "schemaVersion": bp.SCHEMA_REPAIR_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "repair", "repairRound": 1,
        "items": [{"targetId": "com.acme.C0#m", "status": "repaired",
                   "patchDescriptor": _patch("com.acme.C0", prefix="patch")}],
    }
    try:
        bp.validate_repair_response(resp, {"com.acme.C0#m"}, batch_id="batch-001", repair_round=1)
        _assert("repair patch must use repair prefix", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("repair patch must use repair prefix", "must start with 'repair:'" in str(exc), str(exc))


def case_repair_noncanonical_test_class_rejected() -> None:
    patch = _patch("com.acme.C0", prefix="repair")
    patch["testClass"] = "com.acme.C0CtorTest"
    resp = {
        "schemaVersion": bp.SCHEMA_REPAIR_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "repair", "repairRound": 1,
        "items": [{"targetId": "com.acme.C0#m", "status": "repaired",
                   "patchDescriptor": patch}],
    }
    requested = [{"targetId": "com.acme.C0#m", "sut": "com.acme.C0",
                  "canonicalTestClass": "com.acme.C0Test"}]
    try:
        bp.validate_repair_response(
            resp,
            {"com.acme.C0#m"},
            batch_id="batch-001",
            repair_round=1,
            requested_items=requested,
        )
        _assert("noncanonical repair testClass rejected", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("noncanonical repair testClass rejected", "must be canonical" in str(exc), str(exc))


def case_state_transitions_update_totals() -> None:
    m = bp.new_manifest("run-1", "/repo", generation_mode="handoff-batch",
                        batch_size=10, max_repair_rounds=2)
    for i in range(4):
        bp.ensure_target(m, f"t{i}", sut=f"S{i}", batch_id="batch-001")
    bp.set_status(m, "t0", bp.PASSED)
    bp.set_status(m, "t1", bp.SKIPPED)
    bp.set_status(m, "t2", bp.TEST_FAILED)
    bp.set_status(m, "t3", bp.ABANDONED)
    tot = m["totals"]
    _assert("totals passed", tot["passed"] == 1, str(tot))
    _assert("totals skipped", tot["skipped"] == 1, str(tot))
    _assert("totals failed", tot["failed"] == 1, str(tot))
    _assert("totals abandoned", tot["abandoned"] == 1, str(tot))
    _assert("t2 status persisted", m["targets"]["t2"]["status"] == bp.TEST_FAILED)


def case_failing_ids_order_preserving() -> None:
    m = bp.new_manifest("run-1", "/repo", generation_mode="handoff-batch",
                        batch_size=10, max_repair_rounds=2)
    ids = ["a", "b", "c"]
    for t in ids:
        bp.ensure_target(m, t)
    bp.set_status(m, "a", bp.PASSED)
    bp.set_status(m, "b", bp.COMPILE_FAILED)
    bp.set_status(m, "c", bp.TEST_FAILED)
    _assert("failing ids only failures, in order",
            bp.failing_target_ids(m, ids) == ["b", "c"])


# ── repair request + abandon ─────────────────────────────────────────────────────

def case_repair_request_only_failed_items() -> None:
    failed = [
        {"targetId": "b", "failureKind": "COMPILATION_ERROR", "testFile": "BTest.java",
         "errorSummary": "unclosed string literal"},
    ]
    req = bp.build_repair_request("run-1", "batch-001", 1, failed)
    _assert("repair schemaVersion", req["schemaVersion"] == bp.SCHEMA_REPAIR_REQUEST)
    _assert("repair role", req["role"] == "repair")
    _assert("repair round", req["repairRound"] == 1)
    _assert("repair only failed", [i["targetId"] for i in req["failedItems"]] == ["b"])


def case_abandon_after_max_rounds() -> None:
    m = bp.new_manifest("run-1", "/repo", generation_mode="handoff-batch",
                        batch_size=10, max_repair_rounds=2)
    bp.ensure_target(m, "x")
    _assert("not abandoned at 0 rounds", not bp.should_abandon(m, "x", 2))
    bp.bump_repair_round(m, "x")
    _assert("not abandoned at 1 round", not bp.should_abandon(m, "x", 2))
    bp.bump_repair_round(m, "x")
    _assert("abandoned at 2 rounds", bp.should_abandon(m, "x", 2))


# ── advance_decision ─────────────────────────────────────────────────────────────

def case_advance_rules() -> None:
    _assert("100% → continue", bp.advance_decision(10, 10)["action"] == bp.ADVANCE_CONTINUE)
    _assert("80% → repair-then-continue",
            bp.advance_decision(8, 10)["action"] == bp.ADVANCE_REPAIR_THEN_CONTINUE)
    _assert("60% → repair-then-continue",
            bp.advance_decision(6, 10)["action"] == bp.ADVANCE_REPAIR_THEN_CONTINUE)
    _assert("40% → stop", bp.advance_decision(4, 10)["action"] == bp.ADVANCE_STOP)
    _assert("global compile error → repair-then-continue",
            bp.advance_decision(10, 10, had_global_compile_error=True)["action"]
            == bp.ADVANCE_REPAIR_THEN_CONTINUE)


def main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("case_")]
    for c in cases:
        try:
            c()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{c.__name__} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print("FAIL test_batch_protocol:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK   test_batch_protocol: {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
