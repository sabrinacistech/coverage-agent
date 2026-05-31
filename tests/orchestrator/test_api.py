"""E1.4 — fachada FastAPI. Hermético: sin Java ni LLM.

El ciclo de vida se prueba con un state-dir sin batch-plan: el grafo arranca,
el trabajo devuelve "sin targets" (rc 7 = DONE) y el run termina rc 0.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import _pending_ide_request, app
from orchestrator import config

client = TestClient(app)


def _wait_done(run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    s = client.get(f"/runs/{run_id}").json()
    while s["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
        s = client.get(f"/runs/{run_id}").json()
    return s


def test_run_lifecycle_no_targets(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    r = client.post("/runs", json={"repo": str(tmp_path), "state_dir": str(state)})
    assert r.status_code == 200, r.text
    run_id = r.json()["runId"]

    s = _wait_done(run_id)
    assert s["status"] == "done", s
    assert s["stopRc"] == 0  # sin targets -> DONE


def test_start_run_rejects_missing_state_dir(tmp_path):
    r = client.post("/runs", json={"repo": str(tmp_path), "state_dir": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_get_unknown_run_is_404():
    assert client.get("/runs/deadbeef").status_code == 404


def test_pending_ide_request_helper(tmp_path):
    ide = config.ide_dir(tmp_path)
    ide.mkdir(parents=True)
    assert _pending_ide_request(tmp_path) is None
    (ide / "request-1-c1-generation.md").write_text("x", encoding="utf-8")
    found = _pending_ide_request(tmp_path)
    assert found is not None and found.endswith("request-1-c1-generation.md")
