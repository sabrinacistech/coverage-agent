"""test_response_hydrator.py — Python builds the canonical patchDescriptor.

Locks the new generation contract (orchestrator.batch_protocol.hydrate_generation_response):
  * the LLM returns a MINIMAL completion (status + methods); Python hydrates the
    canonical patchDescriptor from the TARGET (schemaVersion/patchId/sut/testClass/
    template/allowedImports never come from the model);
  * the legacy shape (item.patchDescriptor.methods) is accepted but its metadata is
    ignored — Python still rebuilds everything from the target;
  * a single bad ITEM never aborts the batch: it becomes failed with a classified
    reason while valid / NEED_MORE_CONTEXT siblings are untouched;
  * omitted / unknown / duplicated targets are diagnosed, not fatal;
  * a structurally broken response raises BatchResponseError (the batch-level abort).

Legacy-suite convention: expose ``main() -> int`` (0 = ok). Auto-discovered by
test_aa_suite_runner.py. Run standalone:
    python tools/python/tests/test_response_hydrator.py
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


def _target(tid: str = "tgt:0001", sut: str = "com.acme.Foo",
            evid: str = "sym:com.acme.Foo#bar:abcd1234") -> dict:
    """A request target shaped like build_generation_request emits."""
    return {
        "targetId": tid,
        "sut": sut,
        "canonicalTestClass": f"{sut}Test",
        "template": "unit",
        "allowedImports": ["org.junit.jupiter.api.Test"],
        "allowedEvidenceIds": [evid],
        "evidenceRefs": [{"kind": "method", "name": "bar", "id": evid}],
        "targetEvidenceRequired": False,
        "targetEvidenceIds": [],
    }


def _method(evid: str = "sym:com.acme.Foo#bar:abcd1234") -> dict:
    """A minimal LLM completion method whose body declares no SUT variable (so the
    unevidenced-SUT-call check stays out of the way) and cites valid evidence."""
    return {
        "name": "shouldReturnOne_whenInputIsOne",
        "annotations": ["@Test"],
        "body": ("// given\nint x = 1;\n// when\nint y = x;\n// then\n"
                 "org.junit.jupiter.api.Assertions.assertEquals(1, y);"),
        "evidenceIds": [evid],
    }


def _request(*targets: dict) -> dict:
    return {"targets": list(targets)}


def _response(*items: dict) -> dict:
    return {"targets": list(items)}


# ── A. new format: minimal completion → canonical patchDescriptor ────────────────

def case_a_new_format_hydrates_canonical() -> None:
    t = _target()
    out = bp.hydrate_generation_response(
        _request(t),
        _response({"targetId": "tgt:0001", "status": "generated",
                   "methods": [_method()]}),
    )
    item = out["targets"][0]
    _assert("A status generated", item["status"] == "generated", item["status"])
    pd = item["patchDescriptor"]
    _assert("A schemaVersion==1", pd["schemaVersion"] == 1, repr(pd.get("schemaVersion")))
    _assert("A patchId prefix", pd["patchId"].startswith("patch:"), pd["patchId"])
    _assert("A sut from target", pd["sut"] == "com.acme.Foo", pd["sut"])
    _assert("A testClass canonical", pd["testClass"] == "com.acme.FooTest", pd["testClass"])
    _assert("A testPackage derived", pd["testPackage"] == "com.acme", pd["testPackage"])
    _assert("A template from target", pd["template"] == "unit", repr(pd.get("template")))
    _assert("A allowedImports = whitelist",
            pd["allowedImports"] == ["org.junit.jupiter.api.Test"], repr(pd["allowedImports"]))
    _assert("A methods present", len(pd["methods"]) == 1, repr(pd["methods"]))
    _assert("A counts generatedValid", out["counts"]["generatedValid"] == 1, repr(out["counts"]))


def case_f_no_patch_descriptor_required() -> None:
    """A generated item with ONLY methods (no patchDescriptor key) is valid."""
    t = _target()
    out = bp.hydrate_generation_response(
        _request(t),
        _response({"targetId": "tgt:0001", "status": "generated", "methods": [_method()]}),
    )
    _assert("F no descriptor needed", out["targets"][0]["status"] == "generated")
    _assert("F has hydrated descriptor", "patchDescriptor" in out["targets"][0])


def case_a_default_test_annotation() -> None:
    """A method that omits annotations defaults to ['@Test'] in the hydrated descriptor."""
    t = _target()
    m = _method()
    m.pop("annotations")
    out = bp.hydrate_generation_response(
        _request(t), _response({"targetId": "tgt:0001", "status": "generated", "methods": [m]}))
    pd = out["targets"][0]["patchDescriptor"]
    _assert("A default annotation", pd["methods"][0]["annotations"] == ["@Test"],
            repr(pd["methods"][0]["annotations"]))


# ── B. legacy format: patchDescriptor.methods used, metadata ignored ─────────────

def case_b_legacy_format_ignores_llm_metadata() -> None:
    t = _target()
    # The model sends a full (and WRONG) patchDescriptor; only its methods survive.
    legacy_item = {
        "targetId": "tgt:0001",
        "status": "generated",
        "patchDescriptor": {
            "schemaVersion": 99,
            "patchId": "WRONG",
            "sut": "com.evil.Hacked",
            "testClass": "com.evil.HackedCtorTest",
            "allowedImports": ["org.evil.Backdoor"],
            "methods": [_method()],
        },
    }
    out = bp.hydrate_generation_response(_request(t), _response(legacy_item))
    pd = out["targets"][0]["patchDescriptor"]
    _assert("B status generated", out["targets"][0]["status"] == "generated")
    _assert("B schemaVersion forced 1", pd["schemaVersion"] == 1, repr(pd["schemaVersion"]))
    _assert("B sut from target not LLM", pd["sut"] == "com.acme.Foo", pd["sut"])
    _assert("B testClass canonical not LLM", pd["testClass"] == "com.acme.FooTest", pd["testClass"])
    _assert("B allowedImports from target not LLM",
            pd["allowedImports"] == ["org.junit.jupiter.api.Test"], repr(pd["allowedImports"]))
    _assert("B patchId from Python", pd["patchId"].startswith("patch:"), pd["patchId"])


# ── C. one bad item does NOT break the batch ─────────────────────────────────────

def case_c_invalid_item_isolated() -> None:
    t1 = _target("tgt:0001", "com.acme.A", "sym:com.acme.A#m:11111111")
    t2 = _target("tgt:0002", "com.acme.B", "sym:com.acme.B#m:22222222")
    t3 = _target("tgt:0003", "com.acme.C", "sym:com.acme.C#m:33333333")
    out = bp.hydrate_generation_response(
        _request(t1, t2, t3),
        _response(
            {"targetId": "tgt:0001", "status": "generated",
             "methods": [_method("sym:com.acme.A#m:11111111")]},
            # invalid: cites an evidenceId that is not in the target's allowed set
            {"targetId": "tgt:0002", "status": "generated",
             "methods": [{"name": "shouldX_whenY", "annotations": ["@Test"],
                          "body": "// given\n// when\n// then\n",
                          "evidenceIds": ["sym:does-not-exist"]}]},
            {"targetId": "tgt:0003", "status": "NEED_MORE_CONTEXT",
             "missingSymbols": ["com.acme.C#ctor"], "reason": "no constructor evidence"},
        ),
    )
    by_id = {it["targetId"]: it for it in out["targets"]}
    _assert("C valid applied", by_id["tgt:0001"]["status"] == "generated",
            by_id["tgt:0001"]["status"])
    _assert("C invalid → failed", by_id["tgt:0002"]["status"] == "failed",
            by_id["tgt:0002"]["status"])
    _assert("C invalid reason = INVALID_EVIDENCE_ID",
            by_id["tgt:0002"]["reason"] == bp.HYDRATION_INVALID_EVIDENCE,
            by_id["tgt:0002"]["reason"])
    _assert("C need-context preserved", by_id["tgt:0003"]["status"] == "NEED_MORE_CONTEXT",
            by_id["tgt:0003"]["status"])
    c = out["counts"]
    _assert("C counts valid", c["generatedValid"] == 1, repr(c))
    _assert("C counts invalid", c["generatedInvalid"] == 1, repr(c))
    _assert("C counts needmore", c["needMoreContext"] == 1, repr(c))


def case_c_missing_methods_failed() -> None:
    t = _target()
    out = bp.hydrate_generation_response(
        _request(t), _response({"targetId": "tgt:0001", "status": "generated", "methods": []}))
    _assert("C empty methods → failed", out["targets"][0]["status"] == "failed")
    _assert("C empty methods reason", out["targets"][0]["reason"] == bp.HYDRATION_MISSING_METHODS,
            out["targets"][0]["reason"])


# ── omitted / unknown / duplicated targets are diagnosed, not fatal ──────────────

def case_omitted_target_failed() -> None:
    t1 = _target("tgt:0001", "com.acme.A", "sym:com.acme.A#m:11111111")
    t2 = _target("tgt:0002", "com.acme.B", "sym:com.acme.B#m:22222222")
    out = bp.hydrate_generation_response(
        _request(t1, t2),
        _response({"targetId": "tgt:0001", "status": "generated",
                   "methods": [_method("sym:com.acme.A#m:11111111")]}),
    )
    by_id = {it["targetId"]: it for it in out["targets"]}
    _assert("omitted present as failed", by_id["tgt:0002"]["status"] == "failed")
    _assert("omitted reason", by_id["tgt:0002"]["reason"] == bp.HYDRATION_OMITTED)
    _assert("omitted counted", out["counts"]["omitted"] == 1, repr(out["counts"]))


def case_unknown_and_duplicate_diagnosed() -> None:
    t = _target()
    out = bp.hydrate_generation_response(
        _request(t),
        _response(
            {"targetId": "tgt:0001", "status": "generated", "methods": [_method()]},
            {"targetId": "tgt:0001", "status": "generated", "methods": [_method()]},  # dup
            {"targetId": "tgt:9999", "status": "generated", "methods": [_method()]},  # unknown
        ),
    )
    reasons = {d["reason"] for d in out["diagnostics"]}
    _assert("dup diagnosed", bp.HYDRATION_DUPLICATED_TARGET in reasons, repr(reasons))
    _assert("unknown diagnosed", bp.HYDRATION_UNKNOWN_TARGET in reasons, repr(reasons))
    _assert("dup counted", out["counts"]["duplicated"] == 1, repr(out["counts"]))
    _assert("unknown counted", out["counts"]["unknown"] == 1, repr(out["counts"]))
    # the first (valid) tgt:0001 still hydrates fine
    _assert("first wins", out["counts"]["generatedValid"] == 1, repr(out["counts"]))


# ── D(partial)/structural: a broken response aborts the batch ────────────────────

def case_structural_errors_raise() -> None:
    t = _target()
    for label, bad in (("response not object", "nope"),
                       ("items not list", {"targets": "x"})):
        try:
            bp.hydrate_generation_response(_request(t), bad)  # type: ignore[arg-type]
            _assert(f"structural raises: {label}", False, "no exception")
        except bp.BatchResponseError:
            pass
        except Exception as exc:  # noqa: BLE001
            _assert(f"structural raises BatchResponseError: {label}", False,
                    f"{type(exc).__name__}")


def case_skipped_passthrough() -> None:
    t = _target()
    out = bp.hydrate_generation_response(
        _request(t),
        _response({"targetId": "tgt:0001", "status": "skipped", "reason": "trivial getter"}))
    _assert("skipped passthrough", out["targets"][0]["status"] == "skipped")
    _assert("skipped counted", out["counts"]["skipped"] == 1, repr(out["counts"]))


def main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("case_")]
    for c in cases:
        try:
            c()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{c.__name__} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print("FAIL test_response_hydrator:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK   test_response_hydrator: {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
