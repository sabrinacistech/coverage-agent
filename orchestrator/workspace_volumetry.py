"""workspace_volumetry.py — physical disk-size metrics for the efficiency demo.

Mide, en bytes reales de disco, dos volúmenes y los compara para demostrar
empíricamente la tesis de la arquitectura: el contexto curado que enviamos al LLM
es órdenes de magnitud menor que el repositorio entero que procesaría un LLM con
acceso libre vía Agent Skills/Tools.

  * directory_size_bytes  — suma recursiva tolerante a fallos de permisos
    (os.walk con onerror no-op + try/except por archivo), excluyendo .git y
    directorios de build/deps (target, node_modules, ...). NUNCA levanta: ante
    cualquier error de FS devuelve lo acumulado hasta ese punto.
  * sum_file_sizes        — suma de un conjunto explícito de archivos (el contexto
    realmente enviado: los request-*.json del lote).
  * format_efficiency_table — la tabla ASCII prominente para STDOUT.

Sin dependencias externas: caracteres ASCII puros (no requiere `tabulate`).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

# Directorios que NO son "el repositorio" a efectos de la comparación: control de
# versiones, dependencias descargadas y artefactos de build. Incluir target/ es
# clave en repos Maven (suele pesar más que el código fuente). .claude evita contar
# los worktrees anidados del propio coverage-agent.
DEFAULT_EXCLUDES = frozenset({
    ".git", "node_modules", "target", "build", "dist", "out",
    ".idea", ".gradle", ".mvn", "__pycache__", ".pytest_cache",
    ".venv", "venv", ".claude",
})

_BYTES_PER_MB = 1_048_576
_BYTES_PER_KB = 1024


def directory_size_bytes(
    root: str | os.PathLike, *, exclude_dirs: Iterable[str] = DEFAULT_EXCLUDES
) -> int:
    """Tamaño físico recursivo de *root* en bytes, tolerante a fallos.

    Excluye (por nombre, en cualquier nivel) los directorios de *exclude_dirs*.
    Salta symlinks (evita loops y doble conteo). Cualquier OSError por permisos o
    carrera se ignora silenciosamente — la telemetría jamás rompe el pipeline.
    """
    root = Path(root)
    try:
        if not root.exists():
            return 0
        if root.is_file():
            return root.stat().st_size
    except OSError:
        return 0

    excl = set(exclude_dirs or ())
    total = 0
    # onerror=no-op: un directorio sin permiso de lectura no aborta el walk.
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        # Poda in-place: os.walk no desciende a los directorios removidos de la lista.
        dirnames[:] = [d for d in dirnames if d not in excl]
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                if os.path.islink(fp):
                    continue
                total += os.path.getsize(fp)
            except OSError:
                continue
    return total


def sum_file_sizes(paths: Iterable[str | os.PathLike]) -> int:
    """Suma de tamaños de un conjunto explícito de archivos, tolerante a fallos."""
    total = 0
    for p in paths:
        try:
            sp = os.fspath(p)
            if os.path.islink(sp):
                continue
            total += os.path.getsize(sp)
        except OSError:
            continue
    return total


def human_mb(n_bytes: int) -> str:
    return f"{n_bytes / _BYTES_PER_MB:.2f} MB"


def human_kb(n_bytes: int) -> str:
    return f"{n_bytes / _BYTES_PER_KB:.2f} KB"


def reduction_factor(repo_bytes: int, context_bytes: int) -> float:
    """Factor de reducción de ruido = repo / contexto. 0.0 si no hay contexto."""
    if context_bytes <= 0:
        return 0.0
    return repo_bytes / context_bytes


def _box(title: str, rows: list[str]) -> str:
    """Caja ASCII de ancho fijo: título centrado-izquierda + filas, alineadas."""
    inner = max([len(title)] + [len(r) for r in rows])
    bar = "+" + "-" * (inner + 2) + "+"
    out = [bar, f"| {title.ljust(inner)} |", bar]
    out += [f"| {r.ljust(inner)} |" for r in rows]
    out.append(bar)
    return "\n".join(out)


def format_efficiency_table(repo_bytes: int, context_bytes: int) -> str:
    """Tabla comparativa para STDOUT (ver formato en el spec de telemetría)."""
    factor = reduction_factor(repo_bytes, context_bytes)
    label_w = 37  # alinea los valores de las tres filas
    rows = [
        f"{'Tamaño Total del Repositorio Real:'.ljust(label_w)}{human_mb(repo_bytes)}",
        f"{'Tamaño del Contexto Enviado (Lote):'.ljust(label_w)}{human_kb(context_bytes)}",
        f"{'Factor de Reducción de Ruido:'.ljust(label_w)}{factor:.1f} x",
    ]
    return _box("METRICA DE EFICIENCIA DE CONTEXTO (COMPRESIÓN DE WORKSPACE)", rows)
