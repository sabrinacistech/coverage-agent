"""test_gate_g2.py — golden cases for the G2 symbol-evidence gate.

G2 blocks a patch whose test methods do not cite verifiable evidence:
  - PASS            : method cites an evidenceId present in a symbol-contract
  - FAIL (no ev)    : method has empty/absent evidenceIds → G2_SYMBOL_WITHOUT_EVIDENCE
  - FAIL (orphan)   : method cites a well-formed id absent from the contracts
  - SKIPPED         : patch is in the BLOCKED shape
  - PASS (no method): patch declares no test methods

Run: `python tools/python/tests/test_gate_g2.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from gate_runner import gate_g2, _collect_contract_evidence  # noqa: E402

FAILURES: list[str] = []

_GOOD_SYM = "sym:com.acme.FooService#calc(java.math.BigDecimal):e7a1b2c3"
_GOOD_CTOR = "ctor:com.acme.FooService:b3c1d2e0"
_ORPHAN = "sym:com.acme.FooService#ghost():deadbeef"


def _assert(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [ OK ] {label}")
    else:
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        FAILURES.append(label)


def _state_with_contract() -> Path:
    """Create a temp state dir with one symbol-contract carrying known evidence."""
    state = Path(tempfile.mkdtemp())
    contracts = state / "symbol-contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    contract = {
        "schemaVersion": 1,
        "fqcn": "com.acme.FooService",
        "kind": "class",
        "instantiation": {"allowed": True, "strategy": "constructor"},
        "constructors": [
            {"evidenceId": _GOOD_CTOR, "visibility": "public", "params": []}
        ],
        "methods": [
            {
                "evidenceId": _GOOD_SYM,
                "name": "calc",
                "returnType": "java.math.BigDecimal",
                "params": [],
                "usable": True,
            }
        ],
    }
    (contracts / "com.acme.FooService.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )
    return state


def case_pass_cited_evidence() -> None:
    print("== PASS: method cites known evidence ==")
    state = _state_with_contract()
    patch = {
        "methods": [
            {"name": "calc_ok", "body": "// ...", "evidenceIds": [_GOOD_SYM, _GOOD_CTOR]}
        ]
    }
    res = gate_g2(patch, state)
    _assert("status PASS", res.get("status") == "PASS", json.dumps(res))


def case_fail_no_evidence() -> None:
    print("== FAIL: method without evidenceIds ==")
    state = _state_with_contract()
    patch = {"methods": [{"name": "calc_bad", "body": "// ...", "evidenceIds": []}]}
    res = gate_g2(patch, state)
    _assert("status FAIL", res.get("status") == "FAIL", json.dumps(res))
    _assert(
        "blockedReason G2_SYMBOL_WITHOUT_EVIDENCE",
        res.get("blockedReason") == "G2_SYMBOL_WITHOUT_EVIDENCE",
        json.dumps(res),
    )
    _assert(
        "method flagged",
        "calc_bad" in (res.get("methodsWithoutEvidence") or []),
        json.dumps(res),
    )


def case_fail_orphan_evidence() -> None:
    print("== FAIL: method cites id absent from contracts ==")
    state = _state_with_contract()
    patch = {"methods": [{"name": "calc_ghost", "body": "// ...", "evidenceIds": [_ORPHAN]}]}
    res = gate_g2(patch, state)
    _assert("status FAIL", res.get("status") == "FAIL", json.dumps(res))
    orphans = [o.get("evidenceId") for o in (res.get("orphanEvidenceIds") or [])]
    _assert("orphan id reported", _ORPHAN in orphans, json.dumps(res))


def case_skipped_blocked() -> None:
    print("== SKIPPED: BLOCKED patch ==")
    state = _state_with_contract()
    patch = {"schemaVersion": 1, "status": "BLOCKED", "blockReason": "no constructor evidence"}
    res = gate_g2(patch, state)
    _assert("status SKIPPED", res.get("status") == "SKIPPED", json.dumps(res))


def case_pass_no_methods() -> None:
    print("== PASS: patch with no methods ==")
    state = _state_with_contract()
    patch = {"methods": []}
    res = gate_g2(patch, state)
    _assert("status PASS", res.get("status") == "PASS", json.dumps(res))


def case_pass_inherited_throwable_evidence() -> None:
    print("== PASS: exception SUT cites inherited Throwable evidence ==")
    import inherited_evidence  # noqa: E402 (same dir on sys.path)
    state = _state_with_contract()  # contract carries no getMessage method
    sut = "com.acme.FooException"
    getmsg_id = inherited_evidence.throwable_evidence_id(sut, "getMessage")
    patch = {
        "sut": sut,
        "methods": [
            {"name": "getMessage_whenConstructed_returnsMessage",
             "body": "// ...", "evidenceIds": [getmsg_id]}
        ],
    }
    res = gate_g2(patch, state)
    # The synthetic inherited-Throwable id is honored even though it is absent
    # from the symbol-contracts — same source of truth the request advertises.
    _assert("status PASS (inherited evidence honored)",
            res.get("status") == "PASS", json.dumps(res))


def case_fail_inherited_evidence_on_non_throwable() -> None:
    print("== FAIL: non-exception SUT may not use synthetic Throwable id ==")
    import inherited_evidence  # noqa: E402
    state = _state_with_contract()
    sut = "com.acme.FooService"  # not an exception → synthetic ids not honored
    getmsg_id = inherited_evidence.throwable_evidence_id(sut, "getMessage")
    patch = {"sut": sut,
             "methods": [{"name": "x_y_z", "body": "// ...", "evidenceIds": [getmsg_id]}]}
    res = gate_g2(patch, state)
    _assert("status FAIL (not throwable)", res.get("status") == "FAIL", json.dumps(res))


def case_collect_evidence() -> None:
    print("== helper: _collect_contract_evidence ==")
    state = _state_with_contract()
    known = _collect_contract_evidence(state)
    _assert("collects method evidence", _GOOD_SYM in known)
    _assert("collects ctor evidence", _GOOD_CTOR in known)


def main() -> int:
    case_pass_cited_evidence()
    case_fail_no_evidence()
    case_fail_orphan_evidence()
    case_skipped_blocked()
    case_pass_no_methods()
    case_pass_inherited_throwable_evidence()
    case_fail_inherited_evidence_on_non_throwable()
    case_collect_evidence()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} case(s): {FAILURES}")
        return 1
    print("All G2 golden cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
