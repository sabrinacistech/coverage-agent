"""test_handoff_and_budget.py — M3 protocol-gap fixes.

Covers:
  A1  handoff-summary schema accepts the three real shapes and rejects a
      READY summary missing its required facts.
  M1  budget boundary: with tick-before-check, exactly maxCycles cycles run
      and the (maxCycles+1)th is refused (no off-by-one extra cycle).

Run: `python tools/python/tests/test_handoff_and_budget.py`  (exits non-zero on failure)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from common import validate  # noqa: E402
import budget_enforcer  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ OK ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        FAILURES.append(label)


def _ready() -> dict:
    return {
        "schemaVersion": 1,
        "generatedAt": "2026-05-29T00:00:00Z",
        "phase": "PRE_GENERATION",
        "status": "READY",
        "buildTool": {"type": "maven", "groupId": "com.acme", "javaVersion": "21"},
        "archetype": {"parent": "bgba-parent-paas-java-21", "namespace": "jakarta"},
        "stack": {
            "testFramework": "junit5", "mockingLib": "mockito",
            "assertionLib": "assertj", "diFramework": "spring",
            "springBoot": "3.2.0", "blocked": False,
        },
        "counts": {
            "symbolContracts": 3, "contextPacks": 3, "fixtures": 2,
            "dependencyGraphs": 1, "classes": 10,
        },
        "classification": {"service": 5, "controller": 3},
        "batchPlan": {
            "cycle": 0, "mode": "coverage", "size": 3,
            "topSuts": [{"fqcn": "com.acme.FooService"}],
        },
        "llmInstructions": ["Proceed to Phase 8."],
    }


def case_schema_shapes() -> None:
    print("== A1: handoff-summary schema shapes ==")
    try:
        validate("protocols/handoff-summary", _ready())
        _assert("READY validates", True)
    except Exception as e:
        _assert("READY validates", False, str(e).splitlines()[0])

    for status, key in (
        ("BLOCKED_PRE_STAGE_MISSING", "missing"),
        ("BLOCKED_PRE_STAGE_INVALID", "invalid"),
    ):
        payload = {
            "schemaVersion": 1, "generatedAt": "2026-05-29T00:00:00Z",
            "phase": "PRE_GENERATION", "status": status, key: ["something"],
        }
        try:
            validate("protocols/handoff-summary", payload)
            _assert(f"{status} validates", True)
        except Exception as e:
            _assert(f"{status} validates", False, str(e).splitlines()[0])

    # Negative: READY without its required facts must be rejected.
    broken = {
        "schemaVersion": 1, "generatedAt": "x", "phase": "PRE_GENERATION",
        "status": "READY",
    }
    try:
        validate("protocols/handoff-summary", broken)
        _assert("READY-without-facts rejected", False, "validated unexpectedly")
    except Exception:
        _assert("READY-without-facts rejected", True)


def case_budget_boundary() -> None:
    print("== M1: budget_enforcer.check boundary ==")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "execution-state.json"
        for cyc, expect_ok in ((10, True), (11, False)):
            p.write_text(
                json.dumps({"schemaVersion": 1, "cycle": cyc, "budget": {"maxCycles": 10}}),
                encoding="utf-8",
            )
            rc, _ = budget_enforcer.check(p)
            _assert(
                f"cycle={cyc} → {'ok' if expect_ok else 'exceeded'}",
                (rc == 0) == expect_ok,
                f"rc={rc}",
            )


def case_llm_budget_schema() -> None:
    print("== M4: llm-budget aggregate schema + overBudget flag ==")
    payload = {
        "schemaVersion": 2,
        "tokensPerByte": 0.25,
        "totals": {
            "suts": 2, "compactPackBytes": 24000, "estimatedTokensIn": 6000,
            "maxTokensIn": 4000, "overBudgetCount": 1,
        },
        "entries": [
            {"sut": "com.acme.SmallService", "contextPackBytes": 4000,
             "compactPackBytes": 4000, "estimatedTokensIn": 1000,
             "maxTokensIn": 4000, "overBudget": False, "truncatedFields": []},
            {"sut": "com.acme.HugeService", "contextPackBytes": 30000,
             "compactPackBytes": 20000, "estimatedTokensIn": 5000,
             "maxTokensIn": 4000, "overBudget": True, "truncatedFields": ["imports"]},
        ],
    }
    try:
        validate("protocols/llm-budget", payload)
        _assert("aggregate llm-budget validates", True)
    except Exception as e:
        _assert("aggregate llm-budget validates", False, str(e).splitlines()[0])
    # The over-budget entry's flag must agree with its estimate vs ceiling.
    huge = payload["entries"][1]
    _assert(
        "overBudget flag matches estimate>ceiling",
        huge["overBudget"] == (huge["estimatedTokensIn"] > huge["maxTokensIn"]),
    )


def main() -> int:
    case_schema_shapes()
    case_budget_boundary()
    case_llm_budget_schema()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} case(s): {FAILURES}")
        return 1
    print("All handoff/budget cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
