from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from common import SCHEMAS_DIR, atomic_write_json, atomic_write_text, validate

from .models import Finding, RepoRef, SourceFile


def _validate_architecture_artifact(schema_name: str, data: dict) -> None:
    schema_path = SCHEMAS_DIR / "architecture" / f"{schema_name}.schema.json"
    if not schema_path.exists():
        return
    try:
        import jsonschema  # type: ignore
    except Exception:
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(data, schema)


def render_report(
    repo_ref: RepoRef,
    repo_uri: str,
    branch: str,
    files: list[SourceFile],
    arch: dict,
    dep: dict,
    findings: list[Finding],
) -> str:
    lines = [
        "# Architecture Review",
        "",
        f"- Repositorio: `{repo_uri}`",
        f"- Host: `{repo_ref.host}`",
        f"- API base: `{repo_ref.api_base}`",
        f"- Branch: `{branch}`",
        f"- Archivos analizados: `{len(files)}`",
        f"- Edges de imports: `{dep['edge_count']}`",
        "",
        "## Componentes detectados",
        "",
    ]

    for key, values in arch["components"].items():
        lines.append(f"- {key}: {len(values)}")

    lines.extend(["", "## Señales de framework", ""])
    for key, value in arch["framework_signals"].items():
        lines.append(f"- {key}: `{value}`")

    parser_counts = arch.get("analysis", {}).get("java_parser_counts", {})
    if parser_counts:
        lines.extend(["", "## Parser Java", ""])
        for key, value in parser_counts.items():
            lines.append(f"- {key}: `{value}`")

    lines.extend(["", "## Hallazgos", ""])
    grouped: dict[str, list[Finding]] = {}
    for item in findings:
        grouped.setdefault(item.severity, []).append(item)

    for severity in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
        items = grouped.get(severity, [])
        if not items:
            continue
        lines.extend([f"### {severity}", ""])
        for item in items:
            evidence = ", ".join(f"`{e}`" for e in item.evidence[:10]) or "`sin evidencia`"
            lines.extend([
                f"#### {item.title}",
                "",
                f"- ID: `{item.id}`",
                f"- Categoría: `{item.category}`",
                f"- Descripción: {item.description}",
                f"- Evidencia: {evidence}",
                f"- Fuente: `{item.source}`",
                f"- Confianza: `{item.confidence:.2f}`",
                f"- Recomendación: {item.recommendation}",
                "",
            ])

    lines.extend([
        "## Nota de alcance",
        "",
        "Este análisis es estático y remoto. No compila, no ejecuta tests, no levanta la aplicación y no valida runtime.",
        "Los reportes fueron generados fuera de la arquitectura del agente.",
        "",
    ])
    return "\n".join(lines)


def write_outputs(
    out_dir: Path,
    *,
    inventory: dict,
    architecture_map: dict,
    dependency_map: dict,
    findings: list[Finding],
    report: str,
) -> None:
    finding_dicts = [asdict(f) for f in findings]
    _validate_architecture_artifact("source-inventory", inventory)
    _validate_architecture_artifact("architecture-map", architecture_map)
    _validate_architecture_artifact("dependency-map", dependency_map)
    validate("architecture-findings", finding_dicts)
    atomic_write_json(out_dir / "source-inventory.json", inventory)
    atomic_write_json(out_dir / "architecture-map.json", architecture_map)
    atomic_write_json(out_dir / "dependency-map.json", dependency_map)
    atomic_write_json(out_dir / "architecture-findings.json", finding_dicts)
    atomic_write_text(out_dir / "architecture-report.md", report)
