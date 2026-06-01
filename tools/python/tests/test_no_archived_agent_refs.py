"""test_no_archived_agent_refs.py — los nombres de los agentes LLM archivados
(removidos del árbol) NO deben aparecer en NINGÚN archivo vivo.

Limpieza de arquitectura: se eliminaron los stubs `agents/_archive/`, los reportes
históricos de raíz y toda referencia `ex-<agente>` (reescritas para nombrar solo
el módulo Python que materializa la fase). Este guard impide que esos nombres
reaparezcan —como productor vivo o como nota histórica— y vuelvan a sugerir que
las fases deterministas (1-7, 9, 10a, 11) son turnos LLM.

Escanea .md/.json/.py vivos (excluye .venv/.git y el propio dir de tests, que
contiene la lista) y falla si aparece cualquier nombre archivado.

Run: `python tools/python/tests/test_no_archived_agent_refs.py`  (non-zero on failure)
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARCH_ROOT = HERE.parents[2]  # tools/python/tests → tools/python → tools → <root>

ARCHIVED = [
    "classification-agent", "dependency-graph-agent", "discovery-agent",
    "fixture-agent", "mutation-agent", "planning-agent", "reporting-agent",
    "repository-intelligence-agent", "stack-profile-agent",
    "symbol-contract-agent", "validation-agent",
]

EXCLUDE_TOP = {".venv", ".git"}

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ OK ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        FAILURES.append(label)


def _excluded(rel: Path) -> bool:
    parts = rel.parts
    if parts and parts[0] in EXCLUDE_TOP:
        return True
    # el propio directorio de tests contiene la lista ARCHIVED (este archivo)
    return parts[:3] == ("tools", "python", "tests")


def _scan() -> tuple[list[str], int]:
    violations: list[str] = []
    scanned = 0
    for path in [*ARCH_ROOT.rglob("*.md"), *ARCH_ROOT.rglob("*.json"), *ARCH_ROOT.rglob("*.py")]:
        rel = path.relative_to(ARCH_ROOT)
        if _excluded(rel):
            continue
        scanned += 1
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines, 1):
            for name in ARCHIVED:
                if name in line:
                    violations.append(f"{rel}:{i}: `{name}` → {line.strip()}")
    return violations, scanned


def case_no_archived_refs() -> None:
    print("== ningún nombre de agente archivado aparece en archivos vivos ==")
    violations, scanned = _scan()
    _assert("scanned a non-trivial set of files", scanned > 20, f"scanned={scanned}")
    _assert("cero referencias a agentes archivados",
            not violations, "\n    " + "\n    ".join(violations))


def main() -> int:
    case_no_archived_refs()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} case(s): {FAILURES}")
        return 1
    print("All archived-agent-reference cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
