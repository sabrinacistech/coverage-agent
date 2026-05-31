"""test_boot_routing.py — Step 2 of the audit reconnection: the single entry
point routes every cycle through the loop owner.

The audit found BOOT.md (the one documented entry point) described the per-cycle
procedure but NEVER named cycle_loop.py, while coverage-orchestrator.md declares
cycle_loop the sole loop owner and warns that running gate_runner /
test_patch_applier outside it leaves the budget backstop inert. Two canonical
docs loaded together thus contradicted each other on the only mechanism that
enforces finitude — an orchestrator booting strictly from BOOT could run
generation→patch directly and never tick `cycle`, so the budget never tripped.

This doc-test locks the fix so the contradiction cannot silently return:
  - BOOT.md references cycle_loop.py and carries the prohibition on invoking
    gate_runner / test_patch_applier outside the wrapper.
  - coverage-orchestrator.md still names cycle_loop as the single loop owner
    (the two docs agree).

Run: `python tools/python/tests/test_boot_routing.py`  (exits non-zero on failure)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARCH_ROOT = HERE.parents[2]  # tools/python/tests → tools/python → tools → <root>
BOOT = ARCH_ROOT / "BOOT.md"
ORCH = ARCH_ROOT / "agents" / "coverage-orchestrator.md"

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ OK ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        FAILURES.append(label)


def case_boot_routes_through_cycle_loop() -> None:
    print("== BOOT.md routes the cycle through cycle_loop.py ==")
    _assert("BOOT.md exists", BOOT.exists(), str(BOOT))
    text = BOOT.read_text(encoding="utf-8") if BOOT.exists() else ""

    _assert("BOOT.md names cycle_loop.py", "cycle_loop.py" in text)
    _assert("BOOT.md shows the cycle_loop invocation command",
            bool(re.search(r"python\s+tools/python/cycle_loop\.py", text)),
            "no `python tools/python/cycle_loop.py` command block")
    _assert("BOOT.md prohibits running the patcher/gate_runner outside the wrapper",
            ("test_patch_applier" in text and "gate_runner" in text
             and ("a pelo" in text or "fuera de" in text or "fuera del" in text)),
            "missing the 'do not run ... outside the wrapper' prohibition")


def case_docs_agree_on_loop_owner() -> None:
    print("== BOOT.md and coverage-orchestrator.md agree cycle_loop owns the loop ==")
    _assert("orchestrator doc exists", ORCH.exists(), str(ORCH))
    otext = ORCH.read_text(encoding="utf-8") if ORCH.exists() else ""
    _assert("orchestrator still calls cycle_loop the único dueño del loop",
            "cycle_loop" in otext and "dueño del loop" in otext)


def main() -> int:
    case_boot_routes_through_cycle_loop()
    case_docs_agree_on_loop_owner()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} case(s): {FAILURES}")
        return 1
    print("All BOOT-routing cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
