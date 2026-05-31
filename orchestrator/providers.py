"""providers.py — proveedores intercambiables del gateway de LLM (E1.1).

El gateway (llm_gateway.complete) despacha a UNO de estos según
config.llm_provider(). La firma pública del gateway no cambia; toda la variación
vive acá.

  - IDEProvider (etapa 1): NO llama a ninguna API. Escribe el prompt a
    state/_llm/request-*.{json,md} y queda bloqueado haciendo polling hasta que
    Claude Code / GitHub Copilot dejen la respuesta (el patch-descriptor) en el
    responsePath. Valida la respuesta y la devuelve. Sin API key.
  - LiteLLMProvider (etapa 2+): llama litellm.completion. Dormido por defecto.

Regla de oro: el proveedor solo PRODUCE texto (un patch-descriptor). No adjudica
gates ni toca disco del repo — de eso se encarga el patcher determinista.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Protocol

from . import config


class ProviderError(RuntimeError):
    """Error del proveedor de LLM (timeout, respuesta ausente, etc.)."""


class IDETimeout(ProviderError):
    """El IDE no dejó la respuesta dentro de COVAGENT_IDE_TIMEOUT."""


class Provider(Protocol):
    def complete(self, messages: list[dict], *, role: str, state_dir, **kwargs) -> str:
        ...


# ── Proveedor IDE (Claude Code / GitHub Copilot) ──────────────────────────────

_INSTRUCTIONS = (
    "# Handoff al IDE (Claude Code / GitHub Copilot)\n\n"
    "Resolvé el pedido de abajo y escribí **solo** el JSON del patch-descriptor "
    "(sin markdown, sin texto extra) en:\n\n    {response_path}\n\n"
    "El JSON debe validar contra `state/_schemas/protocols/patch-descriptor.schema.json` "
    "(un patch, o el contrato BLOCKED). El system prompt y el contexto están en el `.json` "
    "hermano de este archivo.\n\n"
    "Rol: **{role}**  ·  cycle: {cycle}\n"
)


class IDEProvider:
    """Handoff por archivo, bloqueante. Sin API."""

    def complete(self, messages: list[dict], *, role: str, state_dir, **kwargs) -> str:
        from . import generation  # import perezoso: evita ciclo providers↔generation

        ide = config.ide_dir(state_dir)
        done = ide / "_done"
        ide.mkdir(parents=True, exist_ok=True)
        done.mkdir(parents=True, exist_ok=True)

        cycle = _read_cycle(Path(state_dir))
        rid = f"{int(time.time())}-c{cycle}-{role}"
        req_json = ide / f"request-{rid}.json"
        req_md = ide / f"request-{rid}.md"
        resp = ide / f"response-{rid}.json"

        req_json.write_text(json.dumps({
            "role": role,
            "modelHint": config.model_for_role(role),
            "messages": messages,
            "responsePath": str(resp),
            "schema": "state/_schemas/protocols/patch-descriptor.schema.json",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        req_md.write_text(
            _INSTRUCTIONS.format(response_path=resp, role=role, cycle=cycle),
            encoding="utf-8")

        print(f"\n[IDE-HANDOFF] Pedido para el IDE en:\n  {req_md}\n"
              f"  Dejá la respuesta (JSON) en:\n  {resp}\n"
              f"  Esperando hasta {config.ide_timeout():.0f}s...\n",
              flush=True)

        deadline = time.time() + config.ide_timeout()
        poll = config.ide_poll_seconds()
        while time.time() < deadline:
            if resp.exists():
                text = resp.read_text(encoding="utf-8")
                # Valida ANTES de devolver; deja el rastro archivado.
                patch = generation.validate_patch(generation.extract_json(text))
                _archive(done, req_json, req_md, resp)
                return json.dumps(patch, ensure_ascii=False)
            time.sleep(poll)

        raise IDETimeout(
            f"sin respuesta del IDE en {config.ide_timeout():.0f}s para {req_md.name}")


def _read_cycle(state_dir: Path) -> int:
    p = state_dir / "execution-state.json"
    if not p.exists():
        return 0
    try:
        return int((json.loads(p.read_text(encoding="utf-8")) or {}).get("cycle", 0) or 0)
    except Exception:
        return 0


def _archive(done: Path, *paths: Path) -> None:
    for p in paths:
        if p.exists():
            try:
                p.replace(done / p.name)
            except Exception:
                pass


# ── Proveedor LiteLLM (autónomo, dormido en etapa 1) ──────────────────────────

class LiteLLMProvider:
    """Llama litellm.completion. Requiere credenciales del proveedor de modelos."""

    def complete(self, messages: list[dict], *, role: str, state_dir, **kwargs) -> str:
        model = config.model_for_role(role)
        import litellm  # import perezoso: aísla litellm de los tests del IDEProvider

        resp = litellm.completion(
            model=model,
            messages=messages,
            temperature=kwargs.pop("temperature", 0.0),
            max_tokens=kwargs.pop("max_tokens", None),
            **kwargs,
        )
        return resp.choices[0].message.content or ""


# ── Selección ─────────────────────────────────────────────────────────────────

_REGISTRY = {"ide": IDEProvider, "litellm": LiteLLMProvider}


def get_provider() -> Provider:
    name = config.llm_provider()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderError(
            f"COVAGENT_LLM_PROVIDER desconocido: {name!r} (esperaba {sorted(_REGISTRY)})")
    return cls()
