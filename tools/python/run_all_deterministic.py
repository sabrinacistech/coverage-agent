# tools/python/run_all_deterministic.py
"""Run every deterministic coverage-agent stage from a plain console.

Goal: reproduce what run_coverage.ps1 does (JaCoCo baseline -> Fase 0 ->
optional cycle loop) without driving it through an IDE agent (Copilot / Claude
Code). That makes the deterministic pre-stage faster and stops burning LLM
tokens on commands a script can launch on its own.

Examples
--------
  # Fase 0 only (analysis), reusing an existing JaCoCo report:
  python tools/python/run_all_deterministic.py \
      --repo C:\\repo\\multi-clusters\\cluster-status-service \
      --state-dir C:\\repo\\agent-state-multiclusters \
      --skip-jacoco

  # Full deterministic baseline + start the generation/repair loop:
  python tools/python/run_all_deterministic.py \
      --repo .../cluster-status-service \
      --state-dir .../agent-state \
      --clean --start-cycle-loop
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _fmt(cmd: list[str]) -> str:
    return " ".join(str(x) for x in cmd)


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    print("\n[RUN]", _fmt(cmd))
    completed = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        # NOTE: str(x) — cmd may carry Path objects, so a bare join would raise.
        raise SystemExit(
            f"[FAIL] command exited with rc={completed.returncode}: {_fmt(cmd)}"
        )


def resolve_exe(name: str) -> str:
    """Resolve an executable on PATH.

    On Windows CreateProcess does NOT honour PATHEXT, so subprocess.run(["mvn",
    ...]) raises FileNotFoundError because the real file is mvn.cmd. shutil.which
    consults PATHEXT and returns the full path, fixing that.
    """
    found = shutil.which(name)
    if not found:
        raise SystemExit(
            f"[FAIL] '{name}' not found on PATH. Install it or add it to PATH."
        )
    return found


def base_env() -> dict[str, str]:
    """Child-process env forced to UTF-8.

    The deterministic tools print Unicode (acentos, '→'); under the Windows
    default cp1252 those prints crash with UnicodeEncodeError. run_coverage.ps1
    sets the same two vars for exactly this reason.
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def ensure_execution_state(
    state_dir: Path, max_cycles: int, max_minutes_per_cycle: int
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "execution-state.json"

    if state_file.exists():
        print(f"[OK] execution-state.json already exists: {state_file}")
        return state_file

    # Field names mirror what budget_enforcer.py (budget.maxCycles /
    # budget.maxMinutesPerCycle, top-level cycle) and cycle_loop.py
    # (consecutiveZeroDeltaCycles, compileFailRateWindow) actually read. If we
    # did not pre-create this, the loop would auto-create an empty budget and
    # silently fall back to the built-in defaults, ignoring --max-cycles.
    payload = {
        "schemaVersion": 1,
        "cycle": 0,
        "phase": "generation",
        "budget": {
            "maxCycles": max_cycles,
            "maxMinutesPerCycle": max_minutes_per_cycle,
        },
        "consecutiveZeroDeltaCycles": 0,
        "compileFailRateWindow": [],
        "checkpoints": [],
    }

    state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[OK] created {state_file}")
    return state_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run coverage-agent deterministic stages without asking the "
        "IDE to approve each command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--agent-root", default=".", help="Path to coverage-agent repo")
    parser.add_argument("--repo", required=True, help="Target Java repo or module path")
    parser.add_argument("--state-dir", required=True, help="External state directory")
    parser.add_argument(
        "--module", default=".", help='Maven module. Use "." for single-module'
    )
    parser.add_argument(
        "--coverage-mode",
        default="coverage",
        choices=["coverage", "branch-coverage", "mutation-hardening"],
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe --state-dir before Fase 0 (mirrors run_coverage.ps1). "
        "Destructive: removes all prior state for a clean baseline.",
    )
    parser.add_argument(
        "--skip-jacoco", action="store_true", help="Do not run Maven JaCoCo bootstrap"
    )
    parser.add_argument("--max-cycles", type=int, default=20)
    parser.add_argument("--max-minutes-per-cycle", type=int, default=10)
    parser.add_argument(
        "--start-cycle-loop",
        action="store_true",
        help="Start cycle_loop after pre-stage",
    )
    parser.add_argument(
        "--llm-provider",
        default="ide",
        help='Usually "ide" for VS Code/Claude/Copilot handoff',
    )

    args = parser.parse_args()

    agent_root = Path(args.agent_root).resolve()
    repo = Path(args.repo).resolve()
    state_dir = Path(args.state_dir).resolve()

    python = sys.executable
    jacoco_xml = repo / "target" / "site" / "jacoco" / "jacoco.xml"

    if not agent_root.exists():
        raise SystemExit(f"[FAIL] agent root does not exist: {agent_root}")

    if not repo.exists():
        raise SystemExit(f"[FAIL] target repo does not exist: {repo}")

    run_pipeline = agent_root / "tools" / "python" / "run_pipeline.py"
    cycle_loop = agent_root / "tools" / "python" / "cycle_loop.py"
    if not run_pipeline.exists():
        raise SystemExit(f"[FAIL] run_pipeline.py not found: {run_pipeline}")
    if args.start_cycle_loop and not cycle_loop.exists():
        raise SystemExit(f"[FAIL] cycle_loop.py not found: {cycle_loop}")

    env = base_env()

    if args.clean and state_dir.exists():
        print(f"[CLEAN] removing state dir: {state_dir}")
        shutil.rmtree(state_dir)

    if not args.skip_jacoco:
        mvn = resolve_exe("mvn")
        run(
            [
                mvn,
                "-q",
                "-DfailIfNoTests=false",
                "org.jacoco:jacoco-maven-plugin:0.8.13:prepare-agent",
                "test",
                "org.jacoco:jacoco-maven-plugin:0.8.13:report",
            ],
            cwd=repo,
            env=env,
        )

    if not jacoco_xml.exists():
        raise SystemExit(
            f"[FAIL] JaCoCo XML not found: {jacoco_xml}\n"
            "Run without --skip-jacoco or check the Maven build."
        )

    run(
        [
            python,
            str(run_pipeline),
            "--repo",
            str(repo),
            "--out",
            str(state_dir),
            "--module",
            args.module,
            "--jacoco-xml",
            str(jacoco_xml),
            "--coverage-mode",
            args.coverage_mode,
        ],
        cwd=agent_root,
        env=env,
    )

    state_file = ensure_execution_state(
        state_dir=state_dir,
        max_cycles=args.max_cycles,
        max_minutes_per_cycle=args.max_minutes_per_cycle,
    )

    print("\n[OK] deterministic pre-stage completed.")
    print(f"[OK] state-dir: {state_dir}")
    print(f"[OK] execution-state: {state_file}")

    if not args.start_cycle_loop:
        print(
            "\n[NEXT] To start the generation/repair loop, rerun with "
            "--start-cycle-loop."
        )
        return 0

    loop_env = dict(env)
    loop_env["COVAGENT_LLM_PROVIDER"] = args.llm_provider

    run(
        [
            python,
            str(cycle_loop),
            "--state",
            str(state_file),
            "--state-dir",
            str(state_dir),
            "--",
            python,
            "-m",
            "orchestrator.one_cycle",
            "--state-dir",
            str(state_dir),
            "--repo",
            str(repo),
        ],
        cwd=agent_root,
        env=loop_env,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
