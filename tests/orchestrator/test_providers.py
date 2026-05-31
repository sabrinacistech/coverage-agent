"""E1.1 — proveedores del gateway: selección y roundtrip del handoff IDE.

Hermético: el IDEProvider no llama a ninguna API; simulamos al IDE escribiendo
el archivo de respuesta desde otro hilo.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from orchestrator import config, llm_gateway, providers


def test_provider_selection(monkeypatch):
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "ide")
    assert isinstance(providers.get_provider(), providers.IDEProvider)
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "litellm")
    assert isinstance(providers.get_provider(), providers.LiteLLMProvider)
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "bogus")
    with pytest.raises(providers.ProviderError):
        providers.get_provider()


def test_ide_provider_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "ide")
    monkeypatch.setenv("COVAGENT_IDE_TIMEOUT", "10")
    monkeypatch.setenv("COVAGENT_IDE_POLL_SECONDS", "0.05")

    result: dict = {}

    def run():
        try:
            result["out"] = llm_gateway.complete(
                [{"role": "user", "content": "hola"}],
                role="generation", state_dir=tmp_path)
        except Exception as exc:  # captura para aserción en el hilo principal
            result["err"] = exc

    t = threading.Thread(target=run)
    t.start()

    ide = config.ide_dir(tmp_path)
    deadline = time.time() + 5
    reqs: list[Path] = []
    while time.time() < deadline:
        reqs = list(ide.glob("request-*.json"))
        if reqs:
            break
        time.sleep(0.05)
    assert reqs, "el proveedor IDE no escribió el request"

    payload = json.loads(reqs[0].read_text(encoding="utf-8"))
    assert payload["role"] == "generation"
    assert payload["messages"][0]["content"] == "hola"  # el prompt llega íntegro al IDE
    resp_path = Path(payload["responsePath"])
    # El "IDE" responde con un contrato BLOCKED válido.
    resp_path.write_text(json.dumps(
        {"schemaVersion": 1, "status": "BLOCKED", "blockReason": "roundtrip"}),
        encoding="utf-8")

    t.join(timeout=5)
    assert not t.is_alive(), "complete() no retornó tras la respuesta del IDE"
    assert "err" not in result, f"complete() falló: {result.get('err')}"

    out = json.loads(result["out"])
    assert out["status"] == "BLOCKED"
    # request/response archivados en _done
    assert list((ide / "_done").glob("request-*.json"))
    assert list((ide / "_done").glob("response-*.json"))


def test_ide_provider_times_out(tmp_path, monkeypatch):
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "ide")
    monkeypatch.setenv("COVAGENT_IDE_TIMEOUT", "0.2")
    monkeypatch.setenv("COVAGENT_IDE_POLL_SECONDS", "0.05")
    with pytest.raises(providers.IDETimeout):
        llm_gateway.complete([{"role": "user", "content": "x"}],
                             role="generation", state_dir=tmp_path)
