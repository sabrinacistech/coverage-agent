"""narrow_test_runner.py — run a single test class against a Maven repo.

Resolves the build tool (mvnd preferred, falls back to mvn) and the target
module (from state/build-tool-contract.json when not explicitly supplied),
then executes:

  <tool> -T 1C -o -Dtest=<SimpleName> -DfailIfNoTests=false test
         [-pl <module> -am]
         -Djacoco.destFile=target/jacoco-narrow.exec

stdout+stderr are mirrored into state/_summaries/build-output.log. On failure,
if tools/python/compile_error_parser.py exists, it is invoked to index errors
into state/compile-error-index.json, and a state/_summaries/last-failure.json
record is written.

The runner NEVER invokes `mvn clean`.

Usage
-----
  python tools/python/narrow_test_runner.py \\
      --repo <repo> --state state --test-class com.acme.FooServiceTest \\
      [--module <module-name>]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun  # noqa: E402


def _resolve_tool() -> str | None:
    return shutil.which("mvnd") or shutil.which("mvn")


def _simple_name(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[-1]


def _resolve_module(state_dir: Path, override: str | None) -> str | None:
    if override:
        return override
    contract = state_dir / "build-tool-contract.json"
    if not contract.exists():
        return None
    try:
        with contract.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    modules = data.get("modules", []) or []
    non_pom = [m for m in modules if m.get("packaging") != "pom"]
    if non_pom:
        return non_pom[0].get("name")
    return None


def _write_failure_record(
    state_dir: Path,
    test_class: str,
    module: str | None,
    cmd: list[str],
    rc: int,
    duration_ms: int,
    compile_error_index: Path | None,
) -> None:
    summaries = state_dir / "_summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    record = {
        "schemaVersion": 1,
        "kind": "narrow-test",
        "testClass": test_class,
        "module": module,
        "command": cmd,
        "exitCode": rc,
        "durationMs": duration_ms,
        "buildLog": str(summaries / "build-output.log"),
        "compileErrorIndex": str(compile_error_index) if compile_error_index else None,
    }
    out = summaries / "last-failure.json"
    with out.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)


def _run_compile_error_parser(state_dir: Path, log_path: Path) -> Path | None:
    parser = HERE / "compile_error_parser.py"
    if not parser.exists():
        return None
    out_path = state_dir / "compile-error-index.json"
    cmd = [
        sys.executable,
        str(parser),
        "--format", "auto",
        "--log", str(log_path),
        "--out", str(out_path),
        "--run", f"narrow-{int(time.time())}",
    ]
    try:
        subprocess.run(cmd, check=False)
    except Exception as exc:
        print(f"[WARN] compile_error_parser invocation failed: {exc}", file=sys.stderr)
        return None
    return out_path if out_path.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run a single test class against a Maven module (narrow loop).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", required=True, help="Maven repo root.")
    ap.add_argument("--state", default="state", help="State directory (default: state).")
    ap.add_argument(
        "--test-class",
        required=True,
        help="Fully-qualified name of the test class to run.",
    )
    ap.add_argument(
        "--module",
        default=None,
        help="Maven module name. Inferred from state/build-tool-contract.json if absent.",
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state_dir = Path(args.state).resolve()

    if not (repo / "pom.xml").exists():
        print(f"[FAIL] No pom.xml at {repo}", file=sys.stderr)
        return 2

    tool = _resolve_tool()
    if not tool:
        print("[FAIL] Neither mvnd nor mvn found on PATH.", file=sys.stderr)
        return 2

    module = _resolve_module(state_dir, args.module)
    simple = _simple_name(args.test_class)

    jacoco_exec = repo / "target" / "jacoco-narrow.exec"
    cmd: list[str] = [
        tool,
        "-T", "1C",
        "-o",
        f"-Dtest={simple}",
        "-DfailIfNoTests=false",
        f"-Djacoco.destFile={jacoco_exec}",
        "test",
    ]
    if module:
        cmd += ["-pl", module, "-am"]

    summaries = state_dir / "_summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    log_path = summaries / "build-output.log"

    print(f"[INFO] tool={Path(tool).name} module={module or '<root>'} test={simple}")
    t0 = time.perf_counter()
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(repo),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                fh.write(line)
                sys.stdout.write(line)
            rc = proc.wait()
    except FileNotFoundError as exc:
        print(f"[FAIL] cannot execute {tool}: {exc}", file=sys.stderr)
        return 2
    duration_ms = int((time.perf_counter() - t0) * 1000)

    if rc != 0:
        compile_idx = _run_compile_error_parser(state_dir, log_path)
        _write_failure_record(
            state_dir, args.test_class, module, cmd, rc, duration_ms, compile_idx
        )
        print(f"[FAIL] tests failed (exit {rc}); see {log_path}", file=sys.stderr)
        return rc

    print(f"[OK] tests passed in {duration_ms} ms; jacoco at {jacoco_exec}")
    return 0


if __name__ == "__main__":
    with _TimedRun("narrow_test_runner") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
