#!/usr/bin/env python3
"""
Piloto: análisis de arquitectura desde URI GitHub sin git clone manual.

Diseñado como pipeline hermano de run_pipeline.py:
- No compila.
- No requiere JaCoCo.
- No escribe en el repo objetivo.
- Descarga solo archivos relevantes desde GitHub Contents API / raw URLs.

Uso:
  python tools/python/run_architecture_review.py \
    --repo-uri https://github.com/org/repo \
    --branch main \
    --out ./architecture-state

Para repos privados:
  set GITHUB_TOKEN=...
  python tools/python/run_architecture_review.py --repo-uri ... --github-token-env GITHUB_TOKEN
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import fnmatch
import hashlib
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

RELEVANT_EXTENSIONS = {
    ".java", ".kt", ".xml", ".gradle", ".kts", ".yml", ".yaml",
    ".properties", ".md", ".dockerfile", ".tf", ".json"
}
RELEVANT_BASENAMES = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "Dockerfile", "Jenkinsfile", "README.md", "application.yml", "application.yaml",
    "application.properties", "openapi.yml", "openapi.yaml"
}
DEFAULT_EXCLUDES = [
    "target/**", "build/**", ".git/**", ".idea/**", ".vscode/**", "node_modules/**",
    "**/generated/**", "**/build/generated/**", "**/target/generated-sources/**",
]
MAX_FILE_BYTES_DEFAULT = 250_000
MAX_FILES_DEFAULT = 900


@dataclasses.dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    sha: str
    download_url: str | None


def parse_github_uri(uri: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(uri)
    if parsed.netloc.lower() != "github.com":
        raise ValueError("Este piloto solo soporta URI github.com. Para GitLab/Bitbucket agregar otro adapter.")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError("URI GitHub inválida. Esperado: https://github.com/{owner}/{repo}")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def github_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "coverage-agent-architecture-pilot",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def http_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API error {exc.code} for {url}: {body[:500]}") from exc


def http_text(url: str, headers: dict[str, str]) -> str:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Download error {exc.code} for {url}: {body[:300]}") from exc


def get_default_branch(owner: str, repo: str, headers: dict[str, str]) -> str:
    data = http_json(f"https://api.github.com/repos/{owner}/{repo}", headers)
    return data.get("default_branch") or "main"


def list_tree(owner: str, repo: str, branch: str, headers: dict[str, str]) -> tuple[list[RemoteFile], bool]:
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1"
    data = http_json(url, headers)
    truncated = bool(data.get("truncated"))
    files: list[RemoteFile] = []
    for item in data.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = item.get("path") or ""
        size = int(item.get("size") or 0)
        sha = item.get("sha") or ""
        files.append(RemoteFile(path=path, size=size, sha=sha, download_url=None))
    return files, truncated


def is_relevant(path: str, size: int, max_file_bytes: int, includes: list[str], excludes: list[str]) -> bool:
    norm = path.replace("\\", "/")
    if size > max_file_bytes:
        return False
    if any(fnmatch.fnmatch(norm, pattern) for pattern in excludes):
        return False
    if includes and not any(fnmatch.fnmatch(norm, pattern) for pattern in includes):
        return False
    base = os.path.basename(norm)
    ext = os.path.splitext(base)[1]
    return base in RELEVANT_BASENAMES or ext in RELEVANT_EXTENSIONS or norm.startswith(".github/workflows/")


def raw_url(owner: str, repo: str, branch: str, path: str) -> str:
    quoted_path = "/".join(urllib.parse.quote(part) for part in path.split("/"))
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{urllib.parse.quote(branch)}/{quoted_path}"


def package_of_java(source: str) -> str | None:
    m = re.search(r"^\s*package\s+([a-zA-Z_][\w.]*)\s*;", source, flags=re.MULTILINE)
    return m.group(1) if m else None


def class_name_of_java(source: str) -> str | None:
    m = re.search(r"\b(class|interface|enum|record)\s+([A-Za-z_][A-Za-z0-9_]*)", source)
    return m.group(2) if m else None


def annotations_of_java(source: str) -> list[str]:
    return sorted(set(re.findall(r"@([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)", source)))


def imports_of_java(source: str) -> list[str]:
    return sorted(set(re.findall(r"^\s*import\s+(?:static\s+)?([a-zA-Z_][\w.]*)(?:\.\*)?\s*;", source, flags=re.MULTILINE)))


def classify_java(path: str, source: str) -> str:
    anns = set(annotations_of_java(source))
    lowered = path.lower()
    if anns & {"RestController", "Controller"} or "/controller" in lowered:
        return "controller"
    if anns & {"Service", "Component"} or "/service" in lowered:
        return "service"
    if anns & {"Repository"} or "/repository" in lowered or "/dao" in lowered:
        return "repository"
    if anns & {"Entity", "MappedSuperclass", "Embeddable"} or "/entity" in lowered or "/model" in lowered:
        return "domain-or-entity"
    if anns & {"Configuration"} or "/config" in lowered:
        return "configuration"
    if "/dto" in lowered or path.endswith("Dto.java") or path.endswith("DTO.java"):
        return "dto"
    if "/exception" in lowered:
        return "exception"
    return "unknown"


def detect_framework(files: dict[str, str]) -> dict[str, Any]:
    joined = "\n".join(files.get(p, "")[:50_000] for p in files if p.endswith(("pom.xml", ".gradle", ".kts", ".yml", ".yaml", ".properties")))
    markers = {
        "spring_boot": ["spring-boot-starter", "SpringBootApplication"],
        "spring_web": ["spring-boot-starter-web", "@RestController"],
        "spring_data_jpa": ["spring-boot-starter-data-jpa", "JpaRepository", "@Entity"],
        "spring_security": ["spring-boot-starter-security", "SecurityFilterChain", "WebSecurityConfigurerAdapter"],
        "actuator": ["spring-boot-starter-actuator", "management.endpoints"],
        "openapi": ["springdoc-openapi", "swagger", "openapi"],
        "lombok": ["lombok", "@Getter", "@Builder", "@Data"],
        "mapstruct": ["mapstruct", "@Mapper"],
        "kafka": ["spring-kafka", "KafkaTemplate", "@KafkaListener"],
        "junit5": ["junit-jupiter", "org.junit.jupiter"],
        "junit4": ["junit:junit", "org.junit.Test"],
    }
    return {name: any(marker in joined for marker in marker_list) for name, marker_list in markers.items()}


def build_inventory(files: dict[str, str], meta: list[RemoteFile]) -> dict[str, Any]:
    by_ext = Counter(Path(f.path).suffix or Path(f.path).name for f in meta)
    java_entries = []
    for path, source in files.items():
        if not path.endswith(".java"):
            continue
        java_entries.append({
            "path": path,
            "package": package_of_java(source),
            "className": class_name_of_java(source),
            "role": classify_java(path, source),
            "annotations": annotations_of_java(source),
            "imports": imports_of_java(source),
            "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        })
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "fileCount": len(files),
        "byExtension": dict(by_ext),
        "javaTypes": java_entries,
    }


def build_architecture_map(inventory: dict[str, Any], framework: dict[str, Any]) -> dict[str, Any]:
    roles = Counter(entry["role"] for entry in inventory["javaTypes"])
    packages = Counter(entry["package"] for entry in inventory["javaTypes"] if entry.get("package"))
    root_candidates = Counter(pkg.split(".")[0] + "." + pkg.split(".")[1] if pkg and len(pkg.split(".")) > 1 else pkg for pkg in packages)
    return {
        "frameworkProfile": framework,
        "roleCounts": dict(roles),
        "topPackages": dict(packages.most_common(30)),
        "rootPackageCandidates": dict(root_candidates.most_common(10)),
        "hasLayeredShape": roles.get("controller", 0) > 0 and roles.get("service", 0) > 0,
        "hasPersistenceShape": roles.get("repository", 0) > 0 or roles.get("domain-or-entity", 0) > 0,
    }


def build_dependency_map(inventory: dict[str, Any]) -> dict[str, Any]:
    known_packages = {entry["package"] for entry in inventory["javaTypes"] if entry.get("package")}
    edges = []
    package_edges = Counter()
    for entry in inventory["javaTypes"]:
        src_pkg = entry.get("package")
        if not src_pkg:
            continue
        for imp in entry.get("imports", []):
            dst_pkg = ".".join(imp.split(".")[:-1])
            if dst_pkg in known_packages or any(dst_pkg.startswith(p + ".") for p in known_packages):
                package_edges[(src_pkg, dst_pkg)] += 1
                edges.append({"from": src_pkg, "to": dst_pkg, "import": imp, "source": entry["path"]})
    return {
        "internalImportEdges": edges[:1000],
        "packageEdgeCounts": [
            {"from": k[0], "to": k[1], "count": v}
            for k, v in package_edges.most_common(200)
        ],
    }


def evaluate_rules(files: dict[str, str], inventory: dict[str, Any], architecture: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    roles = Counter(entry["role"] for entry in inventory["javaTypes"])

    def add(severity: str, rule: str, title: str, evidence: list[str], recommendation: str) -> None:
        findings.append({
            "severity": severity,
            "rule": rule,
            "title": title,
            "evidence": evidence[:20],
            "recommendation": recommendation,
        })

    if roles.get("controller", 0) and roles.get("service", 0) == 0:
        add("HIGH", "LAYER-001", "Hay controllers pero no se detecta capa de servicios", [], "Introducir servicios de aplicación para evitar lógica de negocio en controllers.")

    suspicious_controller_repo = []
    for entry in inventory["javaTypes"]:
        if entry["role"] == "controller" and any("Repository" in imp or ".repository." in imp.lower() for imp in entry.get("imports", [])):
            suspicious_controller_repo.append(entry["path"])
    if suspicious_controller_repo:
        add("HIGH", "LAYER-002", "Controllers importan repositories directamente", suspicious_controller_repo, "Hacer que controllers dependan de servicios/casos de uso, no de persistencia.")

    entity_exposure = []
    for entry in inventory["javaTypes"]:
        if entry["role"] == "controller":
            src = files.get(entry["path"], "")
            if "@Entity" in src or re.search(r"ResponseEntity\s*<\s*[A-Za-z0-9_]*Entity", src):
                entity_exposure.append(entry["path"])
    if entity_exposure:
        add("MEDIUM", "API-001", "Posible exposición de entidades en la API", entity_exposure, "Separar DTOs de entidades JPA y mapear explícitamente.")

    config_files = [p for p in files if p.endswith((".yml", ".yaml", ".properties"))]
    secret_hits = []
    secret_re = re.compile(r"(?i)(password|secret|token|apikey|api-key)\s*[:=]\s*[^\s${][^\n#]+")
    for path in config_files:
        if secret_re.search(files[path]):
            secret_hits.append(path)
    if secret_hits:
        add("CRITICAL", "SEC-001", "Posibles secretos hardcodeados en configuración", secret_hits, "Mover secretos a vault/variables de entorno y dejar placeholders.")

    if architecture["frameworkProfile"].get("spring_boot") and not architecture["frameworkProfile"].get("actuator"):
        add("MEDIUM", "OBS-001", "No se detecta Spring Boot Actuator", [], "Agregar actuator y exponer health/metrics según política corporativa.")

    if not any(Path(p).name == "Dockerfile" for p in files):
        add("LOW", "DELIVERY-001", "No se detecta Dockerfile", [], "Confirmar si la imagen se construye con buildpack corporativo; si no, agregar Dockerfile estándar.")

    if not any(p.startswith(".github/workflows/") or "jenkinsfile" in p.lower() for p in files):
        add("LOW", "DELIVERY-002", "No se detecta pipeline CI/CD versionado", [], "Agregar workflow/Jenkinsfile o documentar el pipeline externo.")

    if roles.get("unknown", 0) > max(10, len(inventory["javaTypes"]) * 0.6):
        add("INFO", "STRUCT-001", "Muchos tipos Java no pudieron clasificarse por rol", [], "Evaluar convenciones de paquetes/anotaciones o extender las reglas de clasificación.")

    return findings


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def render_report(repo_uri: str, branch: str, inventory: dict[str, Any], architecture: dict[str, Any], findings: list[dict[str, Any]], truncated: bool) -> str:
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    findings_sorted = sorted(findings, key=lambda f: severity_order.get(f["severity"], 99))
    lines = [
        "# Architecture Review — piloto remoto",
        "",
        f"- Repo: `{repo_uri}`",
        f"- Branch: `{branch}`",
        f"- Generado: `{inventory['generatedAt']}`",
        f"- Archivos analizados: `{inventory['fileCount']}`",
        f"- Tree truncado por GitHub API: `{truncated}`",
        "",
        "## Perfil detectado",
        "",
    ]
    for k, v in architecture["frameworkProfile"].items():
        lines.append(f"- {k}: `{v}`")
    lines += ["", "## Capas / roles", ""]
    for k, v in architecture["roleCounts"].items():
        lines.append(f"- {k}: `{v}`")
    lines += ["", "## Hallazgos", ""]
    if not findings_sorted:
        lines.append("No se detectaron hallazgos con las reglas piloto.")
    for f in findings_sorted:
        evidence = ", ".join(f.get("evidence") or []) or "sin archivo puntual"
        lines += [
            f"### {f['severity']} — {f['rule']} — {f['title']}",
            "",
            f"Evidencia: {evidence}",
            "",
            f"Recomendación: {f['recommendation']}",
            "",
        ]
    lines += [
        "## Limitaciones del piloto",
        "",
        "Este análisis es estático y remoto. No compila, no levanta Spring, no resuelve classpath real y no reemplaza los gates determinísticos del flujo de cobertura.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Piloto de análisis arquitectónico remoto para coverage-agent")
    parser.add_argument("--repo-uri", required=True, help="URI GitHub: https://github.com/{owner}/{repo}")
    parser.add_argument("--branch", default=None, help="Branch/ref. Si se omite, usa default_branch")
    parser.add_argument("--out", required=True, help="Directorio de salida del estado")
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN", help="Nombre de env var con token GitHub")
    parser.add_argument("--include", action="append", default=[], help="Glob de inclusión, repetible")
    parser.add_argument("--exclude", action="append", default=[], help="Glob de exclusión adicional, repetible")
    parser.add_argument("--max-files", type=int, default=MAX_FILES_DEFAULT)
    parser.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES_DEFAULT)
    args = parser.parse_args(argv)

    owner, repo = parse_github_uri(args.repo_uri)
    token = os.environ.get(args.github_token_env)
    headers = github_headers(token)
    branch = args.branch or get_default_branch(owner, repo, headers)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    remote_files, truncated = list_tree(owner, repo, branch, headers)
    excludes = DEFAULT_EXCLUDES + args.exclude
    selected = [
        f for f in remote_files
        if is_relevant(f.path, f.size, args.max_file_bytes, args.include, excludes)
    ][: args.max_files]

    contents: dict[str, str] = {}
    for f in selected:
        url = raw_url(owner, repo, branch, f.path)
        contents[f.path] = http_text(url, headers)

    inventory = build_inventory(contents, selected)
    framework = detect_framework(contents)
    architecture = build_architecture_map(inventory, framework)
    dependency_map = build_dependency_map(inventory)
    findings = evaluate_rules(contents, inventory, architecture)
    report = render_report(args.repo_uri, branch, inventory, architecture, findings, truncated)

    write_json(out / "source-inventory.json", inventory)
    write_json(out / "architecture-map.json", architecture)
    write_json(out / "dependency-map.json", dependency_map)
    write_json(out / "architecture-findings.json", findings)
    (out / "architecture-report.md").write_text(report, encoding="utf-8")

    print(f"OK: arquitectura analizada desde {args.repo_uri}@{branch}")
    print(f"Archivos analizados: {len(contents)}")
    print(f"Hallazgos: {len(findings)}")
    print(f"Reporte: {out / 'architecture-report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
