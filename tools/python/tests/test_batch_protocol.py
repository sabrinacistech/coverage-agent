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
             "score": 100 - i,
             "allowedImports": ["org.junit.jupiter.api.Test"],
             "allowedEvidenceIds": [f"sym:com.acme.C{i}#m:12345678"]} for i in range(n)]


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
            "evidenceIds": [f"sym:{sut}#m:12345678"],
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


def case_request_is_self_contained_payload() -> None:
    # Hermetic payload: the isolation rule + self-contained policy + pass-through
    # of sutSourceCode / dependencySignatures injected by the runner.
    targets = bp.select_batch(_plan(1), set(), 10)
    targets[0]["sutSourceCode"] = "public class C0 {}"
    targets[0]["sutSourceTruncated"] = False
    targets[0]["dependencySignatures"] = [{"fqcn": "com.acme.Repo", "signatures": ["String lookup(String k)"]}]
    req = bp.build_generation_request("run-1", "batch-001", targets, batch_size=10)
    _assert("isolation rule is first generation rule",
            req["rules"][0] == bp.SELF_CONTAINED_RULE)
    _assert("request has selfContainedPolicy",
            "READ_SOURCE_CODE" in req["selfContainedPolicy"]["forbiddenActions"])
    t0 = req["targets"][0]
    _assert("target passes through sutSourceCode", t0["sutSourceCode"] == "public class C0 {}")
    _assert("target passes through dependencySignatures",
            t0["dependencySignatures"][0]["fqcn"] == "com.acme.Repo")
    # Repair request carries the same isolation contract.
    rreq = bp.build_repair_request("run-1", "batch-001", 1, [{"targetId": "com.acme.C0#m"}])
    _assert("isolation rule is first repair rule",
            rreq["rules"][0] == bp.SELF_CONTAINED_RULE)
    _assert("repair request has selfContainedPolicy",
            "failedItem.currentTestSource" in rreq["selfContainedPolicy"]["authoritativeFields"])


def case_request_completion_contract_no_patch_descriptor() -> None:
    # New contract: the LLM returns a minimal completion; Python hydrates the
    # patchDescriptor. The request must NOT ask for a patchDescriptor and must ship
    # the responseCompletionContract instead.
    targets = bp.select_batch(_plan(2), set(), 10)
    req = bp.build_generation_request("run-1", "batch-001", targets, batch_size=10)
    rcc = req.get("responseCompletionContract")
    _assert("request has responseCompletionContract", isinstance(rcc, dict), repr(rcc))
    _assert("completion contract schema", rcc["schemaVersion"] == "test-generation-completion-v1")
    _assert("completion itemShape has methods", "methods" in rcc["itemShape"])
    _assert("completion itemShape has no patchDescriptor",
            "patchDescriptor" not in rcc["itemShape"])
    for it in req["expectedResponse"]["items"]:
        _assert("expectedResponse item has no patchDescriptor",
                "patchDescriptor" not in it, repr(it))
        _assert("expectedResponse item has methods", "methods" in it, repr(it))


def case_rules_do_not_reference_patch_descriptor_dotpath() -> None:
    # No generation rule may still tell the model to build patchDescriptor.<field>
    # (that contradicts "do not return patchDescriptor").
    offenders = [r for r in bp.GENERATION_RULES if "patchdescriptor." in r.lower()]
    _assert("no rule references patchDescriptor.<field>", not offenders, repr(offenders))
    joined = " ".join(bp.GENERATION_RULES).lower()
    _assert("rules forbid returning patchDescriptor", "do not return a patchdescriptor" in joined)


# ── validate_generation_envelope (per-item split — hydration flow) ───────────────

def _gen_resp(*items: dict, batch_id: str = "batch-001") -> dict:
    return {"schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "role": "generation",
            "batchId": batch_id, "items": list(items)}


def case_envelope_accepts_well_formed_wrapper() -> None:
    items = bp.validate_generation_envelope(
        _gen_resp({"targetId": "x", "status": "generated", "methods": []}),
        batch_id="batch-001")
    _assert("envelope returns items", isinstance(items, list) and len(items) == 1, repr(items))


def case_envelope_rejects_structural_breaches() -> None:
    bad = [
        ("not an object", "nope"),
        ("schemaVersion", {"schemaVersion": "wrong", "role": "generation",
                           "batchId": "batch-001", "items": []}),
        ("role", {"schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "role": "repair",
                  "batchId": "batch-001", "items": []}),
        ("batchId", _gen_resp(batch_id="batch-999")),
        ("items not list", {"schemaVersion": bp.SCHEMA_GENERATION_RESPONSE,
                            "role": "generation", "batchId": "batch-001", "items": "x"}),
    ]
    for label, resp in bad:
        try:
            bp.validate_generation_envelope(resp, batch_id="batch-001")  # type: ignore[arg-type]
            _assert(f"envelope rejects {label}", False, "did not raise")
        except bp.BatchResponseError:
            _assert(f"envelope rejects {label}", True)


def case_envelope_does_not_require_patch_descriptor() -> None:
    # The new contract: a generated item with NO patchDescriptor and an unknown
    # targetId must NOT abort the batch at the envelope level (the hydrator decides
    # per item). This is the behavioural contrast with validate_generation_response.
    resp = _gen_resp({"targetId": "not-in-batch", "status": "generated", "methods": []})
    try:
        bp.validate_generation_envelope(resp, batch_id="batch-001")
        _assert("envelope ignores per-item issues", True)
    except bp.BatchResponseError as exc:
        _assert("envelope ignores per-item issues", False, str(exc))


# ── validate_generation_response (legacy/compat path) ────────────────────────────

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


def case_response_nonwhitelisted_import_rejected() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    bad_patch = _patch("com.acme.C0")
    bad_patch["allowedImports"] = ["org.junit.jupiter.api.Test", "org.junit.jupiter.api.DisplayName"]
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                   "patchDescriptor": bad_patch}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("nonwhitelisted import rejected", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("nonwhitelisted import rejected", "non-whitelisted import" in str(exc), str(exc))


def case_response_nonwhitelisted_annotation_rejected() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    bad_patch = _patch("com.acme.C0")
    bad_patch["methods"][0]["annotations"] = ["@Test", "@DisplayName(\"x\")"]
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                   "patchDescriptor": bad_patch}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("nonwhitelisted annotation rejected", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("nonwhitelisted annotation rejected", "uses annotation" in str(exc), str(exc))


def case_response_unknown_evidence_rejected() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    bad_patch = _patch("com.acme.C0")
    bad_patch["methods"][0]["evidenceIds"] = ["sym:com.acme.C0#ghost:deadbeef"]
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                   "patchDescriptor": bad_patch}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("unknown evidence rejected", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("unknown evidence rejected", "unknown evidenceId" in str(exc), str(exc))


def case_response_missing_target_evidence_rejected() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    targets[0]["targetEvidenceRequired"] = True
    targets[0]["targetEvidenceIds"] = []
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                   "patchDescriptor": _patch("com.acme.C0")}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("missing target evidence rejected", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("missing target evidence rejected", "targetEvidenceIds is empty" in str(exc), str(exc))


def case_response_must_cite_target_evidence() -> None:
    targets = bp.select_batch(_plan(1), set(), 10)
    targets[0]["allowedEvidenceIds"] = [
        "ctor:com.acme.C0:11111111",
        "sym:com.acme.C0#m:12345678",
    ]
    targets[0]["targetEvidenceRequired"] = True
    targets[0]["targetEvidenceIds"] = ["sym:com.acme.C0#m:12345678"]
    bad_patch = _patch("com.acme.C0")
    bad_patch["methods"][0]["evidenceIds"] = ["ctor:com.acme.C0:11111111"]
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                   "patchDescriptor": bad_patch}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("target evidence must be cited", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("target evidence must be cited", "targetEvidenceIds" in str(exc), str(exc))


def case_response_sut_body_call_requires_evidence_ref() -> None:
    targets = [{
        "targetId": "com.acme.MyException#<init>",
        "sut": "com.acme.MyException",
        "method": "<init>(Ljava/lang/String;)V",
        "allowedImports": ["org.junit.jupiter.api.Test"],
        "allowedEvidenceIds": ["ctor:com.acme.MyException:11111111"],
        "evidenceRefs": [{
            "evidenceId": "ctor:com.acme.MyException:11111111",
            "kind": "constructor",
            "name": "constructor",
            "params": [{"type": "java.lang.String"}],
        }],
    }]
    patch = _patch("com.acme.MyException")
    patch["methods"][0]["body"] = (
        "// given\nString message = \"missing\";\n"
        "// when\nMyException exception = new MyException(message);\n"
        "// then\norg.junit.jupiter.api.Assertions.assertEquals(message, exception.getMessage());"
    )
    patch["methods"][0]["evidenceIds"] = ["ctor:com.acme.MyException:11111111"]
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.MyException#<init>", "status": "generated",
                   "patchDescriptor": patch}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("sut body call requires evidence ref", False, "did not raise")
    except bp.BatchResponseError as exc:
        _assert("sut body call requires evidence ref", "getMessage" in str(exc), str(exc))


def case_response_sut_body_call_allowed_with_evidence_ref() -> None:
    targets = [{
        "targetId": "com.acme.MyException#<init>",
        "sut": "com.acme.MyException",
        "method": "<init>(Ljava/lang/String;)V",
        "allowedImports": ["org.junit.jupiter.api.Test"],
        "allowedEvidenceIds": [
            "ctor:com.acme.MyException:11111111",
            "sym:com.acme.MyException#getMessage:22222222",
        ],
        "evidenceRefs": [
            {
                "evidenceId": "ctor:com.acme.MyException:11111111",
                "kind": "constructor",
                "name": "constructor",
                "params": [{"type": "java.lang.String"}],
            },
            {
                "evidenceId": "sym:com.acme.MyException#getMessage:22222222",
                "kind": "method",
                "name": "getMessage",
                "returnType": "java.lang.String",
                "params": [],
            },
        ],
    }]
    patch = _patch("com.acme.MyException")
    patch["methods"][0]["body"] = (
        "// given\nString message = \"missing\";\n"
        "// when\nMyException exception = new MyException(message);\n"
        "// then\norg.junit.jupiter.api.Assertions.assertEquals(message, exception.getMessage());"
    )
    patch["methods"][0]["evidenceIds"] = [
        "ctor:com.acme.MyException:11111111",
        "sym:com.acme.MyException#getMessage:22222222",
    ]
    resp = {
        "schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
        "batchId": "batch-001", "role": "generation",
        "items": [{"targetId": "com.acme.MyException#<init>", "status": "generated",
                   "patchDescriptor": patch}],
    }
    try:
        bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("sut body call allowed with evidence ref", True)
    except bp.BatchResponseError as exc:
        _assert("sut body call allowed with evidence ref", False, str(exc))


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
                  "canonicalTestClass": "com.acme.C0Test",
                  "allowedImports": ["org.junit.jupiter.api.Test"],
                  "allowedEvidenceIds": ["sym:com.acme.C0#m:12345678"]}]
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
