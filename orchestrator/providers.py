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

import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Protocol

from . import config

_SKIP_PATCH = {"schemaVersion": 1, "status": "BLOCKED", "blockReason": "skipped by user from terminal"}


@contextlib.contextmanager
def _budget_paused(state_dir, reason: str):
    """Pause the per-cycle minute budget around a MANUAL handoff wait.

    The minute budget must measure the runner's automatic work, never the time a
    human spends asking Claude Code to generate the patch and pressing ENTER.
    Without this, the interactive handoff blocks inside a cycle whose
    cycleStartedAt is already stamped, so the test_patch_applier backstop trips
    BUDGET_EXCEEDED while the user is still thinking (the exact bug being fixed).

    budget_enforcer lives in tools/python (the deterministic core, invoked by
    path), so it is imported lazily here. If it cannot be imported (e.g. an
    orchestrator-only unit test), this degrades to a no-op that still prints the
    handoff markers — the wait simply is not budget-aware, never an error.
    """
    state_path = Path(state_dir) / "execution-state.json"
    be = None
    try:
        if str(config.TOOLS_PYTHON) not in sys.path:
            sys.path.insert(0, str(config.TOOLS_PYTHON))
        import budget_enforcer as be  # noqa: F811
    except Exception:  # pragma: no cover - core not importable in some unit tests
        be = None

    if be is None or not state_path.exists():
        print(f"[budget] paused: {reason}", flush=True)
        try:
            yield
        finally:
            print("[budget] resumed", flush=True)
        return

    with be.paused(state_path, reason):
        yield


class ProviderError(RuntimeError):
    """Error del proveedor de LLM (timeout, respuesta ausente, etc.)."""


class IDETimeout(ProviderError):
    """El IDE no dejó la respuesta dentro de COVAGENT_IDE_TIMEOUT."""


class Provider(Protocol):
    def complete(self, messages: list[dict], *, role: str, state_dir, **kwargs) -> str:
        ...


# ── Proveedor IDE (Claude Code / GitHub Copilot) ──────────────────────────────

def _banner(req_json: Path, resp: Path, role: str, cycle: int) -> str:
    return (
        "\n" + "=" * 70 + "\n"
        f"[HANDOFF] Falta generar UN test (rol={role}, cycle={cycle}).\n"
        "  El test lo genera Claude Code — vos NO escribís JSON.\n\n"
        "  1) En el chat de Claude Code (VS Code) pedile:\n"
        f"       \"Resolvé el handoff: leé\n          {req_json}\n"
        f"        y escribí el patch-descriptor en\n          {resp}\"\n"
        "  2) Cuando Claude Code deje ese archivo, volvé acá y presioná ENTER.\n"
        + "=" * 70
    )


class IDEProvider:
    """Handoff por archivo, manejado por el USUARIO desde la terminal.

    Interactivo (TTY): muestra instrucciones y espera que el usuario presione
    ENTER cuando dejó la respuesta (o 'skip' para saltar el target). No se congela
    en silencio. No-interactivo (API/background): polling con latido + timeout.
    """

    def complete(self, messages: list[dict], *, role: str, state_dir, **kwargs) -> str:
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
            "# Handoff al IDE (Claude Code / GitHub Copilot)\n\n"
            f"Resolvé el pedido y escribí **solo** el JSON del patch-descriptor en:\n\n    {resp}\n\n"
            "Debe validar contra `state/_schemas/protocols/patch-descriptor.schema.json` "
            "(un patch, o el contrato BLOCKED). El system prompt y el contexto están en el "
            f"`.json` hermano.\n\nRol: **{role}**  ·  cycle: {cycle}\n",
            encoding="utf-8")

        print(_banner(req_json, resp, role, cycle), flush=True)

        # The wait below is MANUAL handoff time (Claude Code generating the JSON,
        # the user pressing ENTER). Pause the per-cycle minute budget so only the
        # runner's automatic work is charged against maxMinutesPerCycle.
        with _budget_paused(state_dir, "waiting for manual Claude Code handoff"):
            print(f"[handoff] waiting for response JSON: {resp.name}", flush=True)
            if config.ide_interactive():
                return self._await_interactive(req_json, req_md, resp, done)
            return self._await_polling(req_json, req_md, resp, done)

    # — modo interactivo: el usuario maneja cada paso desde la terminal —
    def _await_interactive(self, req_json, req_md, resp, done) -> str:
        while True:
            try:
                ans = input(
                    "[handoff] ENTER = Claude Code ya dejó la respuesta · 'skip' = "
                    "saltar este target · Ctrl+C = cortar todo > "
                ).strip().lower()
            except EOFError:
                # sin stdin real → caer a polling para no romper
                return self._await_polling(req_json, req_md, resp, done)

            if ans in ("skip", "s"):
                print("[handoff] target saltado por el usuario.", flush=True)
                return json.dumps(_SKIP_PATCH, ensure_ascii=False)

            if not resp.exists():
                print(f"[handoff] No encuentro la respuesta en:\n  {resp}\n"
                      "  Creala (el JSON del patch) y volvé a presionar ENTER.", flush=True)
                continue
            ok, out = self._try_consume(resp, req_json, req_md, done)
            if ok:
                return out
            print(f"[handoff] La respuesta no es un patch válido: {out}\n"
                  "  Corregí el JSON y presioná ENTER de nuevo.", flush=True)

    # — modo no-interactivo (API/background): polling con latido + timeout —
    def _await_polling(self, req_json, req_md, resp, done) -> str:
        timeout = config.ide_timeout()
        poll = config.ide_poll_seconds()
        print(f"[handoff] (no-interactivo) esperando {resp.name} hasta {timeout:.0f}s "
              "(Ctrl+C para cortar)...", flush=True)
        deadline = time.time() + timeout
        last_hb = time.time()
        while time.time() < deadline:
            if resp.exists():
                ok, out = self._try_consume(resp, req_json, req_md, done)
                if ok:
                    return out
                raise ProviderError(f"respuesta inválida del IDE: {out}")
            if time.time() - last_hb >= 30:
                print(f"[handoff] sigo esperando {resp.name}... (Ctrl+C para cortar)", flush=True)
                last_hb = time.time()
            time.sleep(poll)
        raise IDETimeout(f"sin respuesta del IDE en {timeout:.0f}s para {req_md.name}")

    @staticmethod
    def _try_consume(resp: Path, req_json: Path, req_md: Path, done: Path) -> tuple[bool, str]:
        from . import generation  # import perezoso: evita ciclo providers↔generation
        try:
            patch = generation.validate_patch(generation.extract_json(resp.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 — devolver el error para re-prompt
            return False, str(exc)
        _archive(done, req_json, req_md, resp)
        return True, json.dumps(patch, ensure_ascii=False)


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


# ── Prompt caching (F3) ────────────────────────────────────────────────────────

def cache_system_messages(messages: list[dict]) -> list[dict]:
    """Marca cada *system message* de texto con `cache_control: ephemeral`.

    Pura y sin dependencias de litellm (testeable en aislamiento). Convierte
    ``{"role":"system","content":"<str>"}`` en la forma de bloques de contenido
    que la API soporta para cachear, dejando intactos los demás mensajes (el
    *user message* lleva el context-pack, que cambia en cada llamada y NO se
    cachea). Idempotente: un system message que ya viene como lista se respeta.
    """
    out: list[dict] = []
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            out.append({
                "role": "system",
                "content": [{
                    "type": "text",
                    "text": m["content"],
                    "cache_control": {"type": "ephemeral"},
                }],
            })
        else:
            out.append(m)
    return out


def _supports_prompt_caching(model: str) -> bool:
    """¿El modelo soporta prompt caching?

    Todos los modelos Claude actuales lo soportan, así que la heurística por nombre
    tiene precedencia sobre el mapa de capacidades de LiteLLM (que puede estar
    desactualizado y no reconocer nombres nuevos como `claude-opus-4-8`). Para
    otros proveedores se consulta a LiteLLM. `cache_control` es inocuo si el
    proveedor no lo soporta (LiteLLM lo descarta), así que ser permisivo no rompe.
    """
    m = model.lower()
    if "claude" in m or "anthropic" in m:
        return True
    try:
        from litellm.utils import supports_prompt_caching as _spc

        return bool(_spc(model=model))
    except Exception:
        return False


# ── Proveedor LiteLLM (autónomo, dormido en etapa 1) ──────────────────────────

class LiteLLMProvider:
    """Llama litellm.completion. Requiere credenciales del proveedor de modelos.

    Aplica prompt caching del system prompt (F3) cuando el modelo lo soporta y
    `COVAGENT_PROMPT_CACHE != 0`: el agente markdown se cachea una vez y los
    ciclos siguientes del mismo rol no re-facturan esos tokens de entrada.
    """

    def complete(self, messages: list[dict], *, role: str, state_dir, **kwargs) -> str:
        model = config.model_for_role(role)
        import litellm  # import perezoso: aísla litellm de los tests del IDEProvider

        if config.prompt_caching_enabled() and _supports_prompt_caching(model):
            messages = cache_system_messages(messages)

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
