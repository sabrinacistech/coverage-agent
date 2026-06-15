from __future__ import annotations

import base64
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Protocol

from .models import RepoRef, SourceFile

PROTECTED_AGENT_DIRS = {"agents", "skills", "tools", "schemas"}


class RepoSource(Protocol):
    repo_ref: RepoRef

    def list_files(self) -> list[SourceFile]:
        ...

    def get_content(self, path: str) -> str:
        ...


def parse_repo_uri(repo_uri: str, github_api_base: str | None = None) -> RepoRef:
    parsed = urllib.parse.urlparse(repo_uri)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            "URI invalida. Formato esperado: https://<github-host>/<owner>/<repo>[.git]"
        )

    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(
            "URI invalida. Formato esperado: https://<github-host>/<owner>/<repo>[.git]"
        )

    owner = parts[0]
    repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    if github_api_base:
        api_base = github_api_base.rstrip("/")
    elif host == "github.com":
        api_base = "https://api.github.com"
    else:
        api_base = f"https://{host}/api/v3"
    return RepoRef(host=host, owner=owner, repo=repo, api_base=api_base)


def classify_path(path: str) -> str:
    p = path.lower().replace("\\", "/")
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
    if ".github/workflows/" in p:
        return "ci"
    if p.endswith(".yml") or p.endswith(".yaml") or p.endswith(".properties"):
        return "config"
    if p.endswith("dockerfile") or p == "dockerfile" or p.endswith(".dockerfile"):
        return "docker"
    if p.endswith(".md"):
        return "docs"
    return "other"


def is_relevant(path: str) -> bool:
    p = path.lower().replace("\\", "/")
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
    project_root = Path(__file__).resolve().parents[3].resolve()
    output = Path(out).expanduser().resolve() if out else (
        project_root.parent / "architecture-reviews" / repo_name
    ).resolve()

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


class GitHubRepoSource:
    def __init__(
        self,
        repo_ref: RepoRef,
        branch: str,
        token: str | None,
        *,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.repo_ref = repo_ref
        self.branch = branch
        self.token = token
        self.max_retries = max(0, max_retries)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def _request(self, url: str) -> bytes:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "coverage-agent-architecture-pilot")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                retry_after = exc.headers.get("Retry-After")
                rate_limited = (
                    exc.code in (429, 500, 502, 503, 504)
                    or (exc.code == 403 and (
                        exc.headers.get("X-RateLimit-Remaining") == "0"
                        or "rate limit" in body.lower()
                    ))
                )
                last_error = RuntimeError(f"HTTP {exc.code} consultando {url}\n{body}")
                if not rate_limited or attempt >= self.max_retries:
                    raise last_error from exc
                self._sleep_before_retry(attempt, retry_after)
            except urllib.error.URLError as exc:
                last_error = RuntimeError(f"Error de red consultando {url}: {exc}")
                if attempt >= self.max_retries:
                    raise last_error from exc
                self._sleep_before_retry(attempt, None)
            except ssl.SSLError as exc:
                raise RuntimeError(
                    "Error SSL al conectar con GitHub Enterprise. "
                    "Configurar certificados corporativos/CA bundle para Python. "
                    "No se recomienda desactivar verificacion SSL."
                ) from exc
        raise RuntimeError(str(last_error or f"No se pudo consultar {url}"))

    def _sleep_before_retry(self, attempt: int, retry_after: str | None) -> None:
        try:
            seconds = float(retry_after) if retry_after else self.backoff_seconds * (2 ** attempt)
        except ValueError:
            seconds = self.backoff_seconds * (2 ** attempt)
        if seconds > 0:
            print(f"WARN: retry GitHub REST en {seconds:.1f}s", file=sys.stderr)
            time.sleep(seconds)

    def _http_json(self, url: str) -> dict:
        return json.loads(self._request(url).decode("utf-8"))

    def list_files(self) -> list[SourceFile]:
        encoded_branch = urllib.parse.quote(self.branch, safe="")
        url = (
            f"{self.repo_ref.api_base}/repos/"
            f"{urllib.parse.quote(self.repo_ref.owner, safe='')}/"
            f"{urllib.parse.quote(self.repo_ref.repo, safe='')}/"
            f"git/trees/{encoded_branch}?recursive=1"
        )
        data = self._http_json(url)
        if data.get("truncated"):
            print(
                "WARN: GitHub devolvio tree truncado. "
                "El analisis puede estar incompleto. Considerar filtros o ZIP adapter.",
                file=sys.stderr,
            )

        files: list[SourceFile] = []
        for item in data.get("tree", []):
            if item.get("type") != "blob":
                continue
            path = item.get("path", "")
            if is_relevant(path):
                files.append(SourceFile(
                    path=path,
                    kind=classify_path(path),
                    size=int(item.get("size") or 0),
                ))
        return files

    def get_content(self, path: str) -> str:
        encoded_path = urllib.parse.quote(path)
        encoded_ref = urllib.parse.quote(self.branch, safe="")
        url = (
            f"{self.repo_ref.api_base}/repos/"
            f"{urllib.parse.quote(self.repo_ref.owner, safe='')}/"
            f"{urllib.parse.quote(self.repo_ref.repo, safe='')}/"
            f"contents/{encoded_path}?ref={encoded_ref}"
        )
        data = self._http_json(url)
        if isinstance(data, list):
            return ""
        if data.get("encoding") == "base64":
            return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        download_url = data.get("download_url")
        if download_url:
            return self._request(download_url).decode("utf-8", errors="replace")
        return ""


class LocalRepoSource:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not self.root.exists() or not self.root.is_dir():
            raise ValueError(f"repo local invalido: {self.root}")
        self.repo_ref = RepoRef(host="local", owner="local", repo=self.root.name, api_base="local")

    def list_files(self) -> list[SourceFile]:
        files: list[SourceFile] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if is_relevant(rel):
                files.append(SourceFile(path=rel, kind=classify_path(rel), size=path.stat().st_size))
        return files

    def get_content(self, path: str) -> str:
        target = (self.root / path).resolve()
        target.relative_to(self.root)
        return target.read_text(encoding="utf-8", errors="replace")


class ZipRepoSource:
    def __init__(self, zip_path: Path) -> None:
        self.zip_path = zip_path.resolve()
        if not self.zip_path.exists() or not self.zip_path.is_file():
            raise ValueError(f"zip invalido: {self.zip_path}")
        self.repo_ref = RepoRef(host="zip", owner="zip", repo=self.zip_path.stem, api_base="zip")
        self._prefix = self._detect_prefix()

    def _detect_prefix(self) -> str:
        with zipfile.ZipFile(self.zip_path) as zf:
            names = [n for n in zf.namelist() if n and not n.endswith("/")]
        first_parts = {n.split("/", 1)[0] for n in names if "/" in n}
        if len(first_parts) == 1:
            return next(iter(first_parts)) + "/"
        return ""

    def _logical_path(self, name: str) -> str:
        if self._prefix and name.startswith(self._prefix):
            return name[len(self._prefix):]
        return name

    def _physical_path(self, logical: str) -> str:
        return f"{self._prefix}{logical}" if self._prefix else logical

    def list_files(self) -> list[SourceFile]:
        files: list[SourceFile] = []
        with zipfile.ZipFile(self.zip_path) as zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                if info.is_dir():
                    continue
                rel = self._logical_path(info.filename)
                if is_relevant(rel):
                    files.append(SourceFile(path=rel, kind=classify_path(rel), size=int(info.file_size)))
        return files

    def get_content(self, path: str) -> str:
        with zipfile.ZipFile(self.zip_path) as zf:
            return zf.read(self._physical_path(path)).decode("utf-8", errors="replace")


def _path_from_file_uri(repo_uri: str) -> Path:
    parsed = urllib.parse.urlparse(repo_uri)
    return Path(urllib.request.url2pathname(parsed.path))


def resolve_repo_source(
    repo_uri: str,
    *,
    branch: str,
    github_api_base: str | None,
    token: str | None,
) -> RepoSource:
    if len(repo_uri) >= 2 and repo_uri[1] == ":":
        path = Path(repo_uri)
        return ZipRepoSource(path) if path.suffix.lower() == ".zip" else LocalRepoSource(path)
    parsed = urllib.parse.urlparse(repo_uri)
    if parsed.scheme in ("", "file"):
        path = _path_from_file_uri(repo_uri) if parsed.scheme == "file" else Path(repo_uri)
        return ZipRepoSource(path) if path.suffix.lower() == ".zip" else LocalRepoSource(path)
    repo_ref = parse_repo_uri(repo_uri, github_api_base)
    return GitHubRepoSource(repo_ref, branch, token)
