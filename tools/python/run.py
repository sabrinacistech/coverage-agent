"""run.py — single-command entry point for the deterministic pipeline.

Sequence
--------
  1. doctor.py --json           (abort on FAIL)
  2. bootstrap.py               (when present)
  3. run_pipeline.py            (forward --sut when supplied)

After all three succeed, print a single READY line directing callers to the
compact context pack as the LLM input. This script intentionally does NOT
implement any LLM loop — patch generation and gating happen outside.

Usage
-----
  python tools/python/run.py --repo <repo> --state state --mode coverage [--sut <fqcn>]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun  # noqa: E402


def _run(cmd: list[str]) -> int:
    print(f"[INFO] $ {' '.join(cmd)}")
    return subprocess.call(cmd)


def _run_doctor(repo: Path, state: Path) -> int:
    doctor = HERE / "doctor.py"
    cmd = [
        sys.executable,
        str(doctor),
        "--repo", str(repo),
        "--state", str(state),
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    try:
        report = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        report = {"status": "UNKNOWN"}
    if proc.returncode != 0 or report.get("status") == "FAIL":
        print("[FAIL] doctor reports FAIL; aborting.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run doctor + bootstrap + pipeline deterministically (no LLM loop).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", required=True, help="Java repo root.")
    ap.add_argument("--state", default="state", help="State directory (default: state).")
    ap.add_argument(
        "--mode",
        default="coverage",
        choices=["coverage", "branch-coverage", "mutation-hardening"],
        help="Coverage scoring mode (default: coverage).",
    )
    ap.add_argument(
        "--sut",
        default=None,
        help="Optional fully-qualified class name; forwarded to context_pack_builder.",
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state = Path(args.state).resolve()

    # 1. Doctor
    if _run_doctor(repo, state) != 0:
        return 1

    # 2. Bootstrap (preferred entry point). Falls through to run_pipeline if absent.
    bootstrap = HERE / "bootstrap.py"
    pipeline = HERE / "run_pipeline.py"

    if bootstrap.exists():
        cmd = [
            sys.executable,
            str(bootstrap),
            "--repo", str(repo),
            "--out", str(state),
            "--coverage-mode", args.mode,
        ]
        rc = _run(cmd)
        if rc != 0:
            print("[FAIL] bootstrap failed.", file=sys.stderr)
            return rc

    # 3. run_pipeline (always; honours --sut for context-pack scoping).
    if not pipeline.exists():
        print(f"[FAIL] missing {pipeline}", file=sys.stderr)
        return 2
    cmd = [
        sys.executable,
        str(pipeline),
        "--repo", str(repo),
        "--out", str(state),
        "--coverage-mode", args.mode,
    ]
    if args.sut:
        cmd += ["--sut", args.sut]
    rc = _run(cmd)
    if rc != 0:
        print("[FAIL] run_pipeline failed.", file=sys.stderr)
        return rc

    if args.sut:
        hint = f"state/context-packs-compact/{args.sut}.json"
    else:
        hint = "state/context-packs-compact/<fqcn>.json"
    print(
        f"READY. Use {hint} as LLM input and produce patch JSON."
    )
    return 0


if __name__ == "__main__":
    with _TimedRun("run") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
