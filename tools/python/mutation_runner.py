"""mutation_runner.py — deterministic PIT mutation runner.

Solo se invoca cuando el ciclo arranca en modo `mutation-hardening` (fase
determinista, no un turno LLM). Cero síntesis: verificación de plugin, ejecución
del goal de Maven, parseo de XML y agregación por método.

Procedimiento (idéntico al prompt original):

  1. Verificar plugin: si `org.pitest:pitest-maven` no está en pom.xml,
     emitir `status: BLOCKED_NO_PIT` y abortar (rc=2).
  2. Ejecutar PIT narrow:
       mvn -pl <module> -DtargetClasses=<glob> -DtargetTests=<glob>
         org.pitest:pitest-maven:mutationCoverage
  3. Parsear el `mutations.xml` más reciente de `target/pit-reports/`.
  4. Por cada `<mutation status="SURVIVED">`, registrar
     {class, method, line, mutator, description, suggestedAssertion}.
  5. Agrupar por método y priorizar los métodos con más sobrevivientes.

Salida: state/mutation-intelligence.json (schema v1).

CLI
---
  python tools/python/mutation_runner.py \\
      --pom path/to/pom.xml \\
      --module service-foo \\
      --target-classes 'com.acme.*' \\
      --target-tests 'com.acme.*Test' \\
      --out state/mutation-intelligence.json \\
      [--budget-seconds 1800] \\
      [--mvn mvn] \\
      [--skip-mvn]    # only parse existing reports (debug / CI replay)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, atomic_write_json  # noqa: E402

SCHEMA_VERSION = 1

# ── PIT plugin detection ──────────────────────────────────────────────────────

_PIT_RE = re.compile(
    r"<groupId>\s*org\.pitest\s*</groupId>\s*<artifactId>\s*pitest-maven\s*</artifactId>",
    re.IGNORECASE | re.DOTALL,
)


def has_pit_plugin(pom_path: Path) -> bool:
    if not pom_path.exists():
        return False
    text = pom_path.read_text(encoding="utf-8", errors="ignore")
    return bool(_PIT_RE.search(text))


# ── Maven invocation ──────────────────────────────────────────────────────────

def run_pit(
    mvn: str,
    pom_path: Path,
    module: str | None,
    target_classes: str,
    target_tests: str,
    budget_seconds: int,
) -> tuple[int, str]:
    """Run the PIT goal. Returns (returncode, combined stdout+stderr)."""
    cmd = [mvn, "-f", str(pom_path)]
    if module:
        cmd.extend(["-pl", module])
    cmd.extend([
        f"-DtargetClasses={target_classes}",
        f"-DtargetTests={target_tests}",
        "org.pitest:pitest-maven:mutationCoverage",
    ])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=budget_seconds, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {budget_seconds}s"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ── Report discovery ──────────────────────────────────────────────────────────

def find_latest_report(module_dir: Path) -> Path | None:
    """Locate the newest target/pit-reports/<timestamp>/mutations.xml."""
    reports_root = module_dir / "target" / "pit-reports"
    if not reports_root.exists():
        return None
    candidates = sorted(
        (p for p in reports_root.glob("*/mutations.xml") if p.is_file()),
        key=lambda p: p.parent.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


# ── XML parsing ───────────────────────────────────────────────────────────────

# Hints per PIT mutator (deterministic — no LLM).
_MUTATOR_HINTS: dict[str, str] = {
    "ConditionalsBoundaryMutator": "boundary-condition",
    "NegateConditionalsMutator": "negate-condition",
    "MathMutator": "arithmetic-operator",
    "IncrementsMutator": "increment-counter",
    "InvertNegsMutator": "sign-flip",
    "ReturnValsMutator": "return-value",
    "VoidMethodCallMutator": "void-call-elision",
    "NonVoidMethodCallMutator": "non-void-call-elision",
    "ConstructorCallMutator": "constructor-elision",
    "EmptyObjectReturnValsMutator": "empty-return",
    "FalseReturnsMutator": "false-return",
    "TrueReturnsMutator": "true-return",
    "NullReturnsMutator": "null-return",
    "PrimitiveReturnsMutator": "primitive-return",
}


def _hint_for(mutator_fqn: str, line: int) -> str:
    simple = mutator_fqn.rsplit(".", 1)[-1]
    base = _MUTATOR_HINTS.get(simple, "behaviour-change")
    return f"{base}-around-line-{line}"


def parse_mutations_xml(xml_path: Path) -> list[dict]:
    """Return list of SURVIVED mutations as dicts."""
    survivors: list[dict] = []
    tree = ET.parse(xml_path)
    for m in tree.getroot().findall("mutation"):
        if (m.get("status") or "").upper() != "SURVIVED":
            continue
        cls = (m.findtext("mutatedClass") or "").strip()
        meth = (m.findtext("mutatedMethod") or "").strip()
        desc_meth = (m.findtext("methodDescription") or "").strip()
        method_sig = f"{meth}{desc_meth}" if desc_meth else meth
        line_text = (m.findtext("lineNumber") or "0").strip()
        try:
            line = int(line_text)
        except ValueError:
            line = 0
        mutator = (m.findtext("mutator") or "").strip()
        desc = (m.findtext("description") or "").strip()
        survivors.append({
            "class": cls,
            "method": method_sig,
            "line": line,
            "mutator": mutator,
            "description": desc,
            "suggestedAssertion": _hint_for(mutator, line),
        })
    return survivors


def prioritize(survivors: list[dict]) -> list[dict]:
    """Sort survivors so methods with more mutants come first.

    Deterministic ordering inside each group: (class, method, line, mutator).
    """
    counts: dict[tuple[str, str], int] = {}
    for s in survivors:
        key = (s["class"], s["method"])
        counts[key] = counts.get(key, 0) + 1
    return sorted(
        survivors,
        key=lambda s: (
            -counts[(s["class"], s["method"])],
            s["class"], s["method"], s["line"], s["mutator"],
        ),
    )


# ── Main builder ──────────────────────────────────────────────────────────────

def build_intelligence(
    module: str,
    module_dir: Path,
    run_id: str,
) -> dict:
    xml_path = find_latest_report(module_dir)
    if xml_path is None:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "runId": run_id,
            "module": module,
            "survivors": [],
            "blocked": [{
                "reason": "NO_PIT_REPORT",
                "detail": f"No mutations.xml under {module_dir}/target/pit-reports/",
            }],
        }
    survivors = prioritize(parse_mutations_xml(xml_path))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "module": module,
        "survivors": survivors,
        "blocked": [],
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Deterministic PIT mutation runner (deterministic phase, not an LLM turn). "
            "Verifies pitest-maven, runs PIT narrow, parses mutations.xml and "
            "emits state/mutation-intelligence.json with SURVIVED mutants "
            "grouped by method."
        )
    )
    ap.add_argument("--pom", required=True, metavar="PATH", help="Root pom.xml.")
    ap.add_argument("--module", default=None,
                    help="Module name (-pl). Omit for single-module projects.")
    ap.add_argument("--target-classes", required=True,
                    help="PIT -DtargetClasses glob (e.g. 'com.acme.*').")
    ap.add_argument("--target-tests", required=True,
                    help="PIT -DtargetTests glob (e.g. 'com.acme.*Test').")
    ap.add_argument("--out", required=True, metavar="PATH",
                    help="Where to write state/mutation-intelligence.json.")
    ap.add_argument("--budget-seconds", type=int, default=1800,
                    help="Wall-clock budget for the mvn invocation. Default: 1800.")
    ap.add_argument("--mvn", default="mvn", help="Maven executable. Default: 'mvn'.")
    ap.add_argument("--skip-mvn", action="store_true",
                    help="Skip mvn invocation; parse the existing latest report.")
    args = ap.parse_args()

    pom_path = Path(args.pom).resolve()
    module_dir = pom_path.parent / args.module if args.module else pom_path.parent
    run_id = f"pit-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}"

    if not has_pit_plugin(pom_path):
        result = {
            "schemaVersion": SCHEMA_VERSION,
            "runId": run_id,
            "module": args.module or "<root>",
            "survivors": [],
            "blocked": [{
                "reason": "BLOCKED_NO_PIT",
                "detail": (
                    f"org.pitest:pitest-maven not declared in {pom_path}. "
                    "Aborting per the no-auto-add policy (PIT plugin must be present)."
                ),
            }],
        }
        atomic_write_json(Path(args.out), result)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2

    if not args.skip_mvn:
        rc, log_tail = run_pit(
            args.mvn, pom_path, args.module,
            args.target_classes, args.target_tests, args.budget_seconds,
        )
        if rc != 0:
            result = {
                "schemaVersion": SCHEMA_VERSION,
                "runId": run_id,
                "module": args.module or "<root>",
                "survivors": [],
                "blocked": [{
                    "reason": "PIT_RUN_FAILED" if rc != 124 else "PIT_BUDGET_EXCEEDED",
                    "exitCode": rc,
                    "tail": "\n".join(log_tail.strip().splitlines()[-20:]),
                }],
            }
            atomic_write_json(Path(args.out), result)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 1

    intel = build_intelligence(args.module or "<root>", module_dir, run_id)
    atomic_write_json(Path(args.out), intel)
    print(json.dumps(intel, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    with _TimedRun("mutation_runner") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
