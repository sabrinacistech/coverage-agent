"""bootstrap.py — auto-detect pipeline parameters and invoke run_pipeline.py.

Designed as the Phase 0 entry point described in `BOOT.md`. Reads the root
`pom.xml` (and submodules via `pom_parser.find_pom_modules`) to infer:

- `--module`         : first non-aggregator module name (`packaging != pom`).
- `--include-fqcn`   : regex derived from root `<groupId>` (e.g. `com.acme` -> `^com\\.acme\\.`).
- `--jacoco-xml`     : path to `target/site/jacoco/jacoco.xml` if it exists.

Then it invokes `run_pipeline.py` with the inferred arguments and emits a
single JSON block on stdout with the final values used. With `--dry-run`,
prints the command without executing.

Any explicit CLI flag (`--module`, `--include-fqcn`, `--jacoco-xml`,
`--coverage-mode`) overrides the inferred value.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from lxml import etree

from common import find_pom_modules
from pom_parser import parse_pom

HERE = Path(__file__).resolve().parent
NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def _read_group_id(pom_path: Path) -> str | None:
    """Return effective <groupId> of a POM: explicit, else parent's."""
    try:
        tree = etree.parse(str(pom_path))
    except etree.XMLSyntaxError:
        return None
    root = tree.getroot()
    gid = root.xpath("m:groupId/text()", namespaces=NS)
    if gid:
        return str(gid[0]).strip() or None
    parent = root.xpath("m:parent/m:groupId/text()", namespaces=NS)
    if parent:
        return str(parent[0]).strip() or None
    return None


def _infer_include_fqcn(group_id: str | None) -> str:
    """Derive a regex anchored on the root groupId. Falls back to `.*`."""
    if not group_id:
        return ".*"
    escaped = group_id.replace(".", r"\.")
    return f"^{escaped}\\."


def _pick_module(repo: Path, override: str | None) -> str | None:
    """Pick a module name. If override is provided, use it verbatim."""
    if override:
        return override
    modules = []
    for mod_dir in find_pom_modules(repo):
        pom = mod_dir / "pom.xml"
        if not pom.exists():
            continue
        try:
            modules.append(parse_pom(pom))
        except etree.XMLSyntaxError:
            continue
    if not modules:
        return None
    non_pom = [m for m in modules if m.get("packaging") != "pom"]
    return non_pom[0]["name"] if non_pom else None


def _detect_jacoco(repo: Path, override: str | None) -> str | None:
    if override:
        return override
    candidate = repo / "target" / "site" / "jacoco" / "jacoco.xml"
    return str(candidate) if candidate.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Auto-detect Phase 0 parameters and invoke run_pipeline.py.\n"
            "Emits a single JSON block on stdout describing the parameters used."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", required=True, help="Root of the Java repository.")
    ap.add_argument(
        "--out",
        default=None,
        help=(
            "State directory for generated artifacts. Defaults to "
            "<architecture-repo>/../.agent-state — a sibling folder that "
            "keeps generated state out of the architecture repo."
        ),
    )
    ap.add_argument("--module", default=None, help="Override inferred module name.")
    ap.add_argument(
        "--include-fqcn",
        default=None,
        help="Override inferred FQCN regex.",
    )
    ap.add_argument(
        "--jacoco-xml",
        default=None,
        help="Override JaCoCo XML path.",
    )
    ap.add_argument(
        "--coverage-mode",
        default="coverage",
        choices=["coverage", "branch-coverage", "mutation-hardening"],
        help="Coverage scoring mode (default: coverage).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the run_pipeline.py command and resolved parameters without executing it.",
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / "pom.xml").exists():
        print(f"[FAIL] No pom.xml found at {repo}", file=sys.stderr)
        return 2

    if args.out is None:
        # Default: sibling of the architecture repo (one level up from tools/python/../..).
        arch_root = Path(__file__).resolve().parents[2]
        state_path = (arch_root.parent / ".agent-state").resolve()
    else:
        state_path = Path(args.out).resolve()

    group_id = _read_group_id(repo / "pom.xml")
    include_fqcn = args.include_fqcn or _infer_include_fqcn(group_id)
    module = _pick_module(repo, args.module)
    jacoco_xml = _detect_jacoco(repo, args.jacoco_xml)

    cmd: list[str] = [
        sys.executable,
        str(HERE / "run_pipeline.py"),
        "--repo", str(repo),
        "--out", str(state_path),
        "--include-fqcn", include_fqcn,
        "--coverage-mode", args.coverage_mode,
    ]
    if module:
        cmd += ["--module", module]
    if jacoco_xml:
        cmd += ["--jacoco-xml", jacoco_xml]

    payload = {
        "module": module,
        "includeFqcn": include_fqcn,
        "jacocoXml": jacoco_xml,
        "statePath": str(state_path),
        "groupId": group_id,
        "coverageMode": args.coverage_mode,
        "command": cmd,
        "dryRun": bool(args.dry_run),
    }

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    rc = subprocess.call(cmd)
    print(json.dumps(payload, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
