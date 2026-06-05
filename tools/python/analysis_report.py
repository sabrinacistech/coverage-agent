# tools/python/analysis_report.py
"""analysis_report.py — consolidated, human-readable report of the deterministic analysis.

Reads the state directory produced by run_pipeline.py and emits ONE report that
gathers everything an operator wants to see at a glance:

  * project / build-tool / archetype / test stack
  * counts (contracts, packs, fixtures, ...)
  * COVERAGE TO REALIZE: the ranked batch plan (SUT + method + score) and the
    full coverage-target universe (per-SUT missed lines/branches)
  * class classification (type, testability risk, coverage value, reasons)
  * EXCLUDED AS GENERATED: detected generators, excluded FQCNs/packages, and how
    many coverage targets were dropped because they belong to generated code

Outputs (under <state-dir>/_summaries/):
  analysis-report.md    human-readable
  analysis-report.json  machine-readable (same data)

Pure read-only + deterministic: no LLM, no network.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _pkg_matchers(gen_index: dict):
    """Replicate the canonical generated-code matcher (excludedFqcns + excludedPackages)
    so the report can count which coverage targets are generated."""
    fqcns = set(gen_index.get("excludedFqcns", []) or [])
    pats = []
    for pkg in (gen_index.get("excludedPackages", []) or []):
        try:
            pats.append(re.compile(re.escape(pkg).replace(r"\*", ".*")))
        except re.error:
            pass
    return fqcns, pats


def _is_generated(fqcn: str, fqcns: set, pats: list) -> bool:
    if fqcn in fqcns:
        return True
    return any(p.match(fqcn or "") for p in pats)


def build_report(state_dir: Path) -> dict:
    handoff = _load(state_dir / "_summaries" / "handoff-summary.json", {})
    build_tool = _load(state_dir / "build-tool-contract.json", {})
    archetype = _load(state_dir / "archetype-profile.json", {})
    stack = _load(state_dir / "stack-profile.json", {})
    classification = _load(state_dir / "classification-index.json", {"classes": []})
    cov_targets = _load(state_dir / "coverage-targets.json", {"targets": []})
    batch = _load(state_dir / "batch-plan.json", {"items": []})
    gen_index = _load(state_dir / "generated-code-index.json", {})

    classes = classification.get("classes", []) or []
    targets = cov_targets.get("targets", []) or []
    items = batch.get("items", []) or []

    fqcns, pats = _pkg_matchers(gen_index)

    # Coverage-target universe split into real vs generated.
    real_targets = [t for t in targets if not _is_generated(t.get("sut", ""), fqcns, pats)]
    gen_targets = [t for t in targets if _is_generated(t.get("sut", ""), fqcns, pats)]

    def _sum(ts, key):
        return sum(int(t.get(key, 0) or 0) for t in ts)

    # Per-SUT rollup of the REAL coverage targets.
    per_sut: dict[str, dict] = {}
    for t in real_targets:
        s = per_sut.setdefault(
            t.get("sut", "?"), {"targets": 0, "missedLines": 0, "missedBranches": 0}
        )
        s["targets"] += 1
        s["missedLines"] += int(t.get("missedLines", 0) or 0)
        s["missedBranches"] += int(t.get("missedBranches", 0) or 0)

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stateDir": str(state_dir),
        "handoffStatus": handoff.get("status", "UNKNOWN"),
        "module": handoff.get("module") or build_tool.get("module") or batch.get("module"),
        "buildTool": handoff.get("buildTool") or {
            "type": build_tool.get("buildTool") or build_tool.get("type"),
        },
        "archetype": handoff.get("archetype") or {
            "parent": archetype.get("parent"),
            "namespace": archetype.get("namespace"),
        },
        "stack": handoff.get("stack") or {
            k: stack.get(k) for k in ("testFramework", "mockingLib", "assertionLib", "diFramework")
        },
        "coverageMode": batch.get("mode") or cov_targets.get("mode"),
        "counts": handoff.get("counts", {}),
        "batch": {
            "cycle": batch.get("cycle"),
            "size": batch.get("sizeChosen") or len(items),
            "reason": batch.get("reason"),
            "items": items,
        },
        "coverageUniverse": {
            "totalTargets": len(targets),
            "realTargets": len(real_targets),
            "generatedTargetsDropped": len(gen_targets),
            "realMissedLines": _sum(real_targets, "missedLines"),
            "realMissedBranches": _sum(real_targets, "missedBranches"),
            "perSut": per_sut,
        },
        "classification": classes,
        "classificationBreakdown": handoff.get("classification", {}),
        "excludedGenerated": {
            "generators": [g.get("kind") for g in gen_index.get("generators", []) or []],
            "excludedPackages": gen_index.get("excludedPackages", []) or [],
            "excludedFqcns": gen_index.get("excludedFqcns", []) or [],
            "blocked": gen_index.get("blocked", []) or [],
        },
    }


def _md_table(headers: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return out


def to_markdown(r: dict) -> str:
    L: list[str] = []
    status = r["handoffStatus"]
    badge = "✅" if status == "READY" else ("⛔" if status == "BLOCKED" else "❔")
    L.append(f"# Reporte de análisis de cobertura")
    L.append("")
    L.append(f"- **Estado del handoff:** {badge} {status}")
    L.append(f"- **Módulo:** `{r.get('module') or '(raíz)'}`")
    L.append(f"- **Modo de cobertura:** {r.get('coverageMode')}")
    L.append(f"- **Generado:** {r['generatedAt']}")
    L.append(f"- **State dir:** `{r['stateDir']}`")
    L.append("")

    # 1. Stack
    bt, ar, st = r["buildTool"], r["archetype"], r["stack"]
    L.append("## 1. Proyecto y stack")
    L.append(f"- **Build tool:** {bt.get('type')}"
             + (f" · Spring Boot {st.get('springBoot')}" if st.get("springBoot") else "")
             + (f" · Java {bt.get('javaVersion')}" if bt.get("javaVersion") else ""))
    L.append(f"- **Arquetipo:** {ar.get('parent')} · namespace `{ar.get('namespace')}`")
    L.append(f"- **Test stack:** {st.get('testFramework')} + {st.get('mockingLib')} "
             f"+ {st.get('assertionLib')} (DI: {st.get('diFramework')})")
    L.append("")

    # 2. Counts
    L.append("## 2. Resumen (counts)")
    counts = r.get("counts", {})
    if counts:
        L += _md_table(["Métrica", "Valor"], [[k, v] for k, v in counts.items()])
    else:
        L.append("_(sin counts en handoff-summary)_")
    L.append("")

    # 3. Coverage to realize
    b = r["batch"]
    cu = r["coverageUniverse"]
    L.append("## 3. Cobertura a realizar")
    L.append(f"- **Ciclo:** {b.get('cycle')} · **Tamaño del batch:** {b.get('size')}")
    if b.get("reason"):
        L.append(f"- **Criterio de ranking:** {b['reason']}")
    L.append("")
    L.append("### 3.1 Batch del ciclo (SUTs/métodos que se van a testear)")
    rows = [
        [i.get("sut", "").split(".")[-1], i.get("method"), i.get("score"),
         i.get("template") or "—",
         (",".join(i.get("fixtureIds", [])) or "—")]
        for i in b.get("items", [])
    ]
    L += _md_table(["SUT", "Método", "Score", "Template", "Fixtures"], rows) if rows \
        else ["_(batch vacío)_"]
    L.append("")
    L.append("### 3.2 Universo de objetivos (coverage-targets)")
    L.append(f"- **Targets totales (JaCoCo):** {cu['totalTargets']}")
    L.append(f"- **Targets reales (no generados):** {cu['realTargets']} "
             f"— líneas sin cubrir: {cu['realMissedLines']}, ramas sin cubrir: {cu['realMissedBranches']}")
    L.append(f"- **Targets descartados por generados:** {cu['generatedTargetsDropped']}")
    L.append("")
    if cu["perSut"]:
        rows = [[fq.split(".")[-1], v["targets"], v["missedLines"], v["missedBranches"]]
                for fq, v in sorted(cu["perSut"].items(), key=lambda kv: -kv[1]["missedLines"])]
        L.append("**Por clase real (ordenado por líneas sin cubrir):**")
        L += _md_table(["Clase", "#Targets", "Líneas sin cubrir", "Ramas sin cubrir"], rows)
        L.append("")

    # 4. Classification
    L.append("## 4. Clasificación de clases")
    rows = [
        [c.get("fqcn", "").split(".")[-1], c.get("type"), c.get("testabilityRisk"),
         c.get("coverageValue"), "; ".join(c.get("reasons", []) or [])[:80]]
        for c in r.get("classification", [])
    ]
    L += _md_table(["Clase", "Tipo", "Riesgo", "Valor", "Razón"], rows) if rows \
        else ["_(sin classification-index)_"]
    L.append("")

    # 5. Excluded generated
    eg = r["excludedGenerated"]
    L.append("## 5. Excluido por código autogenerado")
    gens = [g for g in eg["generators"] if g]
    L.append(f"- **Generadores detectados:** {', '.join(sorted(set(gens))) or '(ninguno)'}")
    L.append(f"- **Targets de cobertura descartados:** {cu['generatedTargetsDropped']}")
    if eg["excludedPackages"]:
        L.append(f"- **Paquetes excluidos ({len(eg['excludedPackages'])}):**")
        for p in eg["excludedPackages"]:
            L.append(f"    - `{p}`")
    L.append(f"- **FQCNs excluidos ({len(eg['excludedFqcns'])}):**")
    for f in eg["excludedFqcns"][:50]:
        L.append(f"    - `{f}`")
    if len(eg["excludedFqcns"]) > 50:
        L.append(f"    - … (+{len(eg['excludedFqcns']) - 50} más)")
    if eg["blocked"]:
        L.append(f"- **Blocked (contrato faltante):** {len(eg['blocked'])}")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Reporte consolidado del análisis determinista.")
    ap.add_argument("--state-dir", "--state", dest="state_dir", required=True,
                    help="Directorio de estado producido por run_pipeline.py")
    ap.add_argument("--stdout", action="store_true", help="Imprimir el Markdown a stdout también")
    args = ap.parse_args()

    state_dir = Path(args.state_dir).resolve()
    if not state_dir.exists():
        raise SystemExit(f"[FAIL] state dir no existe: {state_dir}")

    report = build_report(state_dir)
    md = to_markdown(report)

    out_dir = state_dir / "_summaries"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "analysis-report.md"
    json_path = out_dir / "analysis-report.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.stdout:
        print(md)
    print(f"\n[OK] reporte escrito en:\n  {md_path}\n  {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
