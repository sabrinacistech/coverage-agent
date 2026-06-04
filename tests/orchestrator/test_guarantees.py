"""Regresión: la capa de orquestación v2 NO erosiona las garantías por
construcción del núcleo determinista.

Hermético: sin LLM real, sin Maven/Java. Cada test fija un punto donde el
sistema debe bloquear y verifica que sigue bloqueando.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from orchestrator import generation, llm_gateway, one_cycle


# ── helpers ───────────────────────────────────────────────────────────────────

def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _fake_completion(content: str):
    def _impl(*args, **kwargs):
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    return _impl


# ── G-coste/tokens: el gateway bloquea ANTES de llamar al modelo ──────────────

def test_gateway_blocks_over_budget_before_calling_model(tmp_path, monkeypatch):
    # llm-budget.json marca un SUT sobre techo de tokens.
    _write(tmp_path / "_summaries" / "llm-budget.json", {
        "schemaVersion": 2,
        "entries": [{"sut": "com.example.Foo", "estimatedTokensIn": 99999,
                     "maxTokensIn": 1000, "overBudget": True}],
    })

    import litellm
    called = {"n": 0}
    monkeypatch.setattr(litellm, "completion",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    with pytest.raises(llm_gateway.BudgetExceeded):
        llm_gateway.complete([{"role": "user", "content": "hi"}],
                             role="generation", state_dir=tmp_path)
    assert called["n"] == 0, "el modelo NO debe llamarse cuando un SUT excede el presupuesto"


def test_gateway_calls_model_when_within_budget(tmp_path, monkeypatch):
    # Sin llm-budget.json → check_token_budget devuelve OK (nada que aplicar).
    # Probamos explícitamente el proveedor litellm (el default de etapa 1 es 'ide').
    monkeypatch.setenv("COVAGENT_LLM_PROVIDER", "litellm")
    import litellm
    monkeypatch.setattr(litellm, "completion", _fake_completion("RESPUESTA"))
    out = llm_gateway.complete([{"role": "user", "content": "hi"}],
                               role="generation", state_dir=tmp_path)
    assert out == "RESPUESTA"


# ── schema: salida del modelo que no cumple patch-descriptor → rechazada ──────

def test_generation_rejects_schema_invalid_output(tmp_path, monkeypatch):
    # Falta 'sut'/'testClass' requeridos → PatchSchemaError, nunca toca disco.
    monkeypatch.setattr(llm_gateway, "complete",
                        lambda *a, **k: '{"schemaVersion": 1, "patchId": "patch:abc123"}')
    with pytest.raises(generation.PatchSchemaError):
        generation.generate_patch(state_dir=tmp_path, context_pack={"sut": "X"}, test_case={})


def test_generation_extracts_json_from_code_fence(tmp_path, monkeypatch):
    valid = {"schemaVersion": 1, "status": "BLOCKED", "blockReason": "sin método"}
    monkeypatch.setattr(llm_gateway, "complete",
                        lambda *a, **k: "```json\n" + json.dumps(valid) + "\n```")
    out = generation.generate_patch(state_dir=tmp_path, context_pack={"sut": "X"}, test_case={})
    assert out["status"] == "BLOCKED"


# ── perímetro/gate: un import fuera del whitelist NO se escribe (G1) ──────────

def test_apply_patch_blocks_import_outside_perimeter(tmp_path):
    """one_cycle.apply_patch delega en test_patch_applier; un import fuera del
    context-pack debe bloquear (rc 3) ANTES de escribir Java. Reusa los gates
    reales del núcleo — prueba que la frontera anti-alucinación sigue viva."""
    sut = "com.example.Foo"
    pack_path = tmp_path / "context-packs" / f"{sut}.json"
    _write(pack_path, {"sut": sut, "allowedImports": ["org.junit.Test"]})

    patch = {
        "schemaVersion": 1,
        "patchId": "patch:abc123",
        "sut": sut,
        "testClass": "com.example.FooTest",
        "allowedImports": ["com.evil.Backdoor"],  # NO está en el context-pack
    }
    rc = one_cycle.apply_patch(
        patch, state_dir=tmp_path, repo=tmp_path, context_pack_path=pack_path,
    )
    assert rc == 3, "un import fuera del perímetro debe bloquearse con rc=3"


def test_run_one_cycle_blocks_when_compact_pack_missing(tmp_path, monkeypatch):
    """F4: si falta el compact-pack del SUT, el ciclo BLOQUEA el target y NUNCA
    invoca al modelo (no degrada al pack completo → sin blowup de tokens)."""
    sut = "com.example.Foo"
    _write(tmp_path / "batch-plan.json", {
        "schemaVersion": 1, "cycle": 1, "mode": "coverage", "sizeChosen": 1,
        "items": [{"targetId": "t1", "sut": sut, "method": "foo()"}],
    })
    # El pack COMPLETO existe; el COMPACTO (lo que va al modelo) NO.
    _write(tmp_path / "context-packs" / f"{sut}.json", {"sut": sut, "allowedImports": []})

    called = {"n": 0}
    monkeypatch.setattr(one_cycle.generation, "generate_patch",
                        lambda **k: called.__setitem__("n", called["n"] + 1))

    rc = one_cycle.run_one_cycle(tmp_path, tmp_path)
    assert rc == one_cycle.RC_OK
    assert called["n"] == 0, "generación NO debe invocarse sin compact-pack"
    # El target quedó procesado → el loop no se queda girando sobre él.
    assert one_cycle.select_next_target(tmp_path.resolve()) is None


def test_select_next_target_skips_processed(tmp_path):
    _write(tmp_path / "batch-plan.json", {
        "schemaVersion": 1, "cycle": 1, "mode": "coverage", "sizeChosen": 2,
        "items": [
            {"targetId": "t1", "sut": "com.example.A", "method": "a()"},
            {"targetId": "t2", "sut": "com.example.B", "method": "b()"},
        ],
    })
    assert one_cycle.select_next_target(tmp_path)["targetId"] == "t1"
    one_cycle.mark_processed(tmp_path, "t1")
    assert one_cycle.select_next_target(tmp_path)["targetId"] == "t2"
    one_cycle.mark_processed(tmp_path, "t2")
    assert one_cycle.select_next_target(tmp_path) is None
