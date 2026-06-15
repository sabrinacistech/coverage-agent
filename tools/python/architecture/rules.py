from __future__ import annotations

import hashlib
import re

from .models import Finding


def _finding_id(category: str, title: str, evidence: list[str]) -> str:
    raw = "|".join([category, title, *evidence])
    return "arch-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def finding(
    *,
    severity: str,
    category: str,
    title: str,
    description: str,
    evidence: list[str],
    recommendation: str,
    source: str = "architecture-static-rules",
    confidence: float = 0.75,
) -> Finding:
    return Finding(
        id=_finding_id(category, title, evidence),
        severity=severity,
        category=category,
        title=title,
        description=description,
        evidence=evidence,
        recommendation=recommendation,
        source=source,
        confidence=max(0.0, min(1.0, confidence)),
    )


def controller_repository_coupling(path: str, text: str) -> Finding | None:
    if ".repository." not in text and not re.search(r"\b[A-Za-z0-9_]+Repository\b", text):
        return None
    return finding(
        severity="HIGH",
        category="layering",
        title="Controller acoplado a Repository",
        description="Se detectó un controller que parece importar o usar repositories directamente.",
        evidence=[path],
        recommendation="Mover el acceso a persistencia detrás de una capa service/use-case.",
        confidence=0.85,
    )


def controller_repository_dependency(path: str, imports: list[str], type_refs: list[str]) -> Finding | None:
    has_repo_import = any(".repository." in imp or imp.endswith("Repository") for imp in imports)
    has_repo_type = any(ref.endswith("Repository") for ref in type_refs)
    if not has_repo_import and not has_repo_type:
        return None
    return finding(
        severity="HIGH",
        category="layering",
        title="Controller acoplado a Repository",
        description="El parser detecto que un controller depende de repositories directamente.",
        evidence=[path],
        recommendation="Mover el acceso a persistencia detras de una capa service/use-case.",
        source="architecture-parser-rules",
        confidence=0.90,
    )


def service_depends_on_web_layer(path: str, imports: list[str], annotations: list[str]) -> Finding | None:
    has_controller_import = any(".controller." in imp or imp.endswith("Controller") for imp in imports)
    has_web_annotation = any(a in {"RestController", "Controller", "RequestMapping"} for a in annotations)
    if not has_controller_import and not has_web_annotation:
        return None
    return finding(
        severity="MEDIUM",
        category="layering",
        title="Service acoplado a capa web",
        description="El parser detecto que una clase service referencia controllers o anotaciones web.",
        evidence=[path],
        recommendation="Mantener la capa service independiente de adapters HTTP/controllers.",
        source="architecture-parser-rules",
        confidence=0.80,
    )


def entity_exposed_as_controller(path: str, annotations: list[str]) -> Finding | None:
    if not any(a in {"RestController", "Controller", "RequestMapping"} for a in annotations):
        return None
    return finding(
        severity="HIGH",
        category="api-design",
        title="Entity mezclada con contrato web",
        description="Una clase clasificada como entity/model tiene anotaciones propias de la capa web.",
        evidence=[path],
        recommendation="Separar entidades de persistencia de controllers y contratos externos.",
        source="architecture-parser-rules",
        confidence=0.85,
    )


def system_out_usage(path: str, text: str) -> Finding | None:
    if "System.out.println" not in text:
        return None
    return finding(
        severity="LOW",
        category="observability",
        title="Uso de System.out.println",
        description="Se detectó salida directa por consola en código Java.",
        evidence=[path],
        recommendation="Usar logger estructurado o mecanismo de logging del framework.",
        confidence=0.95,
    )


def hardcoded_secret(path: str, text: str) -> Finding | None:
    if not re.search(r"(?i)(password|secret|token|apikey|api_key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}", text):
        return None
    return finding(
        severity="CRITICAL",
        category="security",
        title="Posible secreto hardcodeado",
        description="Se detectaron claves o credenciales potenciales en configuración.",
        evidence=[path],
        recommendation="Mover secretos a vault, variables de entorno o secret manager.",
        confidence=0.70,
    )


def controllers_without_services(controllers: list[str]) -> Finding:
    return finding(
        severity="MEDIUM",
        category="layering",
        title="Controllers sin capa service detectable",
        description="Hay controllers, pero no se detectó una capa service clara.",
        evidence=controllers[:10],
        recommendation="Introducir servicios o casos de uso para separar API de lógica de negocio.",
        confidence=0.65,
    )


def entities_without_dtos(entities: list[str]) -> Finding:
    return finding(
        severity="MEDIUM",
        category="api-design",
        title="Entidades sin DTOs detectables",
        description="Hay entidades/modelos, pero no se detectan DTOs.",
        evidence=entities[:10],
        recommendation="Separar entidades de persistencia de contratos externos de API.",
        confidence=0.60,
    )


def actuator_not_detectable(config_files: list[str]) -> Finding:
    return finding(
        severity="INFO",
        category="observability",
        title="Actuator no detectable",
        description="No se detectó configuración explícita de management endpoints.",
        evidence=config_files[:5],
        recommendation="Evaluar health checks, métricas y endpoints de observabilidad.",
        confidence=0.55,
    )
