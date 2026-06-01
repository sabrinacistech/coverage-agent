"""repo_intelligence.py — deterministic repository intelligence orchestrator.

Invoca en orden las 5 tools Python determinísticas (fase determinista, no un turno
LLM) que materializan la inteligencia estructural del repo: stack profile,
contratos de símbolos, índice semántico, classification
y dependency graph. Termina validando el state y persistiendo un manifest con
los hashes de los outputs.

Procedimiento (idéntico al prompt original):

  1. semantic_index_writer       → state/index/*.json
  2. stack_profile_detector      → state/stack-profile.json
  3. bytecode_scanner            → state/symbol-contracts/<fqcn>.json  (si --module)
  4. source_symbol_enricher      → enrich contracts (FreeBuilder/Lombok)
  5. classification_analyzer     → state/classification-index.json
  6. dependency_graph_extractor  → state/dependency-graph.json
  7. state_validator             → valida todos los state/*.json
  8. Persistir manifest          → state/_summaries/repo-intelligence.json

Cada paso es idempotente: misma entrada produce el mismo output byte-exact.
`run_pipeline.py` ejecuta el superset; este script existe para re-correr
SÓLO el subset de inteligencia tras un cambio incremental, sin pagar el
costo de pom_parser/archetype/classpath/jacoco/planning.

CLI
---
  python tools/python/repo_intelligence.py \\
      --repo .  --out state \\
      [--module service-foo] \\
      [--include-fqcn 'com\\.acme\\..*'] \\
      [--skip stack,bytecode,...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, atomic_write_json  # noqa: E402

SCHEMA_VERSION = 1

# Step names accepted by --skip. Mirror the order of execution in main().
_STEP_SKIP_NAMES = (
    "index", "stack", "bytecode", "source",
    "classification", "deps", "validate",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_step(label: str, cmd: list[str]) -> int:
    print(f"[repo-intelligence] >> {label}: {' '.join(cmd[1:])}", file=sys.stderr)
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        print(f"[repo-intelligence] FAIL step '{label}' rc={proc.returncode}", file=sys.stderr)
    return proc.returncode


def _expected_outputs(state_dir: Path) -> dict[str, Path]:
    """Files we expect to exist after a full run; used for the manifest."""
    return {
        "stack-profile":         state_dir / "stack-profile.json",
        "classification-index":  state_dir / "classification-index.json",
        "dependency-graph":      state_dir / "dependency-graph.json",
        "index.classes":         state_dir / "index" / "classes.json",
        "index.methods":         state_dir / "index" / "methods.json",
        "index.annotations":     state_dir / "index" / "annotations.json",
        "index.imports":         state_dir / "index" / "imports.json",
        "index.dependencies":    state_dir / "index" / "dependencies.json",
    }


def _write_manifest(state_dir: Path, steps_run: list[dict]) -> Path:
    summaries = state_dir / "_summaries"
    summaries.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict] = []
    for name, path in _expected_outputs(state_dir).items():
        artifacts.append({
            "name": name,
            "path": str(path.relative_to(state_dir.parent)) if path.is_relative_to(state_dir.parent) else str(path),
            "exists": path.exists(),
            "sha256": _sha256(path) if path.exists() else None,
        })

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "runAt": datetime.now(timezone.utc).isoformat(),
        "steps": steps_run,
        "artifacts": artifacts,
    }
    out = summaries / "repo-intelligence.json"
    atomic_write_json(out, manifest)
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Deterministic repository intelligence orchestrator (deterministic "
            "phase, not an LLM turn). Runs the 5 Python tools that "
            "materialise structural intelligence: stack profile, symbol "
            "contracts, semantic index, classification and dependency graph."
        )
    )
    ap.add_argument("--repo", required=True, help="Repository root.")
    ap.add_argument("--out", required=True, help="State directory.")
    ap.add_argument("--module", default=None,
                    help="Maven module for bytecode scan. Required for symbol contracts.")
    ap.add_argument("--include-fqcn", default=".*", dest="include_fqcn",
                    help="Regex passed to bytecode_scanner --include.")
    ap.add_argument("--full-index", action="store_true",
                    help="Pass --full to semantic_index_writer.")
    ap.add_argument(
        "--skip",
        default="",
        help=(
            f"Comma-separated step names to skip "
            f"({', '.join(_STEP_SKIP_NAMES)})."
        ),
    )
    args = ap.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    unknown = skip - set(_STEP_SKIP_NAMES)
    if unknown:
        print(f"[FAIL] unknown skip names: {sorted(unknown)}", file=sys.stderr)
        return 2

    repo = args.repo
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    steps_run: list[dict] = []

    def _step(name: str, label: str, cmd: list[str]) -> bool:
        """Run one step and record outcome. Returns False to abort the pipeline."""
        if name in skip:
            steps_run.append({"step": name, "status": "SKIPPED"})
            return True
        rc = _run_step(label, [py, *cmd])
        steps_run.append({"step": name, "status": "OK" if rc == 0 else "FAIL", "rc": rc})
        return rc == 0

    # Step 1: semantic index (foundation)
    idx_args = [str(HERE / "semantic_index_writer.py"), "--out", str(out_dir)]
    if args.full_index:
        idx_args.append("--full")
    if not _step("index", "semantic_index_writer", idx_args):
        _write_manifest(out_dir, steps_run)
        return 1

    # Step 2: stack profile
    if not _step("stack", "stack_profile_detector",
                 [str(HERE / "stack_profile_detector.py"),
                  "--repo", repo, "--out", str(out_dir)]):
        _write_manifest(out_dir, steps_run)
        return 1

    # Step 3: bytecode scan (only if --module supplied)
    if args.module:
        if not _step("bytecode", "bytecode_scanner",
                     [str(HERE / "bytecode_scanner.py"),
                      "--repo", repo, "--out", str(out_dir),
                      "--module", args.module, "--include", args.include_fqcn]):
            _write_manifest(out_dir, steps_run)
            return 1
    else:
        steps_run.append({"step": "bytecode", "status": "SKIPPED",
                          "reason": "no --module supplied"})

    # Step 4: source symbol enricher
    source_args = [str(HERE / "source_symbol_enricher.py"),
                   "--repo", repo, "--out", str(out_dir)]
    if args.module:
        source_args.extend(["--module", args.module])
    if not _step("source", "source_symbol_enricher", source_args):
        _write_manifest(out_dir, steps_run)
        return 1

    # Step 5: classification
    if not _step("classification", "classification_analyzer",
                 [str(HERE / "classification_analyzer.py"), "--out", str(out_dir)]):
        _write_manifest(out_dir, steps_run)
        return 1

    # Step 6: dependency graph
    if not _step("deps", "dependency_graph_extractor",
                 [str(HERE / "dependency_graph_extractor.py"), "--out", str(out_dir)]):
        _write_manifest(out_dir, steps_run)
        return 1

    # Step 7: validate state
    if not _step("validate", "state_validator",
                 [str(HERE / "state_validator.py"), "--state", str(out_dir)]):
        _write_manifest(out_dir, steps_run)
        return 1

    manifest_path = _write_manifest(out_dir, steps_run)
    print(f"[repo-intelligence] manifest: {manifest_path}", file=sys.stderr)
    print(json.dumps({"status": "OK", "manifest": str(manifest_path),
                      "steps": steps_run}, indent=2))
    return 0


if __name__ == "__main__":
    with _TimedRun("repo_intelligence") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
