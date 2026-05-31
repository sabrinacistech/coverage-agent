"""main.py — fachada FastAPI del coverage-agent (E1.4).

Pilar "FastAPI" de la imagen Scaffolding, con arranque MANUAL (etapa 1):

    uvicorn app.main:app            # se levanta a mano

Endpoints:
  POST /runs          → arranca un run del grafo en un hilo de fondo; devuelve runId.
  GET  /runs/{id}     → estado del run + cycle + coverage-delta + request del IDE
                        pendiente (la ruta del handoff que Claude Code/Copilot debe
                        resolver). El handoff sigue siendo por archivo (ver
                        orchestrator/providers.py IDEProvider).
  GET  /runs          → lista de runs.

El registro de runs es en memoria (single-user, etapa 1). Si uvicorn reinicia, se
pierde el registro; el estado de cobertura vive en el state-dir y persiste.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from orchestrator import config, graph

app = FastAPI(title="coverage-agent", version="2.0.0a0")

_runs: dict[str, dict] = {}
_lock = threading.Lock()


# ── modelos ───────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    repo: str
    state_dir: str
    done_exit_code: int = 7
    max_iterations: Optional[int] = None


class RunStatus(BaseModel):
    runId: str
    status: str                        # running | done | error
    stopRc: Optional[int] = None
    cycle: Optional[int] = None
    coverageDelta: Optional[dict] = None
    pendingIdeRequest: Optional[str] = None   # ruta del request-*.md a resolver
    error: Optional[str] = None


# ── helpers de lectura de estado ───────────────────────────────────────────────

def _pending_ide_request(state_dir: Path) -> Optional[str]:
    """Último request del IDE sin consumir (los consumidos van a _llm/_done/)."""
    ide = config.ide_dir(state_dir)
    if not ide.exists():
        return None
    reqs = sorted(ide.glob("request-*.md"))
    return str(reqs[-1]) if reqs else None


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cycle(state_dir: Path) -> Optional[int]:
    state = _read_json(state_dir / "execution-state.json")
    return int(state.get("cycle", 0)) if state else None


def _status(run_id: str) -> RunStatus:
    with _lock:
        r = dict(_runs[run_id])
    sd = Path(r["state_dir"])
    return RunStatus(
        runId=run_id,
        status=r["status"],
        stopRc=r.get("stopRc"),
        cycle=_cycle(sd),
        coverageDelta=_read_json(sd / "coverage-delta.json"),
        pendingIdeRequest=_pending_ide_request(sd),
        error=r.get("error"),
    )


# ── ejecución en segundo plano ─────────────────────────────────────────────────

def _run_graph_bg(run_id: str, req: dict) -> None:
    try:
        rc = graph.run_graph(
            req["state_dir"], req["repo"],
            done_exit_code=req["done_exit_code"],
            max_iterations=req["max_iterations"],
        )
        with _lock:
            _runs[run_id].update(status="done", stopRc=rc)
    except Exception as exc:  # noqa: BLE001 — reportar cualquier fallo del run
        with _lock:
            _runs[run_id].update(status="error", error=str(exc))


# ── endpoints ───────────────────────────────────────────────────────────────────

@app.post("/runs", response_model=RunStatus)
def start_run(req: RunRequest) -> RunStatus:
    state_dir = Path(req.state_dir)
    if not state_dir.exists():
        raise HTTPException(status_code=400, detail=f"state_dir no existe: {state_dir}")
    run_id = uuid.uuid4().hex[:12]
    with _lock:
        _runs[run_id] = {
            "status": "running", "stopRc": None, "error": None,
            "state_dir": str(state_dir), "repo": req.repo,
        }
    threading.Thread(target=_run_graph_bg, args=(run_id, req.model_dump()), daemon=True).start()
    return _status(run_id)


@app.get("/runs/{run_id}", response_model=RunStatus)
def get_run(run_id: str) -> RunStatus:
    with _lock:
        known = run_id in _runs
    if not known:
        raise HTTPException(status_code=404, detail="run desconocido")
    return _status(run_id)


@app.get("/runs")
def list_runs() -> dict:
    with _lock:
        ids = list(_runs)
    return {"runs": ids}
