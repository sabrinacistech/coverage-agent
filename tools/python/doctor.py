"""doctor.py — environment & state diagnostic for the deterministic pipeline.

Performs a series of lightweight checks on the host machine and target repo:

  - jsonschema importable
  - lxml importable
  - mvn or mvnd available on PATH (mvnd preferred)
  - <repo>/pom.xml exists
  - <repo>/target/classes populated (WARN with fix hint when missing)
  - state/_schemas/ populated
  - templates/ populated
  - state/_schemas/protocols/ populated

The tool emits a single JSON document with this shape:

  {"schemaVersion": 1, "status": "OK|WARN|FAIL", "checks": [{...}]}

Each entry in `checks` always has `name` and `status`; optional `fix` describes
how to recover from a WARN/FAIL state.

Exit codes
----------
  0 — OK, or WARN (unless --strict)
  1 — FAIL, or WARN under --strict

Usage
-----
  python tools/python/doctor.py --repo <repo> --state state --json
  python tools/python/doctor.py --repo <repo> --state state --strict
"""
from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun  # noqa: E402


def _check_module(name: str) -> dict:
    try:
        importlib.import_module(name)
        return {"name": f"python:{name}", "status": "OK"}
    except Exception as exc:
        return {
            "name": f"python:{name}",
            "status": "FAIL",
            "fix": f"pip install {name}",
            "detail": str(exc),
        }


def _check_maven_tool() -> dict:
    mvnd = shutil.which("mvnd")
    if mvnd:
        return {"name": "build-tool", "status": "OK", "tool": "mvnd", "path": mvnd}
    mvn = shutil.which("mvn")
    if mvn:
        return {"name": "build-tool", "status": "OK", "tool": "mvn", "path": mvn}
    return {
        "name": "build-tool",
        "status": "FAIL",
        "fix": "Install Apache Maven (mvn) or Maven Daemon (mvnd) and add it to PATH",
    }


def _check_repo_pom(repo: Path) -> dict:
    pom = repo / "pom.xml"
    if pom.exists():
        return {"name": "repo:pom.xml", "status": "OK", "path": str(pom)}
    return {
        "name": "repo:pom.xml",
        "status": "FAIL",
        "fix": f"Provide a valid Maven repo at {repo} (pom.xml missing)",
    }


def _check_target_classes(repo: Path) -> dict:
    target = repo / "target" / "classes"
    if target.exists() and any(target.rglob("*.class")):
        return {"name": "repo:target/classes", "status": "OK", "path": str(target)}
    return {
        "name": "repo:target/classes",
        "status": "WARN",
        "fix": "mvn -q -DskipTests package",
        "detail": "no compiled .class files found; bytecode_scanner will yield nothing",
    }


def _check_dir_populated(name: str, path: Path, glob: str = "*") -> dict:
    if not path.exists():
        return {
            "name": name,
            "status": "FAIL",
            "fix": f"create and populate {path}",
        }
    matches = list(path.glob(glob))
    if not matches:
        return {
            "name": name,
            "status": "FAIL",
            "fix": f"{path} exists but is empty (expected entries matching {glob!r})",
        }
    return {"name": name, "status": "OK", "path": str(path), "count": len(matches)}


def run_checks(repo: Path, state: Path) -> list[dict]:
    checks: list[dict] = []
    checks.append(_check_module("jsonschema"))
    checks.append(_check_module("lxml"))
    checks.append(_check_maven_tool())
    checks.append(_check_repo_pom(repo))
    checks.append(_check_target_classes(repo))
    # Schemas live INSIDE the architecture repo (not in the user-writable
    # state dir). They are part of the architecture's contract, not generated
    # output — resolved via package-relative path, same as common.SCHEMAS_DIR.
    from common import SCHEMAS_DIR
    schemas_dir = SCHEMAS_DIR
    checks.append(
        _check_dir_populated("state:_schemas", schemas_dir, "*.schema.json")
    )
    protocols_dir = schemas_dir / "protocols"
    checks.append(
        _check_dir_populated(
            "state:_schemas/protocols", protocols_dir, "*.schema.json"
        )
    )
    repo_root = Path(__file__).resolve().parents[2]
    templates_dir = repo_root / "templates"
    checks.append(_check_dir_populated("templates", templates_dir, "*"))
    return checks


def aggregate_status(checks: list[dict]) -> str:
    if any(c["status"] == "FAIL" for c in checks):
        return "FAIL"
    if any(c["status"] == "WARN" for c in checks):
        return "WARN"
    return "OK"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run environment and state preflight checks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", required=True, help="Path to the Java repository under test.")
    ap.add_argument("--state", default="state", help="State directory (default: state).")
    ap.add_argument("--json", action="store_true", help="Emit JSON report on stdout.")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Treat WARN as FAIL (exit 1).",
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state = Path(args.state).resolve()

    checks = run_checks(repo, state)
    status = aggregate_status(checks)
    report = {"schemaVersion": 1, "status": status, "checks": checks}

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"doctor: {status}")
        for c in checks:
            line = f"  [{c['status']:4}] {c['name']}"
            if c.get("fix"):
                line += f"  fix: {c['fix']}"
            print(line)

    if status == "FAIL":
        return 1
    if status == "WARN" and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    with _TimedRun("doctor") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
