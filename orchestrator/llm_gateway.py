"""llm_gateway.py — gateway único a los modelos (LiteLLM SDK).

Cierra el hueco que tenía v1: aquí nace el cliente LLM autónomo in-tree. Toda
llamada a un modelo pasa por `complete()`, que:

  1. Aplica el control de costo/tokens ANTES de llamar: invoca
     budget_enforcer.check_token_budget(state_dir) — el mismo enforcement
     determinista que usa cycle_loop. Si algún SUT excede maxTokensIn, NO se
     llama al modelo (se levanta BudgetExceeded). Así el "control central" del
     gateway (imagen v2) se apoya en la garantía por construcción ya existente.
  2. Resuelve el modelo por rol vía config.model_for_role.
  3. Llama litellm.completion (import perezoso, para que los tests que mockean
     `complete` no necesiten litellm ni credenciales).

LiteLLM centraliza claves/routing/reintentos y aporta cost-tracking nativo.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import config

sys.path.insert(0, str(config.TOOLS_PYTHON))
import budget_enforcer  # noqa: E402  (núcleo determinista — no se reimplementa)


class BudgetExceeded(RuntimeError):
    """Un SUT excede su techo de tokens de entrada; no se debe llamar al modelo."""

    def __init__(self, payload: dict):
        self.payload = payload
        suts = payload.get("overBudgetSuts") or payload.get("reason")
        super().__init__(f"token budget exceeded: {suts}")


def _assert_within_token_budget(state_dir: Path) -> None:
    rc, payload = budget_enforcer.check_token_budget(Path(state_dir))
    if rc != budget_enforcer.EXIT_OK:
        raise BudgetExceeded(payload)


def complete(
    messages: list[dict],
    *,
    role: str,
    state_dir: Path | str,
    **kwargs,
) -> str:
    """Devuelve el texto de la respuesta del proveedor de LLM activo.

    Firma pública ESTABLE (generation/tests/verify dependen de ella). El control
    de costo/tokens se aplica acá, ANTES de despachar a cualquier proveedor, así
    ningún pack sobre presupuesto llega al modelo ni al handoff del IDE.

    El proveedor concreto (IDE en etapa 1, LiteLLM luego) se elige por
    config.llm_provider(); ver orchestrator/providers.py.
    """
    _assert_within_token_budget(Path(state_dir))

    from . import providers  # import perezoso: evita ciclo en import-time

    provider = providers.get_provider()
    return provider.complete(messages, role=role, state_dir=Path(state_dir), **kwargs)
