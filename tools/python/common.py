"""Shared helpers for tools/python/*.

Keep this dependency-light. Only stdlib + jsonschema + lxml.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMAS_DIR = Path(__file__).resolve().parents[1].parent / "state" / "_schemas"

IS_WINDOWS = os.name == "nt"


def _configure_stdio_utf8() -> None:
    """Force stdout/stderr to utf-8 so non-ASCII output never breaks cp1252 consoles."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


_configure_stdio_utf8()


# ── Structured logging (P4.0) ─────────────────────────────────────────────────

def emit_tool_summary(
    tool: str,
    status: str,
    artifacts: list | None = None,
    duration_ms: int | None = None,
    **extra: Any,
) -> None:
    """Emit a single-line JSON tool summary on stdout.

    Intended as the LAST line printed by a tool's main() so callers (e.g.
    orchestrators) can parse one structured record per invocation.
    """
    payload: dict[str, Any] = {"tool": tool, "status": status}
    if artifacts is not None:
        payload["artifacts"] = artifacts
    if duration_ms is not None:
        payload["durationMs"] = int(duration_ms)
    for k, v in extra.items():
        if v is not None:
            payload[k] = v
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


class _TimedRun:
    """Context manager that times a tool invocation and emits a summary on exit.

    Usage:
        with _TimedRun("my_tool") as tr:
            ...
            tr.set_status("FAIL")           # optional override
            tr.set_artifacts([...])         # optional
            tr.add("extraField", value)     # optional kv pair
    Emits emit_tool_summary(tool, status, artifacts, duration_ms, **extra)
    on __exit__. status is "FAIL" if an exception propagates, otherwise the
    last value set (default "OK").
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.status: str = "OK"
        self.artifacts: list | None = None
        self.extra: dict[str, Any] = {}
        self._t0: float = 0.0

    def __enter__(self) -> "_TimedRun":
        self._t0 = time.perf_counter()
        return self

    def set_status(self, status: str) -> None:
        self.status = status

    def set_artifacts(self, artifacts: list) -> None:
        self.artifacts = artifacts

    def add(self, key: str, value: Any) -> None:
        self.extra[key] = value

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        if exc_type is None:
            status = self.status
        elif issubclass(exc_type, SystemExit):
            code = getattr(exc, "code", 0)
            if isinstance(code, int):
                status = self.status if code == 0 else "FAIL"
            else:
                status = "FAIL" if code else self.status
        else:
            status = "FAIL"
        try:
            emit_tool_summary(
                self.tool,
                status,
                artifacts=self.artifacts,
                duration_ms=duration_ms,
                **self.extra,
            )
        except Exception:
            pass
        return False


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    """Write text via tmp + os.replace, same crash-safety as atomic_write_json.
    Use for any on-disk file a half-write could corrupt (e.g. Java test files an
    AV scan or crash could truncate mid-write on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate(state_name: str, data: Any) -> None:
    """Validate `data` against state/_schemas/<state_name>.schema.json.
    No-op if jsonschema is not installed.
    """
    try:
        import jsonschema  # type: ignore
    except Exception:
        return
    schema_path = SCHEMAS_DIR / f"{state_name}.schema.json"
    if not schema_path.exists():
        return
    with schema_path.open("r", encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(data, schema)


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a subprocess with UTF-8 decoding (Windows-safe).

    Differences vs ``subprocess.run`` defaults:
    - ``encoding="utf-8"`` so non-ASCII output never explodes on cp1252.
    - ``errors="replace"`` so a stray invalid byte does not kill the tool.
    - On Windows, ``.cmd``/``.bat`` shims (e.g. mvn.cmd) are resolved through
      ``shutil.which`` so the launcher does not need a shell.
    """
    resolved = list(cmd)
    if IS_WINDOWS and resolved:
        head = resolved[0]
        # Resolve bare names to absolute path so .cmd/.bat shims work without shell=True.
        if not os.path.isabs(head) and os.sep not in head and "/" not in head:
            found = shutil.which(head)
            if found:
                resolved[0] = found
    return subprocess.run(
        resolved,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def find_tool(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise FileNotFoundError(f"Tool not on PATH: {name}")
    return p


def mvn_executable() -> str:
    """Return the Maven launcher for this platform.

    Windows ships Maven as ``mvn.cmd`` (a batch wrapper). Calling ``mvn`` via
    ``subprocess.run`` without ``shell=True`` fails on Windows because the
    ``PATHEXT`` lookup is not performed for raw executables.
    """
    candidates = ("mvn.cmd", "mvn.bat", "mvn") if IS_WINDOWS else ("mvn",)
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError(
        "Maven launcher not found on PATH. "
        "Install Maven and ensure 'mvn' (or 'mvn.cmd' on Windows) is reachable."
    )


def long_path(p: Path | str) -> str:
    """Return a Windows long-path-safe string for ``p``.

    On Windows the legacy MAX_PATH limit is 260 characters. Prefixing an
    absolute path with ``\\\\?\\`` opts it into the long-path API (~32k).
    On POSIX this is a no-op.
    """
    s = str(p)
    if not IS_WINDOWS:
        return s
    try:
        absolute = os.path.abspath(s)
    except (OSError, ValueError):
        return s
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        # UNC path: \\server\share → \\?\UNC\server\share
        return "\\\\?\\UNC\\" + absolute.lstrip("\\")
    return "\\\\?\\" + absolute


def resolve_target_dirs(repo: Path, module: str | None = None) -> list[Path]:
    """Locate Maven ``target/classes`` directories under ``repo``.

    Resolution order:
      1. If ``module`` names a real subdir, use ``repo/module/target/classes``.
      2. If ``module`` is ``None`` / ``"."`` / ``""``, use the monolithic
         ``repo/target/classes`` when present.
      3. Fall back to scanning every ``pom.xml`` (via :func:`find_pom_modules`)
         and returning each module's ``target/classes`` that exists.

    Returns only directories that actually exist; callers should treat an
    empty list as "nothing built yet — run mvn -DskipTests package first".
    """
    repo = repo.resolve()
    candidates: list[Path] = []

    if module and module not in (".", ""):
        explicit = repo / module / "target" / "classes"
        if explicit.is_dir():
            return [explicit]
        # Module name given but no build output — fall through, caller decides.

    monolithic = repo / "target" / "classes"
    if monolithic.is_dir():
        candidates.append(monolithic)

    for mod_dir in find_pom_modules(repo):
        tc = mod_dir / "target" / "classes"
        if tc.is_dir() and tc not in candidates:
            candidates.append(tc)

    return candidates


def normalize_params(params: Any) -> list[dict]:
    """Coerce a params list into the structured ``[{type, name?}]`` shape.

    Some intermediate artifacts historically stored params as plain
    ``["String", "int"]``. Downstream consumers expect dicts and crash on
    ``str.get(...)``. This helper is idempotent and safe to call defensively.
    """
    if not params:
        return []
    out: list[dict] = []
    for p in params:
        if isinstance(p, dict):
            if "type" in p:
                out.append(p)
            else:
                # Unknown shape — keep deterministic fallback rather than raising.
                out.append({"type": "java.lang.Object"})
        elif isinstance(p, str):
            out.append({"type": p})
        else:
            out.append({"type": "java.lang.Object"})
    return out


def cache_get(state_dir: Path, key: str, input_hashes: dict[str, str]) -> Any | None:
    cache_file = state_dir / "_cache" / f"{key}.cache.json"
    if not cache_file.exists():
        return None
    try:
        c = load_json(cache_file)
    except Exception:
        return None
    if c.get("inputs") == input_hashes:
        return c.get("output")
    return None


def cache_put(state_dir: Path, key: str, input_hashes: dict[str, str], output: Any) -> None:
    cache_file = state_dir / "_cache" / f"{key}.cache.json"
    atomic_write_json(cache_file, {"inputs": input_hashes, "output": output})


def fail(msg: str, code: int = 2) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)
    sys.exit(code)


def find_pom_modules(repo: Path, contract: Path | str | None = None) -> list[Path]:
    """Best-effort list of Maven module directories (root + children with pom.xml).

    Consolidación (audit): el descubrimiento de módulos lo hace UNA sola vez
    ``pom_parser`` (primer paso de la Fase 0) → ``build-tool-contract.json``.
    Cuando se pasa ``contract`` (la ruta a ese JSON) y existe, se reutiliza su
    lista de módulos en lugar de volver a caminar el árbol con ``rglob`` — así
    los 4 pasos de discovery siguientes (archetype/generated/classpath/stack) no
    repiten el mismo walk del repo. Si el contrato falta o no se puede leer, cae
    al ``rglob`` (comportamiento previo, sin cambios observables)."""
    if contract is not None:
        try:
            data = json.loads(Path(contract).read_text(encoding="utf-8"))
            mods = [Path(m["path"]) for m in data.get("modules", []) if m.get("path")]
            if mods:
                return mods
        except Exception:
            pass  # contrato ausente/ilegible → fallback al rglob
    poms = sorted(repo.rglob("pom.xml"))
    # Skip generated/build dirs
    poms = [p for p in poms if "target" not in p.parts and "build" not in p.parts]
    return [p.parent for p in poms]
