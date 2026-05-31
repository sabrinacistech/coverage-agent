"""graph_state.py — estado del grafo LangGraph (E1.2).

Refleja los campos de execution-state.json que gobiernan la finitud del ciclo,
más punteros de ejecución. NO duplica thresholds: budget/G8 se evalúan llamando
a las funciones del núcleo (budget_enforcer, gate_runner), no copiándolas.
"""
from __future__ import annotations

from typing import Optional, TypedDict


class GraphState(TypedDict, total=False):
    # Entradas (constantes durante el run)
    state_dir: str
    repo: str
    exec_state_path: str
    done_exit_code: int
    max_iterations: Optional[int]
    # Evolución del run
    iterations: int
    last_cmd_rc: Optional[int]
    # Resultado de parada
    status: Optional[str]   # "running" | "DONE" | "BUDGET" | "STALL" | "STATE_MALFORMED"
    stop_rc: Optional[int]
