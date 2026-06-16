"""test_batch_flow_hardening.py — the batch flow hardening milestone.

Locks the new safety nets added to the handoff-batch flow:

  * pre-flight evidence gate: a target with no evidence (or a required-but-absent
    target method) is SKIPPED before any LLM call, with the audit reason
  * a target WITH full evidence is NOT skipped
  * the request is batch-only: contextPolicy {scope: batch_only,
    allowRepositoryRead: false, onMissingContext: NEED_MORE_CONTEXT} +
    structuredContext + missingContextPolicy, in both generation and repair
  * NEED_MORE_CONTEXT is a VALID response item (does not break validation) and is
    mapped to SKIPPED(MISSING_CONTEXT) by the runner
  * the repair loop refuses to re-send when there are no actionable logs, when a
    patcher rejection carries no diagnostics, when the same failure signature
    recurs, and when a round makes no progress — abandoning with explicit reasons
  * the repair request carries a structured repairCause (never a bare patcher rc=3)
  * RunPaths is the single source of truth: every request/response/validation path
    belongs to exactly the same runId/batchId, and no mirror folder with a stray
    suffix (run-XXXX vs run-XXXXS) is ever produced

Legacy-suite convention: expose ``main() -> int`` (0 = ok). Auto-discovered by
test_aa_suite_runner.py. Run standalone:
    python tools/python/tests/test_batch_flow_hardening.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))   # repo root → orchestrator.*
sys.path.insert(0, str(HERE.parent))        # tools/python → budget_enforcer

from orchestrator import batch_protocol as bp  # noqa: E402
from orchestrator import batch_runner as br    # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(label + (f" — {detail}" if detail else ""))


# ── pre-flight evidence gate (pure) ───────────────────────────────────────────

def case_preflight_skips_without_evidence() -> None:
    target = {"targetId": "com.acme.C0#m", "sut": "com.acme.C0",
              "allowedEvidenceIds": [], "evidenceRefs": []}
    reason = bp.preflight_evidence_gate(target)
    _assert("preflight skips no-evidence target", reason == bp.PREFLIGHT_SKIP_REASON, str(reason))


def case_preflight_skips_required_target_without_ids() -> None:
    target = {"targetId": "com.acme.C0#m", "sut": "com.acme.C0",
              "allowedEvidenceIds": ["ctor:com.acme.C0:1"],
              "evidenceRefs": [{"evidenceId": "ctor:com.acme.C0:1", "kind": "constructor"}],
              "targetEvidenceRequired": True, "targetEvidenceIds": []}
    reason = bp.preflight_evidence_gate(target)
    _assert("preflight skips required-but-unevidenced method",
            reason == bp.PREFLIGHT_SKIP_REASON, str(reason))


def case_preflight_passes_full_context() -> None:
    target = {"targetId": "com.acme.C0#m", "sut": "com.acme.C0",
              "allowedEvidenceIds": ["sym:com.acme.C0#m:1"],
              "evidenceRefs": [{"evidenceId": "sym:com.acme.C0#m:1", "kind": "method", "name": "m"}],
              "targetEvidenceRequired": True, "targetEvidenceIds": ["sym:com.acme.C0#m:1"]}
    _assert("preflight passes full-evidence target",
            bp.preflight_evidence_gate(target) is None)


# ── contextPolicy batch-only (pure) ───────────────────────────────────────────

def _target(sut: str = "com.acme.C0") -> dict:
    return {"targetId": f"{sut}#m", "sut": sut, "method": "m",
            "allowedImports": ["org.junit.jupiter.api.Test"],
            "allowedEvidenceIds": [f"sym:{sut}#m:1"],
            "evidenceRefs": [{"evidenceId": f"sym:{sut}#m:1", "kind": "method", "name": "m"}],
            "targetEvidenceRequired": True, "targetEvidenceIds": [f"sym:{sut}#m:1"]}


def case_generation_request_is_batch_only() -> None:
    req = bp.build_generation_request("run-1", "batch-001", [_target()], batch_size=10)
    cp = req.get("contextPolicy", {})
    _assert("gen contextPolicy scope", cp.get("scope") == "batch_only", str(cp))
    _assert("gen contextPolicy no repo read", cp.get("allowRepositoryRead") is False, str(cp))
    _assert("gen contextPolicy no prod read", cp.get("allowProductionCodeRead") is False, str(cp))
    _assert("gen contextPolicy onMissing", cp.get("onMissingContext") == "NEED_MORE_CONTEXT", str(cp))
    _assert("gen missingContextPolicy", req["missingContextPolicy"]["allowedStatus"] == "NEED_MORE_CONTEXT")
    sc = req["targets"][0]["structuredContext"]
    _assert("gen structuredContext targetSource", sc["targetSource"]["sut"] == "com.acme.C0", str(sc))
    _assert("gen structuredContext allowedApi", isinstance(sc["allowedApi"], list), str(sc))
    _assert("gen structuredContext missingPolicy",
            sc["missingContextPolicy"]["allowedStatus"] == "NEED_MORE_CONTEXT", str(sc))


def case_repair_request_is_batch_only() -> None:
    failed = [{"targetId": "com.acme.C0#m", "failureKind": "TEST_FAILURE",
               "sut": "com.acme.C0", "canonicalTestClass": "com.acme.C0Test"}]
    req = bp.build_repair_request("run-1", "batch-001", 1, failed)
    cp = req.get("contextPolicy", {})
    _assert("repair contextPolicy scope", cp.get("scope") == "batch_only", str(cp))
    _assert("repair contextPolicy no repo read", cp.get("allowRepositoryRead") is False, str(cp))
    _assert("repair missingContextPolicy",
            req["missingContextPolicy"]["allowedStatus"] == "NEED_MORE_CONTEXT")


# ── NEED_MORE_CONTEXT response (pure) ─────────────────────────────────────────

def case_need_more_context_valid_in_generation() -> None:
    targets = [_target()]
    resp = {"schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "run-1",
            "batchId": "batch-001", "role": "generation",
            "items": [{"targetId": "com.acme.C0#m", "status": "NEED_MORE_CONTEXT",
                       "missingSymbols": ["com.acme.Dep#build"], "reason": "no Dep ctor"}]}
    try:
        items = bp.validate_generation_response(resp, targets, batch_id="batch-001")
        _assert("NEED_MORE_CONTEXT accepted in generation", len(items) == 1)
    except bp.BatchResponseError as exc:
        _assert("NEED_MORE_CONTEXT accepted in generation", False, str(exc))


def case_need_more_context_valid_in_repair() -> None:
    resp = {"schemaVersion": bp.SCHEMA_REPAIR_RESPONSE, "runId": "run-1",
            "batchId": "batch-001", "role": "repair", "repairRound": 1,
            "items": [{"targetId": "com.acme.C0#m", "status": "need_more_context",
                       "missingSymbols": [], "reason": "x"}]}
    try:
        bp.validate_repair_response(resp, {"com.acme.C0#m"}, batch_id="batch-001",
                                    repair_round=1, requested_items=[{"targetId": "com.acme.C0#m"}])
        _assert("NEED_MORE_CONTEXT accepted in repair", True)
    except bp.BatchResponseError as exc:
        _assert("NEED_MORE_CONTEXT accepted in repair", False, str(exc))


# ── repair admission gate (pure) ──────────────────────────────────────────────

def case_repair_admission_no_actionable_logs() -> None:
    fi = {"targetId": "t", "failureKind": "COMPILATION_ERROR", "errorSummary": "COMPILATION_ERROR",
          "compilerErrorDetails": "", "patcherErrorDetails": "", "buildOutput": ""}
    ok, reason = bp.repair_admission(fi)
    _assert("no-logs item not admitted", ok is False)
    _assert("no-logs reason", reason == bp.ABANDON_NO_ACTIONABLE_LOGS, str(reason))


def case_repair_admission_patcher_no_diagnostics() -> None:
    fi = {"targetId": "t", "failureKind": "PATCH_REJECTED", "errorSummary": "patcher rc=3",
          "compilerErrorDetails": "", "patcherErrorDetails": "", "buildOutput": ""}
    ok, reason = bp.repair_admission(fi)
    _assert("bare patcher rc=3 not admitted", ok is False)
    _assert("patcher-no-diag reason", reason == bp.ABANDON_PATCHER_NO_DIAGNOSTICS, str(reason))


def case_repair_admission_repeated_signature() -> None:
    fi = {"targetId": "t", "failureKind": "COMPILATION_ERROR", "errorSummary": "[E_X] boom",
          "compilerErrorDetails": "[E_X] F.java:1: boom", "patcherErrorDetails": "", "buildOutput": ""}
    sig = bp.failure_signature(fi)
    ok, reason = bp.repair_admission(fi, previous_signature=sig)
    _assert("repeated signature not admitted", ok is False)
    _assert("repeated signature reason", reason == bp.ABANDON_REPEATED_SIGNATURE, str(reason))


def case_repair_admission_actionable_admitted() -> None:
    fi = {"targetId": "t", "failureKind": "COMPILATION_ERROR", "errorSummary": "[E_X] boom",
          "compilerErrorDetails": "[E_X] F.java:1: boom", "patcherErrorDetails": "", "buildOutput": ""}
    ok, reason = bp.repair_admission(fi, previous_signature="different")
    _assert("actionable compiler error admitted", ok is True and reason is None, str((ok, reason)))


def case_test_failure_is_actionable_and_weak() -> None:
    fi = {"targetId": "t", "failureKind": "TEST_FAILURE", "errorSummary": "TEST_FAILURE",
          "compilerErrorDetails": "", "patcherErrorDetails": "", "buildOutput": ""}
    ok, _ = bp.repair_admission(fi)
    _assert("test failure admitted (gets one round)", ok is True)
    _assert("test failure has weak diagnostics", bp.weak_diagnostics(fi) is True)


# ── structured repairCause (pure) ─────────────────────────────────────────────

def case_repair_cause_structured() -> None:
    fi = {"targetId": "t", "failureKind": "COMPILATION_ERROR",
          "errorSummary": "[E_CONSTRUCTOR_UNRESOLVED] required: String",
          "compilerErrorDetails": "[E_CONSTRUCTOR_UNRESOLVED] C0Test.java:7: required: String",
          "patcherErrorDetails": "[BLOCKED] G2_SYMBOL_WITHOUT_EVIDENCE", "buildOutput": "mvn output…",
          "testFile": "src/test/java/com/acme/C0Test.java", "rejectedTestClass": "com.acme.C0CtorTest"}
    cause = bp.build_repair_cause(fi, previous_signature="abc123")
    _assert("repairCause kind compiler", cause["kind"] == "COMPILER_ERROR", str(cause))
    _assert("repairCause has stdout", cause["stdout"] == "mvn output…", str(cause))
    _assert("repairCause has patcherDiagnostics",
            cause["patcherDiagnostics"] == ["[BLOCKED] G2_SYMBOL_WITHOUT_EVIDENCE"], str(cause))
    _assert("repairCause failedRules", "E_CONSTRUCTOR_UNRESOLVED" in cause["failedRules"], str(cause))
    _assert("repairCause rejectedFiles", cause["rejectedFiles"] == ["src/test/java/com/acme/C0Test.java"], str(cause))
    _assert("repairCause rejectedMethods", cause["rejectedMethods"] == ["com.acme.C0CtorTest"], str(cause))
    _assert("repairCause prevSignature", cause["previousFailureSignature"] == "abc123", str(cause))


def case_failure_signature_sensitive() -> None:
    a = {"failureKind": "COMPILATION_ERROR", "errorSummary": "x", "compilerErrorDetails": "[E1] a"}
    b = {"failureKind": "COMPILATION_ERROR", "errorSummary": "x", "compilerErrorDetails": "[E2] b"}
    _assert("signature stable", bp.failure_signature(a) == bp.failure_signature(dict(a)))
    _assert("signature changes with cause", bp.failure_signature(a) != bp.failure_signature(b))


# ── RunPaths single source of truth ───────────────────────────────────────────

def case_run_paths_consistent() -> None:
    paths = br.RunPaths(Path("/tmp/state"), "run-20260616-000000")
    bid = "batch-007"
    # assert_consistent raises on any drift; reaching the asserts means it passed.
    paths.assert_consistent(bid, repair_round=2)
    for p in (paths.manifest(), paths.request_generation(bid), paths.response_generation(bid),
              paths.validation_result(bid), paths.request_repair(bid, 2),
              paths.response_repair(bid, 2), paths.validation_result_repair(bid, 2)):
        _assert(f"path carries runId: {p.name}", "run-20260616-000000" in p.parts, str(p))
    for p in (paths.request_generation(bid), paths.validation_result_repair(bid, 2)):
        _assert(f"path carries batchId: {p.name}", bid in p.parts, str(p))


def case_run_paths_no_mirror_suffix() -> None:
    good = br.RunPaths(Path("/tmp/state"), "run-1234")
    mirror = br.RunPaths(Path("/tmp/state"), "run-1234S")
    _assert("distinct run_dir for distinct runId", good.run_dir != mirror.run_dir)
    # No path of the good run ever contains the mirror's suffixed folder name.
    for p in (good.manifest(), good.request_generation("batch-001"),
              good.validation_result_repair("batch-001", 1)):
        _assert("good run never references mirror folder", "run-1234S" not in p.parts, str(p))
    _assert("run_dir basename is exactly run_id", good.run_dir.name == "run-1234", str(good.run_dir))


# ── runner-level integration (stubbed edges) ──────────────────────────────────

def _setup(td: Path, packs: dict[str, dict]) -> Path:
    """packs maps sut → context-pack dict; items are derived from it."""
    state = td / "state"
    (state / "context-packs").mkdir(parents=True, exist_ok=True)
    items = []
    for sut, pack in packs.items():
        (state / "context-packs" / f"{sut}.json").write_text(json.dumps(pack), encoding="utf-8")
        items.append({"targetId": f"{sut}#m", "sut": sut, "method": "m", "score": 100})
    (state / "batch-plan.json").write_text(
        json.dumps({"schemaVersion": 1, "cycle": 0, "mode": "coverage", "items": items}),
        encoding="utf-8")
    (state / "execution-state.json").write_text(
        json.dumps({"schemaVersion": 1, "mode": "coverage", "cycle": 0, "phase": "generation",
                    "budget": {"maxMinutesPerCycle": 999}, "checkpoints": []}), encoding="utf-8")
    return state


def _pack_with_evidence(sut: str) -> dict:
    return {"schemaVersion": 1, "sut": sut, "allowedImports": ["org.junit.jupiter.api.Test"],
            "constructors": [],
            "methods": [{"evidenceId": f"sym:{sut}#m:1", "name": "m", "returnType": "void",
                         "params": [], "usable": True}]}


def _pack_no_evidence(sut: str) -> dict:
    return {"schemaVersion": 1, "sut": sut, "allowedImports": ["org.junit.jupiter.api.Test"],
            "constructors": [], "methods": []}


def _patch(sut: str, *, prefix: str = "patch") -> dict:
    return {"schemaVersion": 1, "patchId": f"{prefix}:abcdef", "cycle": 1, "sut": sut,
            "testClass": sut + "Test", "testPackage": sut.rsplit(".", 1)[0],
            "allowedImports": ["org.junit.jupiter.api.Test"],
            "methods": [{"name": "m_whenCondition_returnsExpected", "annotations": ["@Test"],
                         "body": "// given\nObject v = new Object();\n// when\nObject a = v;\n// then\n"
                                 "org.junit.jupiter.api.Assertions.assertSame(v, a);",
                         "evidenceIds": [f"sym:{sut}#m:1"]}]}


def _manifest(state: Path) -> dict:
    runs = sorted((state / "_llm" / "runs").glob("run-*"))
    return json.loads((runs[-1] / "manifest.json").read_text(encoding="utf-8"))


def _run_dir(state: Path) -> Path:
    return sorted((state / "_llm" / "runs").glob("run-*"))[-1]


def _with_stubs(fn):
    """Run fn with the side-effecting runner edges stubbed; always restore."""
    orig = {n: getattr(br, n) for n in ("_apply_patch", "_run_tests", "_surefire_status",
                                        "_wait_for_response")}
    orig_tool = br.one_cycle._run_tool
    os.environ["COVAGENT_IDE_INTERACTIVE"] = "0"
    try:
        fn()
    finally:
        for n, f in orig.items():
            setattr(br, n, f)
        br.one_cycle._run_tool = orig_tool
        os.environ.pop("COVAGENT_IDE_INTERACTIVE", None)


def case_preflight_skip_persisted() -> None:
    def body() -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = _setup(root, {"com.acme.C0": _pack_with_evidence("com.acme.C0"),
                                  "com.acme.C1": _pack_no_evidence("com.acme.C1")})
            gen = {"schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "r", "batchId": "batch-001",
                   "role": "generation", "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                                                     "patchDescriptor": _patch("com.acme.C0")}]}
            br._apply_patch = lambda patch, *, state_dir, repo, repair_attempts=None: 0  # type: ignore
            br._run_tests = lambda repo, state_dir, tcs: 0  # type: ignore
            br.one_cycle._run_tool = lambda script, args: 0  # type: ignore
            br._wait_for_response = lambda *a, **k: ("ok", gen)  # type: ignore
            br.run_batches(state, root, batch_size=10, max_repair_rounds=0, max_batches=None)
            m = _manifest(state)
            _assert("preflight: C1 SKIPPED",
                    m["targets"]["com.acme.C1#m"]["status"] == bp.SKIPPED,
                    str(m["targets"]["com.acme.C1#m"]))
            _assert("preflight: skip reason persisted",
                    m["targets"]["com.acme.C1#m"].get("reason") == bp.PREFLIGHT_SKIP_REASON,
                    str(m["targets"]["com.acme.C1#m"]))
            _assert("preflight: C0 PASSED", m["targets"]["com.acme.C0#m"]["status"] == bp.PASSED)
            pf = _run_dir(state) / "batches" / "batch-001" / "preflight-result.json"
            _assert("preflight-result.json written", pf.exists())
            _assert("preflight-result lists C1",
                    pf.exists() and "com.acme.C1#m" in pf.read_text(encoding="utf-8"))
    _with_stubs(body)


def case_need_more_context_skips_target() -> None:
    def body() -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = _setup(root, {"com.acme.C0": _pack_with_evidence("com.acme.C0")})
            gen = {"schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "r", "batchId": "batch-001",
                   "role": "generation", "items": [{"targetId": "com.acme.C0#m",
                                                     "status": "NEED_MORE_CONTEXT",
                                                     "missingSymbols": ["com.acme.Dep"], "reason": "no Dep"}]}
            br._apply_patch = lambda *a, **k: 0  # type: ignore
            br._run_tests = lambda *a, **k: 0  # type: ignore
            br.one_cycle._run_tool = lambda script, args: 0  # type: ignore
            br._wait_for_response = lambda *a, **k: ("ok", gen)  # type: ignore
            br.run_batches(state, root, batch_size=10, max_repair_rounds=0, max_batches=None)
            rec = _manifest(state)["targets"]["com.acme.C0#m"]
            _assert("NEED_MORE_CONTEXT → SKIPPED", rec["status"] == bp.SKIPPED, str(rec))
            _assert("NEED_MORE_CONTEXT reason tagged MISSING_CONTEXT",
                    str(rec.get("reason", "")).startswith(bp.ABANDON_MISSING_CONTEXT), str(rec))
            _assert("NEED_MORE_CONTEXT missingSymbols persisted",
                    rec.get("missingSymbols") == ["com.acme.Dep"], str(rec))
    _with_stubs(body)


def case_repair_loop_stops_without_diagnostics() -> None:
    def body() -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = _setup(root, {"com.acme.C0": _pack_with_evidence("com.acme.C0")})
            gen = {"schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "r", "batchId": "batch-001",
                   "role": "generation", "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                                                     "patchDescriptor": _patch("com.acme.C0")}]}
            calls = {"repair_waits": 0}

            def fake_wait(request, response, *, state_path, manifest, kind, batch_id, repair_round=None):
                if kind == "repair":
                    calls["repair_waits"] += 1
                return "ok", gen

            # Patch fails to apply (rc=3) with no patcher-decisions file → PATCH_REJECTED
            # with no diagnostics → repair loop must NOT request a repair handoff.
            br._apply_patch = lambda patch, *, state_dir, repo, repair_attempts=None: 3  # type: ignore
            br._run_tests = lambda repo, state_dir, tcs: 0  # type: ignore
            br.one_cycle._run_tool = lambda script, args: 0  # type: ignore
            br._wait_for_response = fake_wait  # type: ignore
            br.run_batches(state, root, batch_size=10, max_repair_rounds=2, max_batches=None)
            rec = _manifest(state)["targets"]["com.acme.C0#m"]
            _assert("no-diag patch failure → ABANDONED", rec["status"] == bp.ABANDONED, str(rec))
            _assert("no-diag reason PATCHER_REJECTED_WITHOUT_DIAGNOSTICS",
                    rec.get("reason") == bp.ABANDON_PATCHER_NO_DIAGNOSTICS, str(rec))
            _assert("no repair handoff was requested", calls["repair_waits"] == 0,
                    f"repair_waits={calls['repair_waits']}")
            rreq = _run_dir(state) / "batches" / "batch-001" / "request-repair-r1.json"
            _assert("request-repair-r1.json NOT written", not rreq.exists())
    _with_stubs(body)


def case_repair_loop_stops_on_no_progress() -> None:
    def body() -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = _setup(root, {"com.acme.C0": _pack_with_evidence("com.acme.C0")})
            gen = {"schemaVersion": bp.SCHEMA_GENERATION_RESPONSE, "runId": "r", "batchId": "batch-001",
                   "role": "generation", "items": [{"targetId": "com.acme.C0#m", "status": "generated",
                                                     "patchDescriptor": _patch("com.acme.C0")}]}
            # Model skips the repair (re-applies nothing) → NO_PROGRESS after the round.
            rep = {"schemaVersion": bp.SCHEMA_REPAIR_RESPONSE, "runId": "r", "batchId": "batch-001",
                   "role": "repair", "repairRound": 1,
                   "items": [{"targetId": "com.acme.C0#m", "status": "skipped", "reason": "stub"}]}
            waits = {"repair": 0}

            def fake_wait(request, response, *, state_path, manifest, kind, batch_id, repair_round=None):
                if kind == "repair":
                    waits["repair"] += 1
                    return "ok", rep
                return "ok", gen

            br._apply_patch = lambda patch, *, state_dir, repo, repair_attempts=None: 0  # type: ignore
            br._run_tests = lambda repo, state_dir, tcs: 1  # type: ignore  (tests fail)
            br._surefire_status = lambda repo, test_class: "failed"  # type: ignore
            br.one_cycle._run_tool = lambda script, args: 0  # type: ignore
            br._wait_for_response = fake_wait  # type: ignore
            br.run_batches(state, root, batch_size=10, max_repair_rounds=2, max_batches=None)
            rec = _manifest(state)["targets"]["com.acme.C0#m"]
            _assert("no-progress → ABANDONED", rec["status"] == bp.ABANDONED, str(rec))
            _assert("no-progress reason NO_PROGRESS_AFTER_REPAIR",
                    rec.get("reason") == bp.ABANDON_NO_PROGRESS, str(rec))
            _assert("no-progress: stopped after one repair round (no second handoff)",
                    waits["repair"] == 1, f"repair waits={waits['repair']}")
    _with_stubs(body)


# ── existingRelatedTests + expectedBehavior enrichment ───────────────────────

def case_existing_test_methods_extracted() -> None:
    """_existing_test_methods reads @Test method names from a pre-existing test file."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sut = "com.acme.MyService"
        test_path = root / "src" / "test" / "java" / "com" / "acme" / "MyServiceTest.java"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(
            "class MyServiceTest {\n"
            "    @Test\n"
            "    void process_whenValid_returnsResult() {}\n"
            "    @Test\n"
            "    void process_whenNull_throwsException() {}\n"
            "}\n",
            encoding="utf-8",
        )
        names = br._existing_test_methods(root, sut)
        _assert("found both existing test methods", len(names) == 2, str(names))
        _assert("first method name correct",
                "process_whenValid_returnsResult" in names, str(names))
        _assert("second method name correct",
                "process_whenNull_throwsException" in names, str(names))

    # No repo / no test file → empty list, no error.
    _assert("no repo returns []", br._existing_test_methods(None, "com.acme.X") == [])
    _assert("no test file returns []",
            br._existing_test_methods(Path(td), "com.acme.NonExistent") == [])


def case_expected_behavior_hints_extracted() -> None:
    """_expected_behavior_hints reads from plan item context: generationHint and syntheticCoverageTargets."""
    item_with_hint = {"context": {"generationHint": "cover the null path"}}
    hints = br._expected_behavior_hints(item_with_hint)
    _assert("generationHint extracted", hints == ["cover the null path"], str(hints))

    item_with_synthetic = {"context": {
        "syntheticCoverageTargets": [
            {"id": "lambda$process$0", "description": "empty Optional path"},
            {"label": "fallback branch"},
            "bare string hint",
        ]
    }}
    hints2 = br._expected_behavior_hints(item_with_synthetic)
    _assert("synthetic descriptions extracted", len(hints2) == 3, str(hints2))
    _assert("first synthetic hint", hints2[0] == "empty Optional path", str(hints2))
    _assert("fallback label used", hints2[1] == "fallback branch", str(hints2))
    _assert("bare string hint", hints2[2] == "bare string hint", str(hints2))

    _assert("empty context returns []", br._expected_behavior_hints({}) == [])


def case_enrichment_injects_existing_and_behavior() -> None:
    """_enrich_targets_with_imports populates existingRelatedTests + expectedBehavior."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sut = "com.acme.Svc"
        state = root / "state"
        pack_dir = state / "context-packs"
        pack_dir.mkdir(parents=True, exist_ok=True)
        pack = {"schemaVersion": 1, "sut": sut, "allowedImports": [],
                "constructors": [], "methods": []}
        (pack_dir / f"{sut}.json").write_text(json.dumps(pack), encoding="utf-8")

        # Pre-existing test file.
        tf = root / "src" / "test" / "java" / "com" / "acme" / "SvcTest.java"
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text("class SvcTest { @Test void existing_test() {} }\n", encoding="utf-8")

        targets = [{"targetId": f"{sut}#m", "sut": sut, "method": "m", "score": 1,
                    "context": {"generationHint": "cover empty case"}}]
        enriched = br._enrich_targets_with_imports(targets, state_dir=state, repo=root)
        row = enriched[0]
        _assert("existingRelatedTests populated",
                row.get("existingRelatedTests") == ["existing_test"], str(row.get("existingRelatedTests")))
        _assert("expectedBehavior populated",
                row.get("expectedBehavior") == ["cover empty case"], str(row.get("expectedBehavior")))


# ── batch_final_report --run-id consistency ───────────────────────────────────

def case_batch_final_report_run_id_canonical() -> None:
    """batch_final_report._canonical_run_dir computes the same path as RunPaths."""
    import importlib  # noqa: PLC0415
    bfr = importlib.import_module("batch_final_report")  # on sys.path via HERE.parent insert
    state = Path("/tmp/state")
    run_id = "run-20260616-120000"
    canonical = bfr._canonical_run_dir(state, run_id)
    expected = (state / "_llm" / "runs" / run_id).resolve()
    _assert("canonical_run_dir matches RunPaths formula", canonical == expected, str(canonical))
    _assert("canonical_run_dir contains run_id", run_id in canonical.parts, str(canonical))


def main() -> int:
    cases = [
        case_preflight_skips_without_evidence,
        case_preflight_skips_required_target_without_ids,
        case_preflight_passes_full_context,
        case_generation_request_is_batch_only,
        case_repair_request_is_batch_only,
        case_need_more_context_valid_in_generation,
        case_need_more_context_valid_in_repair,
        case_repair_admission_no_actionable_logs,
        case_repair_admission_patcher_no_diagnostics,
        case_repair_admission_repeated_signature,
        case_repair_admission_actionable_admitted,
        case_test_failure_is_actionable_and_weak,
        case_repair_cause_structured,
        case_failure_signature_sensitive,
        case_run_paths_consistent,
        case_run_paths_no_mirror_suffix,
        case_preflight_skip_persisted,
        case_need_more_context_skips_target,
        case_repair_loop_stops_without_diagnostics,
        case_repair_loop_stops_on_no_progress,
        case_existing_test_methods_extracted,
        case_expected_behavior_hints_extracted,
        case_enrichment_injects_existing_and_behavior,
        case_batch_final_report_run_id_canonical,
    ]
    for c in cases:
        try:
            c()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{c.__name__} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print("FAIL test_batch_flow_hardening:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK   test_batch_flow_hardening: {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
