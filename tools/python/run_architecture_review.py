#!/usr/bin/env python3
"""
run_architecture_review.py

Piloto v3:
- Soporta github.com y GitHub Enterprise.
- No hace git clone.
- Descarga árbol y contenido vía GitHub REST API.
- Escribe reportes/análisis fuera de agents/, skills/, tools/ y schemas/.

Ejemplo:
  python tools/python/run_architecture_review.py \
    --repo-uri https://github.p1.com.ar/myop-otorgamiento/margenes-comportamental-backend.git \
    --branch main \
    --out ./state/architecture_app
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


PROTECTED_AGENT_DIRS = {"agents", "skills", "tools", "schemas"}


@dataclass
class RepoRef:
    host: str
    owner: str
    repo: str
    api_base: str


@dataclass
class SourceFile:
    path: str
    kind: str
    size: int


@dataclass
class Finding:
    severity: str
    category: str
    title: str
    description: str
    evidence: list[str]
    recommendation: str


def parse_repo_uri(repo_uri: str, github_api_base: str | None = None) -> RepoRef:
    parsed = urllib.parse.urlparse(repo_uri)

    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            "URI inválida. Formato esperado: https://<github-host>/<owner>/<repo>[.git]"
        )

    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.strip("/").split("/") if p]

    if len(parts) < 2:
        raise ValueError(
            "URI inválida. Formato esperado: https://<github-host>/<owner>/<repo>[.git]"
        )

    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    if github_api_base:
        api_base = github_api_base.rstrip("/")
    elif host == "github.com":
        api_base = "https://api.github.com"
    else:
        # GitHub Enterprise REST API default.
        api_base = f"https://{host}/api/v3"

    return RepoRef(host=host, owner=owner, repo=repo, api_base=api_base)


def http_json(url: str, token: str | None = None) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "coverage-agent-architecture-pilot")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} consultando {url}\n{body}") from exc
    except ssl.SSLError as exc:
        raise RuntimeError(
            "Error SSL al conectar con GitHub Enterprise. "
            "Configurar certificados corporativos/CA bundle para Python. "
            "No se recomienda desactivar verificación SSL."
        ) from exc


def list_remote_files(repo_ref: RepoRef, branch: str, token: str | None) -> list[SourceFile]:
    encoded_branch = urllib.parse.quote(branch, safe="")
    url = (
        f"{repo_ref.api_base}/repos/"
        f"{urllib.parse.quote(repo_ref.owner, safe='')}/"
        f"{urllib.parse.quote(repo_ref.repo, safe='')}/"
        f"git/trees/{encoded_branch}?recursive=1"
    )
    data = http_json(url, token)
    tree = data.get("tree", [])

    if data.get("truncated"):
        print(
            "WARN: GitHub devolvió tree truncado. "
            "El análisis puede estar incompleto. Considerar filtros o ZIP adapter.",
            file=sys.stderr,
        )

    files: list[SourceFile] = []
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        size = int(item.get("size") or 0)
        if is_relevant(path):
            files.append(SourceFile(path=path, kind=classify_path(path), size=size))
    return files


def get_file_content(repo_ref: RepoRef, branch: str, path: str, token: str | None) -> str:
    encoded_path = urllib.parse.quote(path)
    encoded_ref = urllib.parse.quote(branch, safe="")
    url = (
        f"{repo_ref.api_base}/repos/"
        f"{urllib.parse.quote(repo_ref.owner, safe='')}/"
        f"{urllib.parse.quote(repo_ref.repo, safe='')}/"
        f"contents/{encoded_path}?ref={encoded_ref}"
    )
    data = http_json(url, token)

    if isinstance(data, list):
        return ""

    encoding = data.get("encoding")
    content = data.get("content", "")

    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8", errors="replace")

    download_url = data.get("download_url")
    if download_url:
        req = urllib.request.Request(download_url)
        req.add_header("User-Agent", "coverage-agent-architecture-pilot")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace")

    return ""


def classify_path(path: str) -> str:
    p = path.lower()
    if p.endswith(".java"):
        if "/controller/" in p or p.endswith("controller.java"):
            return "java-controller"
        if "/service/" in p or p.endswith("service.java") or "serviceimpl" in p:
            return "java-service"
        if "/repository/" in p or p.endswith("repository.java"):
            return "java-repository"
        if "/entity/" in p or "/model/" in p:
            return "java-entity"
        if "/dto/" in p or p.endswith("dto.java"):
            return "java-dto"
        if "/config/" in p:
            return "java-config"
        return "java"
    if p.endswith("pom.xml"):
        return "maven"
    if p.endswith("build.gradle") or p.endswith("build.gradle.kts"):
        return "gradle"
    if p.endswith(".yml") or p.endswith(".yaml") or p.endswith(".properties"):
        return "config"
    if p.endswith("dockerfile") or p == "dockerfile" or p.endswith(".dockerfile"):
        return "docker"
    if ".github/workflows/" in p:
        return "ci"
    if p.endswith(".md"):
        return "docs"
    return "other"


def is_relevant(path: str) -> bool:
    p = path.lower()
    if p.startswith(("target/", "build/", ".git/")):
        return False
    if "node_modules/" in p or "/dist/" in p or "/target/" in p or "/build/" in p:
        return False

    suffixes = (
        ".java", ".kt", ".xml", ".gradle", ".kts", ".yml", ".yaml",
        ".properties", ".md", "dockerfile", ".dockerfile",
    )
    return p.endswith(suffixes) or ".github/workflows/" in p or p.endswith("jenkinsfile")


def safe_output_dir(out: str | None, repo_name: str) -> Path:
    project_root = Path(__file__).resolve().parents[2].resolve()

    if out:
        output = Path(out).expanduser().resolve()
    else:
        output = (project_root.parent / "architecture-reviews" / repo_name).resolve()

    for dirname in PROTECTED_AGENT_DIRS:
        protected = (project_root / dirname).resolve()
        try:
            output.relative_to(protected)
            raise ValueError(
                f"La salida no puede escribirse dentro de la arquitectura del agente: {protected}"
            )
        except ValueError as exc:
            if "arquitectura del agente" in str(exc):
                raise

    output.mkdir(parents=True, exist_ok=True)
    return output


def package_name(java_text: str) -> str | None:
    match = re.search(r"^\s*package\s+([\w.]+)\s*;", java_text, flags=re.MULTILINE)
    return match.group(1) if match else None


def imports(java_text: str) -> list[str]:
    return re.findall(r"^\s*import\s+([\w.]+)\s*;", java_text, flags=re.MULTILINE)


def annotations(java_text: str) -> list[str]:
    return sorted(set(re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)", java_text)))


def build_maps(files: list[SourceFile], contents: dict[str, str]) -> tuple[dict, dict, list[Finding]]:
    java_files = [f for f in files if f.path.endswith(".java")]

    packages: dict[str, list[str]] = {}
    components: dict[str, list[str]] = {
        "controllers": [],
        "services": [],
        "repositories": [],
        "entities": [],
        "dtos": [],
        "configs": [],
        "other_java": [],
    }
    edges: list[dict] = []
    findings: list[Finding] = []

    for f in java_files:
        text = contents.get(f.path, "")
        pkg = package_name(text) or "(default)"
        packages.setdefault(pkg, []).append(f.path)

        anns = annotations(text)
        lower = f.path.lower()

        is_controller = "RestController" in anns or "Controller" in anns or "/controller/" in lower
        is_service = "Service" in anns or "/service/" in lower
        is_repository = "Repository" in anns or "/repository/" in lower
        is_entity = "Entity" in anns or "/entity/" in lower or "/model/" in lower
        is_dto = "/dto/" in lower or lower.endswith("dto.java")
        is_config = "Configuration" in anns or "/config/" in lower

        if is_controller:
            components["controllers"].append(f.path)
            if ".repository." in text or re.search(r"\b[A-Za-z0-9_]+Repository\b", text):
                findings.append(Finding(
                    severity="HIGH",
                    category="layering",
                    title="Controller acoplado a Repository",
                    description="Se detectó un controller que parece importar o usar repositories directamente.",
                    evidence=[f.path],
                    recommendation="Mover el acceso a persistencia detrás de una capa service/use-case.",
                ))
        elif is_service:
            components["services"].append(f.path)
        elif is_repository:
            components["repositories"].append(f.path)
        elif is_entity:
            components["entities"].append(f.path)
        elif is_dto:
            components["dtos"].append(f.path)
        elif is_config:
            components["configs"].append(f.path)
        else:
            components["other_java"].append(f.path)

        for imp in imports(text):
            edges.append({"source": f.path, "target_import": imp})

        if "System.out.println" in text:
            findings.append(Finding(
                severity="LOW",
                category="observability",
                title="Uso de System.out.println",
                description="Se detectó salida directa por consola en código Java.",
                evidence=[f.path],
                recommendation="Usar logger estructurado o mecanismo de logging del framework.",
            ))

    config_files = [f.path for f in files if f.kind == "config"]
    for path in config_files:
        text = contents.get(path, "")
        if re.search(r"(?i)(password|secret|token|apikey|api_key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}", text):
            findings.append(Finding(
                severity="CRITICAL",
                category="security",
                title="Posible secreto hardcodeado",
                description="Se detectaron claves o credenciales potenciales en configuración.",
                evidence=[path],
                recommendation="Mover secretos a vault, variables de entorno o secret manager.",
            ))

    framework_signals = {
        "spring_boot": any("SpringApplication" in contents.get(f.path, "") for f in java_files),
        "spring_web": any("RestController" in contents.get(f.path, "") for f in java_files),
        "spring_data_jpa": any("JpaRepository" in contents.get(f.path, "") for f in java_files),
        "spring_security": any("SecurityFilterChain" in contents.get(f.path, "") or "EnableWebSecurity" in contents.get(f.path, "") for f in java_files),
        "actuator_configured": any("management.endpoints" in contents.get(p, "") for p in config_files),
    }

    if components["controllers"] and not components["services"]:
        findings.append(Finding(
            severity="MEDIUM",
            category="layering",
            title="Controllers sin capa service detectable",
            description="Hay controllers, pero no se detectó una capa service clara.",
            evidence=components["controllers"][:10],
            recommendation="Introducir servicios o casos de uso para separar API de lógica de negocio.",
        ))

    if components["entities"] and not components["dtos"]:
        findings.append(Finding(
            severity="MEDIUM",
            category="api-design",
            title="Entidades sin DTOs detectables",
            description="Hay entidades/modelos, pero no se detectan DTOs.",
            evidence=components["entities"][:10],
            recommendation="Separar entidades de persistencia de contratos externos de API.",
        ))

    if not framework_signals["actuator_configured"]:
        findings.append(Finding(
            severity="INFO",
            category="observability",
            title="Actuator no detectable",
            description="No se detectó configuración explícita de management endpoints.",
            evidence=config_files[:5],
            recommendation="Evaluar health checks, métricas y endpoints de observabilidad.",
        ))

    architecture_map = {
        "packages": packages,
        "components": components,
        "framework_signals": framework_signals,
        "ci_delivery": {
            "github_actions": [f.path for f in files if f.kind == "ci"],
            "dockerfiles": [f.path for f in files if f.kind == "docker"],
        },
    }
    dependency_map = {"edges": edges, "edge_count": len(edges)}
    return architecture_map, dependency_map, findings


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def render_report(repo_ref: RepoRef, repo_uri: str, branch: str, files: list[SourceFile], arch: dict, dep: dict, findings: list[Finding]) -> str:
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

    lines.extend(["", "## Hallazgos", ""])
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.severity, []).append(finding)

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
                f"- Categoría: `{item.category}`",
                f"- Descripción: {item.description}",
                f"- Evidencia: {evidence}",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-uri", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--out", default=None)
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    parser.add_argument("--github-api-base", default=None)
    parser.add_argument("--max-files", type=int, default=500)
    parser.add_argument("--max-bytes-per-file", type=int, default=200_000)
    args = parser.parse_args(argv)

    repo_ref = parse_repo_uri(args.repo_uri, args.github_api_base)
    token = os.environ.get(args.github_token_env)
    out_dir = safe_output_dir(args.out, repo_ref.repo)

    files = list_remote_files(repo_ref, args.branch, token)
    files = [f for f in files if f.size <= args.max_bytes_per_file][: args.max_files]

    contents: dict[str, str] = {}
    for f in files:
        try:
            contents[f.path] = get_file_content(repo_ref, args.branch, f.path, token)
        except Exception as exc:
            contents[f.path] = f"/* ERROR downloading file: {exc} */"

    inventory = {
        "repo_uri": args.repo_uri,
        "host": repo_ref.host,
        "api_base": repo_ref.api_base,
        "owner": repo_ref.owner,
        "repo": repo_ref.repo,
        "branch": args.branch,
        "output_dir": str(out_dir),
        "rule": "reports_outside_agent_architecture",
        "files": [asdict(f) for f in files],
    }

    architecture_map, dependency_map, findings = build_maps(files, contents)

    write_json(out_dir / "source-inventory.json", inventory)
    write_json(out_dir / "architecture-map.json", architecture_map)
    write_json(out_dir / "dependency-map.json", dependency_map)
    write_json(out_dir / "architecture-findings.json", [asdict(f) for f in findings])

    report = render_report(repo_ref, args.repo_uri, args.branch, files, architecture_map, dependency_map, findings)
    (out_dir / "architecture-report.md").write_text(report, encoding="utf-8")

    print(f"OK: arquitectura analizada desde {args.repo_uri}@{args.branch}")
    print(f"Host: {repo_ref.host}")
    print(f"API base: {repo_ref.api_base}")
    print(f"Salida externa: {out_dir}")
    print(f"Archivos analizados: {len(files)}")
    print(f"Hallazgos: {len(findings)}")
    print(f"Reporte: {out_dir / 'architecture-report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
