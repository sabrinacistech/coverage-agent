# tools/python/run_all_deterministic.py
"""Run every deterministic coverage-agent stage from a plain console.

Goal: reproduce what run_coverage.ps1 does (analysis -> JaCoCo -> optional cycle
loop) without driving it through an IDE agent (Copilot / Claude Code). That makes
the deterministic pre-stage faster and stops burning LLM tokens on commands a
script can launch on its own.

Deterministic order (why it is shaped like this)
-------------------------------------------------
  A. CONTRACTS pre-pass     run_pipeline with only the `pom` + `archetype` steps.
                            These write build-tool-contract.json and
                            archetype-profile.json — the artifacts the JaCoCo
                            guard reads. (Cached, so step D reuses them.)
  B. JaCoCo verification    jacoco_pom_guard.py — the one deterministic gate that
                            decides, per module, whether the project POM needs the
                            jacoco-maven-plugin. --check reports; --apply injects.
  C. Maven baseline         mvn ...:prepare-agent test ...:report → generates
                            target/ and target/site/jacoco/jacoco.xml.
  D. Full Fase 0            run_pipeline WITH --jacoco-xml → coverage-targets.json
                            and the full handoff (pom/archetype are CACHE HITs).
  E. execution-state.json   budget seeded so the loop honours --max-cycles.
  F. (optional) cycle loop  --start-cycle-loop.

--skip-jacoco short-circuits A/B/C and requires an existing jacoco.xml (step D only).

Examples
--------
  # Full deterministic baseline (verify JaCoCo, build it, analyse):
  python tools/python/run_all_deterministic.py \
      --repo /c/repoVC/multi-clusters/cluster-status-service \
      --state-dir /c/repoVC/agent-state-multiclusters --clean

  # Reuse an existing jacoco.xml, analysis only:
  python tools/python/run_all_deterministic.py \
      --repo .../cluster-status-service --state-dir .../agent-state --skip-jacoco
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Steps to SKIP in the contracts pre-pass (A): everything except pom + archetype,
# which are all the JaCoCo guard needs. Keeping the list explicit (run_pipeline has
# no "run-only" flag) means a new pipeline step is skipped here until reviewed.
CONTRACT_PRESTAGE_SKIP = [
    "generated", "classpath", "stack", "bytecode", "source", "jacoco",
    "index", "classification", "deps", "fixtures", "planning",
    "incremental", "validate", "context",
]


def _fmt(cmd: list[str]) -> str:
    return " ".join(str(x) for x in cmd)


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    """Run a command; abort the whole script on a non-zero exit."""
    print("\n[RUN]", _fmt(cmd))
    rc = subprocess.run(cmd, cwd=str(cwd), env=env, text=True, check=False).returncode
    if rc != 0:
        # NOTE: str(x) — cmd may carry Path objects, so a bare join would raise.
        raise SystemExit(f"[FAIL] command exited with rc={rc}: {_fmt(cmd)}")


def run_soft(cmd: list[str], cwd: Path, env: dict[str, str], ok_codes=(0,)) -> int:
    """Run a command but DON'T abort on the listed non-fatal exit codes.

    Used for the JaCoCo verification: in --check mode it only reports, and in
    --apply mode rc=3 means "forbidden" (parent POM already provides JaCoCo) — a
    legitimate decision, not a failure. The Maven baseline below invokes the JaCoCo
    goals directly on the CLI, so it produces jacoco.xml regardless of the POM.
    """
    print("\n[RUN]", _fmt(cmd))
    rc = subprocess.run(cmd, cwd=str(cwd), env=env, text=True, check=False).returncode
    if rc not in ok_codes:
        print(f"[WARN] command rc={rc} (continuing): {_fmt(cmd)}")
    return rc


def mvn_prefix() -> list[str]:
    """Command prefix that launches Maven correctly per-platform.

    On Windows the real launcher is mvn.cmd, but CreateProcess cannot execute a
    .cmd/.bat directly (WinError 193) — and shutil.which("mvn") in a Git-Bash PATH
    may even return the extension-less Unix wrapper (C:\\maven\\bin\\mvn), which is
    also not a valid Win32 image. Going through `cmd /c mvn` lets cmd.exe resolve
    mvn.cmd via PATHEXT and run it. On POSIX we resolve and run the binary directly.
    """
    if os.name == "nt":
        return ["cmd", "/c", "mvn"]
    found = shutil.which("mvn")
    if not found:
        raise SystemExit("[FAIL] 'mvn' not found on PATH. Install it or add it to PATH.")
    return [found]


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
        help="Wipe --state-dir before the run (mirrors run_coverage.ps1). "
        "Destructive: removes all prior state for a clean baseline.",
    )
    parser.add_argument(
        "--skip-jacoco",
        action="store_true",
        help="Skip the JaCoCo verify + Maven baseline (steps A/B/C). Requires an "
        "existing target/site/jacoco/jacoco.xml.",
    )
    parser.add_argument(
        "--apply-jacoco-pom",
        action="store_true",
        help="Run the JaCoCo guard in --apply mode (inject jacoco-maven-plugin into "
        "the project POM for modules whose decision is 'add'). Default: --check "
        "(verify/report only, never writes into the target project).",
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

    tools = agent_root / "tools" / "python"
    run_pipeline = tools / "run_pipeline.py"
    jacoco_guard = tools / "jacoco_pom_guard.py"
    cycle_loop = tools / "cycle_loop.py"
    for required in (run_pipeline, jacoco_guard):
        if not required.exists():
            raise SystemExit(f"[FAIL] required tool not found: {required}")
    if args.start_cycle_loop and not cycle_loop.exists():
        raise SystemExit(f"[FAIL] cycle_loop.py not found: {cycle_loop}")

    env = base_env()

    if args.clean and state_dir.exists():
        print(f"[CLEAN] removing state dir: {state_dir}")
        shutil.rmtree(state_dir)

    if not args.skip_jacoco:
        # ── A. CONTRACTS pre-pass (pom + archetype only) ─────────────────────
        # The JaCoCo guard reads build-tool-contract.json + archetype-profile.json,
        # so they must exist before B. These steps are cached, so the full Fase 0
        # in D reuses them ([CACHE HIT]).
        print("\n==== [A] Contracts pre-pass (pom + archetype) ====")
        run(
            [
                python, str(run_pipeline),
                "--repo", str(repo),
                "--out", str(state_dir),
                "--module", args.module,
                "--coverage-mode", args.coverage_mode,
                "--skip", *CONTRACT_PRESTAGE_SKIP,
            ],
            cwd=agent_root,
            env=env,
        )

        # ── B. JaCoCo verification (the deterministic POM gate) ──────────────
        print("\n==== [B] JaCoCo verification (jacoco_pom_guard) ====")
        guard_cmd = [
            python, str(jacoco_guard),
            "--state", str(state_dir),
            "--module", args.module,
        ]
        if args.apply_jacoco_pom:
            # rc=3 → "forbidden" (parent POM provides JaCoCo): a valid decision.
            run_soft(guard_cmd + ["--apply"], cwd=agent_root, env=env, ok_codes=(0, 3))
        else:
            run_soft(guard_cmd + ["--check"], cwd=agent_root, env=env, ok_codes=(0,))

        # ── C. Maven baseline → target/ + jacoco.xml ─────────────────────────
        # CLI goals invoke the JaCoCo plugin directly, so the report is produced
        # whether or not the plugin is declared in the POM.
        print("\n==== [C] Maven baseline (JaCoCo report) ====")
        run(
            mvn_prefix() + [
                "-q", "-DfailIfNoTests=false",
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

    # ── D. Full Fase 0 with the JaCoCo report ────────────────────────────────
    print("\n==== [D] Full Fase 0 (run_pipeline --jacoco-xml) ====")
    run(
        [
            python, str(run_pipeline),
            "--repo", str(repo),
            "--out", str(state_dir),
            "--module", args.module,
            "--jacoco-xml", str(jacoco_xml),
            "--coverage-mode", args.coverage_mode,
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

    print("\n==== [F] Cycle loop ====")
    run(
        [
            python, str(cycle_loop),
            "--state", str(state_file),
            "--state-dir", str(state_dir),
            "--",
            python, "-m", "orchestrator.one_cycle",
            "--state-dir", str(state_dir),
            "--repo", str(repo),
        ],
        cwd=agent_root,
        env=loop_env,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
