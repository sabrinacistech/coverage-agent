"""test_no_archived_agent_refs.py — Step 3 of the audit reconnection: archived
LLM agents must never be named as LIVE producers/consumers.

The audit found stale metadata (schema descriptions, doc roadmaps, the canonical
state table) naming agents that were archived to agents/_archive/ — e.g.
"leído por validation-agent y mutation-agent". An agent trusting those lines
treats phases 1-7 as live LLM turns and re-reads the nine raw JSONs directly,
exactly what the handoff gate forbids — evidence discipline eluded.

This tripwire scans the live docs/schemas and fails if an archived agent name
appears WITHOUT a historical marker nearby (ex-, antes vivía, migrado, stub,
wrapper, "no es un turno LLM", ...). Legit historical notes keep working; a new
bare reference (the bug class we just fixed) trips the test.

Allowlisted (these legitimately name archived agents): agents/_archive/ itself,
agents/README.md (the canonical "archived → replacement" migration map), and the
audit reports CORRECTIVE_REPORT.md / REFACTOR_REPORT.md.

Run: `python tools/python/tests/test_no_archived_agent_refs.py`  (non-zero on failure)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARCH_ROOT = HERE.parents[2]  # tools/python/tests → tools/python → tools → <root>

ARCHIVED = [
    "classification-agent", "dependency-graph-agent", "discovery-agent",
    "fixture-agent", "mutation-agent", "planning-agent", "reporting-agent",
    "repository-intelligence-agent", "stack-profile-agent",
    "symbol-contract-agent", "validation-agent",
]

# A reference is allowed when one of these appears within WINDOW chars of the
# name — i.e. the line explicitly frames the agent as historical/replaced.
HISTORICAL_MARKERS = [
    "antes vivía", "antes vivian", "antes vivían", "heredad", "ex-", "ex_",
    "migrad", "degradad", "stub", "_archive", "consolidad", "anteriormente",
    "reemplaz", "histó", "histo", "wrapper", "ya no", "deprecated", "archivad",
    "sin llm", "no llm", "turno llm",
]
WINDOW = 220

# Paths that legitimately name archived agents (relative to ARCH_ROOT).
ALLOWLIST = {
    Path("agents/README.md"),
    Path("CORRECTIVE_REPORT.md"),
    Path("REFACTOR_REPORT.md"),
}

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ OK ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        FAILURES.append(label)


def _is_excluded(rel: Path) -> bool:
    parts = rel.parts
    if "_archive" in parts:
        return True
    if parts[:3] == ("tools", "python", "tests"):
        return True
    return rel in ALLOWLIST


def _scan() -> tuple[list[str], int, int]:
    violations: list[str] = []
    scanned = 0
    allowed = 0
    name_re = re.compile("|".join(re.escape(n) for n in ARCHIVED))
    for path in [*ARCH_ROOT.rglob("*.md"), *ARCH_ROOT.rglob("*.json")]:
        rel = path.relative_to(ARCH_ROOT)
        if _is_excluded(rel):
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        for m in name_re.finditer(text):
            s = max(0, m.start() - WINDOW)
            e = min(len(text), m.end() + WINDOW)
            window = low[s:e]
            if any(mk in window for mk in HISTORICAL_MARKERS):
                allowed += 1
                continue
            line_no = text.count("\n", 0, m.start()) + 1
            line = text.splitlines()[line_no - 1].strip()
            violations.append(f"{rel}:{line_no}: bare ref `{m.group(0)}` → {line}")
    return violations, scanned, allowed


def case_no_bare_archived_refs() -> None:
    print("== no archived agent named as a live producer/consumer ==")
    violations, scanned, allowed = _scan()
    _assert("scanned a non-trivial set of files", scanned > 20, f"scanned={scanned}")
    _assert("known historical references were seen and allowed", allowed > 0,
            f"allowed={allowed} (the marker logic may be broken)")
    _assert("zero bare archived-agent references",
            not violations, "\n    " + "\n    ".join(violations))


def main() -> int:
    case_no_bare_archived_refs()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} case(s): {FAILURES}")
        return 1
    print("All archived-agent-reference cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
