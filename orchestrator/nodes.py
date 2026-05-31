"""nodes.py — trabajo del ciclo como nodo del grafo (E1.2).

El "trabajo de un ciclo" (fase 8 generación → 9 aplicación/validación → 10 repair)
ya vive, probado, en one_cycle.run_one_cycle. El nodo lo envuelve para no
reescribir nada. Aislarlo acá permite a los tests de paridad inyectar un trabajo
determinista sin tocar el grafo ni el control de budget/G8.
"""
from __future__ import annotations

from pathlib import Path

from . import one_cycle


def run_cycle_work(state_dir, repo) -> int:
    """Ejecuta el trabajo de UN ciclo y devuelve su exit code
    (0 ok · 2 budget · 7 sin targets), igual que one_cycle."""
    return one_cycle.run_one_cycle(Path(state_dir), Path(repo))
