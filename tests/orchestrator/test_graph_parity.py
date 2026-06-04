"""E1.2 — el grafo LangGraph para EXACTAMENTE donde para cycle_loop.

Prueba de paridad: con el mismo trabajo determinista (_fakework) y el mismo
estado inicial, el grafo y cycle_loop.run_loop deben devolver el MISMO rc y dejar
el MISMO contador de ciclos, en tres escenarios: DONE, presupuesto, G8 stall.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from orchestrator import config, graph, nodes

sys.path.insert(0, str(config.TOOLS_PYTHON))
import cycle_loop  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import _fakework  # noqa: E402


def _seed(d: Path, *, max_cycles: int) -> None:
    (d / "_summaries").mkdir(parents=True, exist_ok=True)
    (d / "execution-state.json").write_text(json.dumps({
        "schemaVersion": 1, "cycle": 0, "phase": "generation",
        "budget": {"maxCycles": max_cycles, "maxMinutesPerCycle": 1000},
        "consecutiveZeroDeltaCycles": 0, "compileFailRateWindow": [], "checkpoints": [],
    }), encoding="utf-8")


def _final_cycle(d: Path) -> int:
    return int(json.loads((d / "execution-state.json").read_text(encoding="utf-8"))["cycle"])


def _via_cycle_loop(d: Path, plan: list) -> tuple[int, int]:
    (d / "_fakeplan.json").write_text(json.dumps({"steps": plan, "i": 0}), encoding="utf-8")
    cmd = [sys.executable, _fakework.__file__, "--state-dir", str(d)]
    rc = cycle_loop.run_loop(d / "execution-state.json", d, cmd, 7, None)
    return rc, _final_cycle(d)


def _via_graph(d: Path, plan: list, monkeypatch) -> tuple[int, int]:
    (d / "_fakeplan.json").write_text(json.dumps({"steps": plan, "i": 0}), encoding="utf-8")
    monkeypatch.setattr(nodes, "run_cycle_work", lambda sd, repo: _fakework.run(sd))
    rc = graph.run_graph(d, d, done_exit_code=7)
    return rc, _final_cycle(d)


@pytest.mark.parametrize("name,max_cycles,plan,expected_rc", [
    # DONE: el trabajo señala "sin targets" (rc 7) en el primer ciclo, con progreso.
    ("done", 20, [[7, 5, 0]], cycle_loop.RC_DONE),
    # PRESUPUESTO: progreso siempre, pero maxCycles=2 -> corta al 3er tick.
    ("budget", 2, [[0, 5, 0]] * 6, cycle_loop.RC_BUDGET_EXCEEDED),
    # G8 STALL: delta cero repetido -> el gate de convergencia corta.
    ("stall", 20, [[0, 0, 0]] * 10, cycle_loop.RC_CONVERGENCE_STALL),
    # CRASH: one_cycle sale con un código anormal (handoff/patch inválido). NO debe
    # contarse como compile-fail ni disparar G8: ambos drivers paran con RC_CYCLE_ERROR.
    ("crash", 20, [[1, 0, 0]], cycle_loop.RC_CYCLE_ERROR),
])
def test_graph_parity(tmp_path, monkeypatch, name, max_cycles, plan, expected_rc):
    d_cyc = tmp_path / f"{name}-cyc"
    d_grf = tmp_path / f"{name}-grf"
    d_cyc.mkdir()
    d_grf.mkdir()
    _seed(d_cyc, max_cycles=max_cycles)
    _seed(d_grf, max_cycles=max_cycles)

    rc_cyc, cyc_cycle = _via_cycle_loop(d_cyc, plan)
    rc_grf, grf_cycle = _via_graph(d_grf, plan, monkeypatch)

    assert rc_cyc == expected_rc, f"cycle_loop rc inesperado en {name}: {rc_cyc}"
    assert rc_grf == rc_cyc, f"PARIDAD rota en {name}: grafo {rc_grf} vs cycle_loop {rc_cyc}"
    assert grf_cycle == cyc_cycle, (
        f"PARIDAD de ciclos rota en {name}: grafo {grf_cycle} vs cycle_loop {cyc_cycle}")
