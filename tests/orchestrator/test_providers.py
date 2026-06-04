"""E1.1 — proveedores del gateway: selección y roundtrip del handoff IDE.

Hermético: el IDEProvider no llama a ninguna API; simulamos al IDE escribiendo
el archivo de respuesta desde otro hilo.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types
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
    monkeypatch.setenv("COVAGENT_IDE_INTERACTIVE", "0")  # polling determinista en test
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
    monkeypatch.setenv("COVAGENT_IDE_INTERACTIVE", "0")  # polling determinista en test
    monkeypatch.setenv("COVAGENT_IDE_TIMEOUT", "0.2")
    monkeypatch.setenv("COVAGENT_IDE_POLL_SECONDS", "0.05")
    with pytest.raises(providers.IDETimeout):
        llm_gateway.complete([{"role": "user", "content": "x"}],
                             role="generation", state_dir=tmp_path)


def test_ide_provider_interactive_skip(tmp_path, monkeypatch):
    # ENTER 'skip' desde la terminal → el target se salta (contrato BLOCKED).
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "ide")
    monkeypatch.setenv("COVAGENT_IDE_INTERACTIVE", "1")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "skip")
    out = json.loads(llm_gateway.complete(
        [{"role": "user", "content": "x"}], role="generation", state_dir=tmp_path))
    assert out["status"] == "BLOCKED"


def test_ide_provider_interactive_enter_continues(tmp_path, monkeypatch):
    # El usuario deja la respuesta y presiona ENTER → continúa con ese patch.
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "ide")
    monkeypatch.setenv("COVAGENT_IDE_INTERACTIVE", "1")
    ide = config.ide_dir(tmp_path)

    def fake_input(*a, **k):
        req = list(ide.glob("request-*.json"))[0]
        resp = Path(json.loads(req.read_text(encoding="utf-8"))["responsePath"])
        resp.write_text(json.dumps(
            {"schemaVersion": 1, "status": "BLOCKED", "blockReason": "enter"}), encoding="utf-8")
        return ""  # ENTER

    monkeypatch.setattr("builtins.input", fake_input)
    out = json.loads(llm_gateway.complete(
        [{"role": "user", "content": "x"}], role="generation", state_dir=tmp_path))
    assert out["status"] == "BLOCKED"
    assert list((ide / "_done").glob("response-*.json"))


# ── F3: prompt caching del system prompt en LiteLLMProvider ────────────────────

def test_cache_system_messages_marks_only_system():
    msgs = [
        {"role": "system", "content": "AGENTE LARGO Y ESTABLE"},
        {"role": "user", "content": "context pack que cambia"},
    ]
    out = providers.cache_system_messages(msgs)
    # system → bloque de contenido con cache_control ephemeral
    assert isinstance(out[0]["content"], list)
    block = out[0]["content"][0]
    assert block["type"] == "text"
    assert block["text"] == "AGENTE LARGO Y ESTABLE"
    assert block["cache_control"] == {"type": "ephemeral"}
    # user intacto (no se cachea: cambia en cada llamada)
    assert out[1] == {"role": "user", "content": "context pack que cambia"}
    # no muta el original
    assert msgs[0]["content"] == "AGENTE LARGO Y ESTABLE"


def _install_fake_litellm(monkeypatch) -> dict:
    """Inyecta un `litellm` falso que captura los kwargs de completion()."""
    captured: dict = {}
    fake = types.ModuleType("litellm")

    def completion(**kwargs):
        captured.update(kwargs)
        msg = types.SimpleNamespace(content='{"schemaVersion":1,"status":"BLOCKED","blockReason":"x"}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    fake.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return captured


def test_litellm_provider_applies_caching(tmp_path, monkeypatch):
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "litellm")
    monkeypatch.delenv("COVAGENT_PROMPT_CACHE", raising=False)  # default ON
    captured = _install_fake_litellm(monkeypatch)

    llm_gateway.complete(
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}],
        role="generation", state_dir=tmp_path)

    sys_msg = captured["messages"][0]
    assert isinstance(sys_msg["content"], list)
    assert sys_msg["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_litellm_provider_caching_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "litellm")
    monkeypatch.setenv("COVAGENT_PROMPT_CACHE", "0")  # opt-out
    captured = _install_fake_litellm(monkeypatch)

    llm_gateway.complete(
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}],
        role="generation", state_dir=tmp_path)

    # system queda como string plano: sin cache_control
    assert captured["messages"][0]["content"] == "SYS"
