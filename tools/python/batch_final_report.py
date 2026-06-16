"""Build the final deterministic report for handoff-batch runs.

The batch runner owns generation/repair. This tool owns the final measurement:
run a full JaCoCo report after all generated tests pass, compute the delta
against state/jacoco-baseline.xml, and summarize the generated tests plus the
new coverage in _summaries/batch-final-report.{json,md}.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, atomic_write_json, atomic_write_text, load_json  # noqa: E402


def _mvn_prefix() -> list[str]:
    if os.name == "nt":
        return ["cmd", "/c", "mvn"]
    return ["mvn"]


def _run(cmd: list[str], *, cwd: Path) -> int:
    print("[RUN]", " ".join(str(x) for x in cmd), flush=True)
    return subprocess.run(cmd, cwd=str(cwd), text=True, check=False).returncode


def _latest_run_dir(state_dir: Path) -> Path | None:
    runs = state_dir / "_llm" / "runs"
    if not runs.exists():
        return None
    found = sorted(p for p in runs.glob("run-*") if p.is_dir())
    return found[-1] if found else None


def _load_json_or(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return load_json(path)
    except Exception:
        return default


def _run_jacoco(repo: Path, module: str | None) -> tuple[str, str]:
    cmd = _mvn_prefix() + [
        "-q",
        "-DfailIfNoTests=false",
        "org.jacoco:jacoco-maven-plugin:0.8.13:prepare-agent",
        "test",
        "org.jacoco:jacoco-maven-plugin:0.8.13:report",
    ]
    if module and module not in (".", ""):
        cmd += ["-pl", module, "-am"]
    rc = _run(cmd, cwd=repo)
    if rc != 0:
        return "FAIL", f"maven jacoco command exited rc={rc}"
    xml = repo / "target" / "site" / "jacoco" / "jacoco.xml"
    if not xml.exists():
        return "FAIL", f"JaCoCo XML not found after Maven run: {xml}"
    return "OK", str(xml)


def _compute_delta(state_dir: Path, repo: Path, coverage_mode: str, cycle: int) -> tuple[str, str]:
    before = state_dir / "jacoco-baseline.xml"
    after = repo / "target" / "site" / "jacoco" / "jacoco.xml"
    if not before.exists():
        return "FAIL", f"baseline not found: {before}"
    if not after.exists():
        return "FAIL", f"final jacoco.xml not found: {after}"
    out = state_dir / "coverage-delta.json"
    cmd = [
        sys.executable,
        str(HERE / "jacoco_parser.py"),
        "--mode", "delta",
        "--before", str(before),
        "--after", str(after),
        "--cycle", str(cycle),
        "--coverage-mode", coverage_mode,
        "--out", str(out),
    ]
    rc = _run(cmd, cwd=HERE.parents[1])
    if rc != 0:
        return "FAIL", f"jacoco_parser exited rc={rc}"
    return "OK", str(out)


def _counter(delta: dict, name: str) -> dict:
    return ((delta.get("totals") or {}).get(name) or {})


def _generated_tests(state_dir: Path) -> list[dict]:
    tests = (_load_json_or(state_dir / "generated-tests.json", {}) or {}).get("tests") or []
    out = []
    seen: set[tuple[str, str, str]] = set()
    for test in tests:
        key = (test.get("testClass", ""), test.get("sut", ""), test.get("patchId", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "testClass": test.get("testClass", ""),
            "sut": test.get("sut", ""),
            "status": test.get("status", ""),
            "patchId": test.get("patchId", ""),
            "evidenceIds": test.get("evidenceIds", []),
        })
    return out


def build_report(
    *,
    state_dir: Path,
    repo: Path,
    run_dir: Path | None,
    jacoco_status: str,
    jacoco_detail: str,
    delta_status: str,
    delta_detail: str,
) -> dict:
    run_dir = run_dir or _latest_run_dir(state_dir)
    manifest = _load_json_or(run_dir / "manifest.json", {}) if run_dir else {}
    delta = _load_json_or(state_dir / "coverage-delta.json", {})
    generated = _generated_tests(state_dir)
    changed_classes = []
    for item in delta.get("perClass") or []:
        lines = item.get("lines") or {}
        branches = item.get("branches") or {}
        if int(lines.get("delta", 0) or 0) or int(branches.get("delta", 0) or 0):
            changed_classes.append(item)
    return {
        "schemaVersion": 1,
        "kind": "batch-final-report",
        "runId": manifest.get("runId"),
        "repo": str(repo),
        "manifest": {
            "status": manifest.get("status"),
            "totals": manifest.get("totals", {}),
            "path": str(run_dir / "manifest.json") if run_dir else "",
        },
        "jacoco": {
            "status": jacoco_status,
            "detail": jacoco_detail,
        },
        "coverageDelta": {
            "status": delta_status,
            "detail": delta_detail,
            "totals": {
                "lines": _counter(delta, "lines"),
                "branches": _counter(delta, "branches"),
            },
            "regressions": delta.get("regressions", []),
            "changedClasses": changed_classes,
        },
        "generatedTests": generated,
    }


def _fmt_counter(counter: dict) -> str:
    if not counter:
        return "n/a"
    return f"{counter.get('before', 0)} -> {counter.get('after', 0)} ({counter.get('delta', 0):+})"


def render_markdown(report: dict) -> str:
    totals = report["coverageDelta"]["totals"]
    lines = [
        "# Batch Final Report",
        "",
        f"- Run: `{report.get('runId') or 'n/a'}`",
        f"- Manifest status: `{report['manifest'].get('status') or 'n/a'}`",
        f"- JaCoCo: `{report['jacoco']['status']}` - {report['jacoco']['detail']}",
        f"- Delta: `{report['coverageDelta']['status']}` - {report['coverageDelta']['detail']}",
        "",
        "## Coverage Delta",
        "",
        f"- Lines covered: {_fmt_counter(totals.get('lines') or {})}",
        f"- Branches covered: {_fmt_counter(totals.get('branches') or {})}",
        "",
        "## Generated Tests",
        "",
    ]
    tests = report.get("generatedTests") or []
    if tests:
        for test in tests:
            lines.append(
                f"- `{test.get('testClass')}` for `{test.get('sut')}` "
                f"({test.get('status')}, {test.get('patchId')})"
            )
    else:
        lines.append("- No generated tests recorded.")
    changed = report["coverageDelta"].get("changedClasses") or []
    lines += ["", "## Changed Coverage By Class", ""]
    if changed:
        for item in changed:
            lines.append(
                f"- `{item.get('fqcn')}`: lines {_fmt_counter(item.get('lines') or {})}; "
                f"branches {_fmt_counter(item.get('branches') or {})}"
            )
    else:
        lines.append("- No class-level coverage changes recorded.")
    regressions = report["coverageDelta"].get("regressions") or []
    lines += ["", "## Regressions", ""]
    if regressions:
        for reg in regressions:
            lines.append(f"- `{reg.get('fqcn')}` lines {reg.get('linesDelta')} branches {reg.get('branchesDelta')}")
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def write_report(report: dict, *, state_dir: Path, run_dir: Path | None) -> tuple[Path, Path]:
    summaries = state_dir / "_summaries"
    json_path = summaries / "batch-final-report.json"
    md_path = summaries / "batch-final-report.md"
    atomic_write_json(json_path, report)
    atomic_write_text(md_path, render_markdown(report))
    if run_dir:
        atomic_write_json(run_dir / "batch-final-report.json", report)
        atomic_write_text(run_dir / "batch-final-report.md", render_markdown(report))
    return json_path, md_path


def _canonical_run_dir(state_dir: Path, run_id: str) -> Path:
    """Same path formula as RunPaths in batch_runner — no orchestrator import needed."""
    return (state_dir / "_llm" / "runs" / run_id).resolve()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build final handoff-batch coverage report.")
    ap.add_argument("--state-dir", required=True, type=Path)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--run-dir", type=Path,
                    help="Explicit run directory. When --run-id is also given, "
                         "both must point to the same path (consistency guard).")
    ap.add_argument("--run-id", type=str, default=None,
                    help="Run id (e.g. run-20260616-000000). When provided, the "
                         "canonical run_dir is computed as state_dir/_llm/runs/<run_id>, "
                         "which must match --run-dir when both are specified.")
    ap.add_argument("--module", default=".")
    ap.add_argument("--coverage-mode", default="coverage")
    ap.add_argument("--skip-maven", action="store_true")
    args = ap.parse_args(argv)

    state_dir = args.state_dir.resolve()
    repo = args.repo.resolve()

    # Compute run_dir via run_id when provided (mirrors RunPaths, no import needed).
    # If both --run-id and --run-dir are given, validate they resolve to the same path
    # (guards against the mirror-folder drift that RunPaths prevents in the runner).
    if args.run_id:
        canonical = _canonical_run_dir(state_dir, args.run_id)
        if args.run_dir:
            given = args.run_dir.resolve()
            if given != canonical:
                print(
                    f"[batch_final_report] WARNING: --run-dir {given} does not match "
                    f"canonical path for --run-id {args.run_id!r} ({canonical}). "
                    "Using the canonical path derived from --run-id.",
                    flush=True,
                )
        run_dir: Path | None = canonical
    else:
        run_dir = args.run_dir.resolve() if args.run_dir else _latest_run_dir(state_dir)
    exec_state = _load_json_or(state_dir / "execution-state.json", {})
    cycle = int(exec_state.get("cycle", 1) or 1)

    with _TimedRun("batch_final_report") as tr:
        if args.skip_maven:
            jacoco_status, jacoco_detail = "SKIPPED", "maven execution skipped"
        else:
            jacoco_status, jacoco_detail = _run_jacoco(repo, args.module)
        if jacoco_status == "OK" or args.skip_maven:
            delta_status, delta_detail = _compute_delta(state_dir, repo, args.coverage_mode, cycle)
        else:
            delta_status, delta_detail = "SKIPPED", "JaCoCo report was not available"
        report = build_report(
            state_dir=state_dir,
            repo=repo,
            run_dir=run_dir,
            jacoco_status=jacoco_status,
            jacoco_detail=jacoco_detail,
            delta_status=delta_status,
            delta_detail=delta_detail,
        )
        json_path, md_path = write_report(report, state_dir=state_dir, run_dir=run_dir)
        tr.set_status("OK" if delta_status == "OK" else "WARN")
        tr.set_artifacts([str(json_path), str(md_path)])
        print(f"[OK] final batch report: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
