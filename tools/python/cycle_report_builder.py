"""cycle_report_builder.py — deterministic cycle report builder.

Calcula métricas exactas (summary, sutReports, gateStatus, recommendations) a
partir de los estados de ejecución del ciclo, sin invocar al LLM (reporting es
una fase determinista, no un turno LLM).

Reglas implementadas (idénticas a las del prompt original):

  sutReports[].status
    - PASS    → todos los test cases en PASS
    - PARTIAL → al menos un PASS y al menos un FAIL
    - FAIL    → al menos un FAIL, ningún PASS
    - SKIP    → todos en SKIP o BLOCKED

  gateStatus
    - G6_coverageImproved → totals.linesAfter > linesBefore OR branchesAfter > branchesBefore
    - G7_noRegressions    → ningún SUT con linesAfter < linesBefore
    - G8_compileClean     → totalCompileErrors == 0 en todos los SUTs

  recommendations[] (tabla de plantillas — máx 5)

CLI
---
  python tools/python/cycle_report_builder.py \\
      --sut-results state/sut-results.json \\
      --coverage-delta state/coverage-delta.json \\
      --cycle 2 \\
      --mode coverage \\
      --out state/_summaries/cycle-2-report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, atomic_write_json, load_json  # noqa: E402

SCHEMA_VERSION = 1
MAX_RECOMMENDATIONS = 5


# ── Status logic ──────────────────────────────────────────────────────────────

def sut_status(test_cases: list[dict]) -> str:
    """Map a SUT's test cases to its aggregate status."""
    statuses = [tc.get("status", "BLOCKED") for tc in test_cases]
    pass_count = statuses.count("PASS")
    fail_count = statuses.count("FAIL")
    skip_or_blocked = sum(1 for s in statuses if s in ("SKIP", "BLOCKED"))

    if pass_count == len(statuses) and pass_count > 0:
        return "PASS"
    if pass_count > 0 and fail_count > 0:
        return "PARTIAL"
    if fail_count > 0 and pass_count == 0:
        return "FAIL"
    if skip_or_blocked == len(statuses):
        return "SKIP"
    # mixed PASS + SKIP/BLOCKED with no FAIL → PARTIAL (treated as not fully PASS)
    return "PARTIAL"


# ── Coverage lookup ───────────────────────────────────────────────────────────

def coverage_for(sut_fqcn: str, per_class: list[dict]) -> dict:
    """Return the {linesBefore, linesAfter, branchesBefore, branchesAfter} for a SUT."""
    for entry in per_class:
        if entry.get("sut") == sut_fqcn:
            return entry
    return {
        "linesBefore": None, "linesAfter": None,
        "branchesBefore": None, "branchesAfter": None,
    }


def _delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return round(after - before, 4)


# ── SUT report ────────────────────────────────────────────────────────────────

def build_sut_report(sut_result: dict, per_class: list[dict]) -> dict:
    sut_fqcn = sut_result.get("sutFqcn") or sut_result.get("sut") or ""
    test_cases = sut_result.get("testCases") or []

    total_compile = sum(int(tc.get("compileErrors", 0) or 0) for tc in test_cases)
    total_runtime = sum(int(tc.get("runtimeErrors", 0) or 0) for tc in test_cases)
    total_repair = sum(int(tc.get("repairAttempts", 0) or 0) for tc in test_cases)

    statuses = [tc.get("status", "BLOCKED") for tc in test_cases]
    cov = coverage_for(sut_fqcn, per_class)

    return {
        "sutFqcn": sut_fqcn,
        "status": sut_status(test_cases),
        "testCasesGenerated": len(test_cases),
        "testCasesPassed": statuses.count("PASS"),
        "testCasesFailed": statuses.count("FAIL"),
        "testCasesSkipped": statuses.count("SKIP") + statuses.count("BLOCKED"),
        "totalCompileErrors": total_compile,
        "totalRuntimeErrors": total_runtime,
        "totalRepairAttempts": total_repair,
        "linesBefore":   cov.get("linesBefore"),
        "linesAfter":    cov.get("linesAfter"),
        "branchesBefore": cov.get("branchesBefore"),
        "branchesAfter":  cov.get("branchesAfter"),
        "deltaLines":    _delta(cov.get("linesAfter"), cov.get("linesBefore")),
        "deltaBranches": _delta(cov.get("branchesAfter"), cov.get("branchesBefore")),
    }


# ── Recommendations ───────────────────────────────────────────────────────────

def build_recommendations(
    sut_reports: list[dict],
    coverage_delta: dict,
    context_packs: dict[str, dict],
) -> list[str]:
    """Apply the recommendation template table; max 5 entries."""
    out: list[str] = []
    totals = coverage_delta.get("totals") or {}
    delta_lines_total = _delta(totals.get("linesAfter"), totals.get("linesBefore"))
    delta_branches_total = _delta(totals.get("branchesAfter"), totals.get("branchesBefore"))

    # Regression check (priority 1 — always surface)
    for r in sut_reports:
        if (r["linesBefore"] is not None and r["linesAfter"] is not None
                and r["linesAfter"] < r["linesBefore"]):
            out.append(
                f"REGRESION detectada en {r['sutFqcn']}: cobertura de lineas "
                f"bajo de {r['linesBefore']}% a {r['linesAfter']}%"
            )

    # SUT sin fixtures (check context-packs if available)
    for r in sut_reports:
        pack = context_packs.get(r["sutFqcn"]) or {}
        fixtures = pack.get("fixtures") or pack.get("fix") or []
        if isinstance(fixtures, list) and len(fixtures) == 0:
            out.append(
                f"{r['sutFqcn']}: 0 fixtures disponibles — "
                "ejecutar fixture_catalog_builder antes del proximo ciclo"
            )

    # ≥ 3 repair attempts fallidos
    for r in sut_reports:
        if r["totalRepairAttempts"] >= 3 and r["testCasesFailed"] > 0:
            out.append(
                f"{r['sutFqcn']}: supera umbral de reparacion "
                f"({r['totalRepairAttempts']} intentos) — revisar "
                f"symbol-contracts/{r['sutFqcn']}.json manualmente"
            )

    # Cobertura sin mejora
    if (delta_lines_total is not None and delta_branches_total is not None
            and delta_lines_total == 0 and delta_branches_total == 0):
        out.append(
            "Sin mejora de cobertura en este ciclo — "
            "considerar aumentar batch size o revisar targets"
        )

    # Todos los SUTs en SKIP
    if sut_reports and all(r["status"] == "SKIP" for r in sut_reports):
        out.append(
            "Todos los SUTs bloqueados — verificar que batch-plan.json "
            "tenga targets con hasContract=true"
        )

    return out[:MAX_RECOMMENDATIONS]


# ── Main builder ──────────────────────────────────────────────────────────────

def build_report(
    cycle: int,
    mode: str,
    sut_results: list[dict],
    coverage_delta: dict,
    context_packs: dict[str, dict] | None = None,
) -> dict:
    per_class = coverage_delta.get("perClass") or []
    totals = coverage_delta.get("totals") or {}

    sut_reports = [build_sut_report(sr, per_class) for sr in sut_results]

    # Summary aggregations
    total_tc = sum(r["testCasesGenerated"] for r in sut_reports)
    total_passed_tc = sum(r["testCasesPassed"] for r in sut_reports)
    total_failed_tc = sum(r["testCasesFailed"] for r in sut_reports)
    total_skipped_tc = sum(r["testCasesSkipped"] for r in sut_reports)
    total_compile = sum(r["totalCompileErrors"] for r in sut_reports)

    by_status = {s: 0 for s in ("PASS", "PARTIAL", "FAIL", "SKIP")}
    for r in sut_reports:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    delta_lines_total = _delta(totals.get("linesAfter"), totals.get("linesBefore"))
    delta_branches_total = _delta(totals.get("branchesAfter"), totals.get("branchesBefore"))

    # Gates
    g6 = bool(
        (delta_lines_total is not None and delta_lines_total > 0)
        or (delta_branches_total is not None and delta_branches_total > 0)
    )
    g7 = not any(
        r["linesBefore"] is not None and r["linesAfter"] is not None
        and r["linesAfter"] < r["linesBefore"]
        for r in sut_reports
    )
    g8 = total_compile == 0

    recommendations = build_recommendations(
        sut_reports, coverage_delta, context_packs or {}
    )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "cycle": cycle,
        "mode": mode,
        "summary": {
            "totalSuts": len(sut_reports),
            "passed": by_status["PASS"],
            "partiallyPassed": by_status["PARTIAL"],
            "failed": by_status["FAIL"],
            "skipped": by_status["SKIP"],
            "totalTestCasesGenerated": total_tc,
            "totalTestCasesPassed": total_passed_tc,
            "totalTestCasesFailed": total_failed_tc,
            "totalTestCasesSkipped": total_skipped_tc,
            "coverageDeltaLines": delta_lines_total,
            "coverageDeltaBranches": delta_branches_total,
            "coverageAfterLines": totals.get("linesAfter"),
            "coverageAfterBranches": totals.get("branchesAfter"),
        },
        "sutReports": sut_reports,
        "recommendations": recommendations,
        "gateStatus": {
            "G6_coverageImproved": g6,
            "G7_noRegressions": g7,
            "G8_compileClean": g8,
        },
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _load_context_packs(packs_dir: Path | None) -> dict[str, dict]:
    if not packs_dir or not packs_dir.exists():
        return {}
    out: dict[str, dict] = {}
    for p in packs_dir.glob("*.json"):
        try:
            data = load_json(p)
        except Exception:
            continue
        sut_raw = data.get("sut") or data.get("fqcn") or ""
        fqcn = sut_raw.get("fqcn") if isinstance(sut_raw, dict) else sut_raw
        if fqcn:
            out[fqcn] = data
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Deterministic cycle report builder (reporting is a deterministic phase, "
            "not an LLM turn). Reads sut-results + coverage-delta and emits the cycle report JSON."
        )
    )
    ap.add_argument("--sut-results", required=True, metavar="PATH",
                    help="JSON file: { sutResults: [{sutFqcn, testCases[]}] } or a bare array.")
    ap.add_argument("--coverage-delta", required=True, metavar="PATH",
                    help="state/coverage-delta.json (produced by jacoco_parser.py).")
    ap.add_argument("--cycle", required=True, type=int)
    ap.add_argument("--mode", required=True,
                    choices=("coverage", "branch-coverage", "mutation-hardening"))
    ap.add_argument("--context-packs", default=None, metavar="DIR",
                    help="state/context-packs/ to detect SUTs without fixtures (optional).")
    ap.add_argument("--out", required=True, metavar="PATH",
                    help="Where to write the report JSON (atomic).")
    args = ap.parse_args()

    try:
        sr_raw = load_json(Path(args.sut_results))
        sut_results = sr_raw.get("sutResults") if isinstance(sr_raw, dict) else sr_raw
        if not isinstance(sut_results, list):
            print("[FAIL] --sut-results must contain a list under 'sutResults' or be a list",
                  file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"[FAIL] cannot load --sut-results: {exc}", file=sys.stderr)
        return 2

    try:
        coverage_delta = load_json(Path(args.coverage_delta))
    except Exception as exc:
        print(f"[FAIL] cannot load --coverage-delta: {exc}", file=sys.stderr)
        return 2

    context_packs = _load_context_packs(Path(args.context_packs)) if args.context_packs else {}

    report = build_report(args.cycle, args.mode, sut_results, coverage_delta, context_packs)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_path, report)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    with _TimedRun("cycle_report_builder") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
