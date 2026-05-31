"""test_patcher_gates.py — gate + budget enforcement is folded into the patcher.

Proves the M2 by-construction guarantee: the only code path that writes Java
refuses to write when a gate blocks or the budget is exhausted.

Cases (patcher run as a subprocess against a temp repo):
  1. G2 block     : method without evidence → exit 3, no .java written
  2. budget block : execution-state cycle > maxCycles → exit 2, no .java written
  3. --no-gates   : same G2-invalid patch bypasses gates → exit 0, .java written

Run: `python tools/python/tests/test_patcher_gates.py`  (exits non-zero on failure)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent.parent  # java-test-coverage-architecture/
PATCHER = HERE.parent / "test_patch_applier.py"
TEMPLATES_DIR = PROJECT_ROOT / "templates"

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ OK ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        FAILURES.append(label)


def _scaffold(tmp: Path) -> tuple[Path, Path]:
    repo = tmp / "repo"
    state = tmp / "state"
    (repo / "src" / "test" / "java" / "com" / "acme").mkdir(parents=True)
    state.mkdir()
    return repo, state


def _target(repo: Path) -> Path:
    return repo / "src" / "test" / "java" / "com" / "acme" / "FooServiceTest.java"


def _patch(evidence_ids: list[str]) -> dict:
    return {
        "patchId": "patch:abc123",
        "schemaVersion": 1,
        "cycle": 1,
        "sut": "com.acme.FooService",
        "testClass": "com.acme.FooServiceTest",
        "testPackage": "com.acme",
        "template": "junit5-mockito",
        "targetDir": "src/test/java",
        "allowedImports": [],
        "fields": [],
        "methods": [
            {
                "name": "shouldDoThing",
                "annotations": ["@Test"],
                "body": "// arrange\n// act\n// assert\nassertTrue(true);",
                "evidenceIds": evidence_ids,
            }
        ],
    }


def _pack() -> dict:
    return {"sut": "com.acme.FooService", "allowedImports": [], "stack": {"test": "junit5"}}


def _run(repo: Path, state: Path, patch: dict, extra=None, pack_path=None, env=None):
    patch_path = state / "p.patch.json"
    patch_path.write_text(json.dumps(patch), encoding="utf-8")
    cmd = [
        sys.executable, str(PATCHER),
        "--patch", str(patch_path),
        "--repo", str(repo),
        "--state", str(state),
        "--templates", str(TEMPLATES_DIR),
        "--out", str(state / "generated-tests.json"),
    ]
    if pack_path:
        cmd += ["--context-pack", str(pack_path)]
    if extra:
        cmd += extra
    run_env = {**os.environ, **env} if env else None
    return subprocess.run(cmd, capture_output=True, text=True, env=run_env)


def case_g2_block() -> None:
    print("== G2 blocks write (method without evidence) ==")
    with tempfile.TemporaryDirectory() as td:
        repo, state = _scaffold(Path(td))
        pack_path = state / "pack.json"
        pack_path.write_text(json.dumps(_pack()), encoding="utf-8")
        proc = _run(repo, state, _patch([]), pack_path=pack_path)
        out = (proc.stdout or "") + (proc.stderr or "")
        _assert("exit 3", proc.returncode == 3, f"got {proc.returncode}: {out}")
        _assert("G2 reason present", "G2_SYMBOL_WITHOUT_EVIDENCE" in out, out)
        _assert("no .java written", not _target(repo).exists())


def case_budget_block() -> None:
    print("== budget blocks write (cycle > maxCycles) ==")
    with tempfile.TemporaryDirectory() as td:
        repo, state = _scaffold(Path(td))
        (state / "execution-state.json").write_text(
            json.dumps({"schemaVersion": 1, "cycle": 11, "budget": {"maxCycles": 10}}),
            encoding="utf-8",
        )
        pack_path = state / "pack.json"
        pack_path.write_text(json.dumps(_pack()), encoding="utf-8")
        proc = _run(repo, state, _patch([]), pack_path=pack_path)
        out = (proc.stdout or "") + (proc.stderr or "")
        _assert("exit 2", proc.returncode == 2, f"got {proc.returncode}: {out}")
        _assert("BUDGET_EXCEEDED present", "BUDGET_EXCEEDED" in out, out)
        _assert("no .java written", not _target(repo).exists())


def case_no_gates_writes() -> None:
    print("== --no-gates + env opt-in bypasses enforcement and writes ==")
    with tempfile.TemporaryDirectory() as td:
        repo, state = _scaffold(Path(td))
        proc = _run(repo, state, _patch([]), extra=["--no-gates"],
                    env={"TPA_ALLOW_NO_GATES": "1"})
        out = (proc.stdout or "") + (proc.stderr or "")
        _assert("exit 0", proc.returncode == 0, f"got {proc.returncode}: {out}")
        _assert(".java written", _target(repo).exists())


def case_no_gates_without_env_still_blocks() -> None:
    print("== --no-gates WITHOUT env is ignored — gates stay ON ==")
    with tempfile.TemporaryDirectory() as td:
        repo, state = _scaffold(Path(td))
        pack_path = state / "pack.json"
        pack_path.write_text(json.dumps(_pack()), encoding="utf-8")
        # Pass --no-gates but DO NOT set TPA_ALLOW_NO_GATES: the flag must be
        # ignored and G2 must still block the evidence-less patch.
        proc = _run(repo, state, _patch([]), extra=["--no-gates"], pack_path=pack_path)
        out = (proc.stdout or "") + (proc.stderr or "")
        _assert("exit 3 (gates enforced)", proc.returncode == 3, f"got {proc.returncode}: {out}")
        _assert("warns flag ignored", "ignored" in out.lower(), out)
        _assert("no .java written", not _target(repo).exists())


def case_no_perimeter_blocks() -> None:
    print("== gates ON but no --context-pack/--whitelist blocks (H-1c) ==")
    with tempfile.TemporaryDirectory() as td:
        repo, state = _scaffold(Path(td))
        # No pack, no whitelist, gates enforced: G1/G5 would be vacuous, so the
        # patcher must refuse rather than write unverified imports.
        proc = _run(repo, state, _patch([]))
        out = (proc.stdout or "") + (proc.stderr or "")
        _assert("exit 3", proc.returncode == 3, f"got {proc.returncode}: {out}")
        _assert("G1_NO_PERIMETER reason present", "G1_NO_PERIMETER" in out, out)
        _assert("no .java written", not _target(repo).exists())


def main() -> int:
    case_g2_block()
    case_budget_block()
    case_no_gates_writes()
    case_no_gates_without_env_still_blocks()
    case_no_perimeter_blocks()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} case(s): {FAILURES}")
        return 1
    print("All patcher-gate cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
