"""graph.py — LangGraph como orquestador del ciclo (E1.2).

El grafo es una representación fiel del bucle de cycle_loop.run_loop, pero
expresado como StateGraph. Punto clave (regla de oro + anti-divergencia): NO
reimplementa la lógica de finitud — llama a las MISMAS funciones del núcleo que
cycle_loop (budget_enforcer.tick/check/check_token_budget/reset,
cycle_loop.record_outcome, gate_runner.gate_g8). Por eso para en las mismas
condiciones (presupuesto rc2, G8 stall rc5, DONE rc0). La paridad se valida en
tests/orchestrator/test_graph_parity.py.

El grafo tiene un único nodo `cycle` que se reencola a sí mismo mientras el run
siga vivo; una arista condicional lo enruta a END cuando se fija un estado de
parada.
"""
from __future__ import annotations

import sys
from pathlib import Path

from langgraph.graph import END, StateGraph

from . import config, nodes
from .graph_state import GraphState

sys.path.insert(0, str(config.TOOLS_PYTHON))
import budget_enforcer  # noqa: E402  (núcleo — no se reimplementa)
import cycle_loop  # noqa: E402  (misma fuente de RC + record_outcome + _read_cycle_delta)
from gate_runner import gate_g8  # noqa: E402  (única fuente de thresholds G8)

# Reusar los MISMOS códigos de salida que cycle_loop (no redefinir).
RC_DONE = cycle_loop.RC_DONE
RC_BUDGET = cycle_loop.RC_BUDGET_EXCEEDED
RC_STATE_MALFORMED = cycle_loop.RC_STATE_MALFORMED
RC_STALL = cycle_loop.RC_CONVERGENCE_STALL
_ABSOLUTE_CAP = cycle_loop._ABSOLUTE_SAFETY_CAP


def _cap(state: GraphState) -> int:
    return min(state.get("max_iterations") or _ABSOLUTE_CAP, _ABSOLUTE_CAP)


def cycle_node(state: GraphState) -> dict:
    """Un ciclo, en el MISMO orden que cycle_loop.run_loop."""
    sp = Path(state["exec_state_path"])
    sd = Path(state["state_dir"])
    done = state["done_exit_code"]

    # 1. tick — budget_enforcer incrementa cycle + estampa inicio.
    trc, _ = budget_enforcer.tick(sp)
    if trc != 0:
        budget_enforcer.reset(sp)
        return {"status": "STATE_MALFORMED", "stop_rc": RC_STATE_MALFORMED}

    # 2. budget (maxCycles / maxMinutesPerCycle).
    crc, _ = budget_enforcer.check(sp)
    if crc != 0:
        budget_enforcer.reset(sp)
        return {"status": "BUDGET", "stop_rc": RC_BUDGET}

    # 2b. token budget — antes de dispatch, igual que cycle_loop.
    tcrc, _ = budget_enforcer.check_token_budget(sd)
    if tcrc != 0:
        budget_enforcer.reset(sp)
        return {"status": "BUDGET", "stop_rc": RC_BUDGET}

    # Stale-guard (audit M3, idéntico a cycle_loop.run_loop): borrar el
    # coverage-delta.json previo ANTES del ciclo, así un ciclo que NO mide
    # (skip/block estructural o baseline ausente) no re-lee el delta del ciclo
    # anterior y no dispara un falso stall G8.
    delta_path = sd / "coverage-delta.json"
    try:
        delta_path.unlink()
    except FileNotFoundError:
        pass

    # 3. trabajo del ciclo (gen→patch→validación). Reescribe coverage-delta.json.
    cmd_rc = nodes.run_cycle_work(sd, Path(state["repo"]))

    # 4. registrar resultado (los dos campos que lee G8), tri-estado igual a M3:
    #    solo un ciclo MEDIDO y plano cuenta como zero-delta; "no medido" preserva.
    delta = cycle_loop._read_cycle_delta(sd)
    compile_failed = cmd_rc not in (0, done)
    if delta is None:
        zero_delta: bool | None = None
    else:
        zero_delta = (delta[0] == 0 and delta[1] == 0)
    cycle_loop.record_outcome(sp, zero_delta=zero_delta, compile_failed=compile_failed)

    # 5. reset del cronómetro del ciclo.
    budget_enforcer.reset(sp)

    # 6. G8 — convergencia.
    if gate_g8(sd).get("status") == "FAIL":
        return {"status": "STALL", "stop_rc": RC_STALL}

    iters = int(state.get("iterations", 0)) + 1
    # 7. DONE (sin más targets) o tope de seguridad absoluto.
    if cmd_rc == done or iters >= _cap(state):
        return {"iterations": iters, "status": "DONE", "stop_rc": RC_DONE}

    # seguir
    return {"iterations": iters, "status": "running", "stop_rc": None, "last_cmd_rc": cmd_rc}


def _route(state: GraphState):
    return "cycle" if state.get("status") in (None, "running") else END


def build_graph():
    g = StateGraph(GraphState)
    g.add_node("cycle", cycle_node)
    g.set_entry_point("cycle")
    g.add_conditional_edges("cycle", _route, {"cycle": "cycle", END: END})
    return g.compile()


def run_graph(state_dir, repo, *, done_exit_code: int = 7, max_iterations: int | None = None) -> int:
    """Corre el grafo y devuelve el RC de parada (mismos códigos que cycle_loop)."""
    app = build_graph()
    init: GraphState = {
        "state_dir": str(state_dir),
        "repo": str(repo),
        "exec_state_path": str(Path(state_dir) / "execution-state.json"),
        "done_exit_code": done_exit_code,
        "max_iterations": max_iterations,
        "iterations": 0,
        "last_cmd_rc": None,
        "status": "running",
        "stop_rc": None,
    }
    cap = min(max_iterations or _ABSOLUTE_CAP, _ABSOLUTE_CAP)
    final = app.invoke(init, config={"recursion_limit": 2 * cap + 10})
    return int(final["stop_rc"])
