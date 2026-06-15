"""config.py — única fuente de configuración de la capa de orquestación v2.

Routing de modelos por rol y rutas del proyecto. Lee variables de entorno
(opcionalmente desde un .env vía python-dotenv). No contiene secretos: las
claves de API las consume LiteLLM directamente del entorno.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # carga .env si existe; no es fatal si python-dotenv no está disponible
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv es opcional
    pass

# ── Rutas del proyecto ────────────────────────────────────────────────────────
# La raíz del repo es el padre del paquete orchestrator/. El núcleo determinista
# vive en tools/python y se invoca por ruta (subprocess / sys.path), no como
# paquete instalado.
ARCH_ROOT = Path(__file__).resolve().parents[1]
TOOLS_PYTHON = ARCH_ROOT / "tools" / "python"
AGENTS_DIR = ARCH_ROOT / "agents"
SCHEMAS_DIR = ARCH_ROOT / "state" / "_schemas"

# ── Routing de modelos por rol ────────────────────────────────────────────────
# Formato LiteLLM "<provider>/<model>". Se sobreescriben por entorno para no
# acoplar el código a un proveedor concreto.
_DEFAULT_MODELS = {
    "generation": "anthropic/claude-opus-4-8",
    "repair": "anthropic/claude-sonnet-4-6",
    "architecture": "anthropic/claude-sonnet-4-6",
}
_ENV_BY_ROLE = {
    "generation": "COVAGENT_MODEL_GENERATION",
    "repair": "COVAGENT_MODEL_REPAIR",
    "architecture": "COVAGENT_MODEL_ARCHITECTURE",
}


def model_for_role(role: str) -> str:
    """Modelo LiteLLM para un rol ('generation' | 'repair')."""
    if role not in _DEFAULT_MODELS:
        raise ValueError(f"rol desconocido: {role!r} (esperaba {sorted(_DEFAULT_MODELS)})")
    env_var = _ENV_BY_ROLE[role]
    return os.environ.get(env_var) or _DEFAULT_MODELS[role]


def langfuse_enabled() -> bool:
    """True solo si LANGFUSE_ENABLED=1 (M4). Por defecto todo es no-op."""
    return os.environ.get("LANGFUSE_ENABLED", "0") == "1"


def prompt_caching_enabled() -> bool:
    """Prompt caching del *system prompt* en LiteLLMProvider (F3).

    El system prompt (el agente markdown: ~1.5K–3K tokens) es estable entre
    llamadas del mismo rol; marcarlo con `cache_control` evita re-facturarlo en
    cada generación/repair. Default ON; `COVAGENT_PROMPT_CACHE=0` lo desactiva.
    """
    return (os.environ.get("COVAGENT_PROMPT_CACHE") or "1").strip() != "0"


# ── Proveedor de LLM (E1.1) ───────────────────────────────────────────────────
# El gateway despacha al proveedor activo. En etapa 1 el default es `ide`: el LLM
# lo pone Claude Code / GitHub Copilot vía handoff por archivo (sin API key). El
# camino autónomo `litellm` queda dormido hasta una etapa posterior.
_DEFAULT_PROVIDER = "ide"


def llm_provider() -> str:
    """Proveedor activo: 'ide' (handoff a Claude Code/Copilot) | 'litellm' (API)."""
    return (os.environ.get("COVAGENT_LLM_PROVIDER") or _DEFAULT_PROVIDER).strip().lower()


# ── Modo de generación + batching (handoff incremental) ───────────────────────
# El runner trabaja en uno de tres modos. El default es handoff-single para no
# cambiar el comportamiento histórico (un target por handoff); handoff-batch es el
# recomendado para proyectos grandes (hasta batchSize tests por tirada). En ambos
# modos de handoff el budget de minutos se PAUSA mientras se espera a Claude Code.
GENERATION_MODES = ("handoff-single", "handoff-batch", "auto")
_DEFAULT_GENERATION_MODE = "handoff-single"
_DEFAULT_BATCH_SIZE = 10
_DEFAULT_MAX_REPAIR_ROUNDS = 2


def generation_mode() -> str:
    """Modo de generación activo. COVAGENT_GENERATION_MODE lo fuerza.

    'handoff-single' (default, compat) | 'handoff-batch' (recomendado) | 'auto'.
    Un valor desconocido cae al default en vez de romper (el CLI valida choices)."""
    v = (os.environ.get("COVAGENT_GENERATION_MODE") or _DEFAULT_GENERATION_MODE).strip().lower()
    return v if v in GENERATION_MODES else _DEFAULT_GENERATION_MODE


def batch_size() -> int:
    """Cantidad máxima de targets por batch (handoff-batch). Default 10.
    COVAGENT_BATCH_SIZE lo fuerza; se acota a [1, 50]."""
    try:
        n = int(os.environ.get("COVAGENT_BATCH_SIZE") or _DEFAULT_BATCH_SIZE)
    except ValueError:
        n = _DEFAULT_BATCH_SIZE
    return max(1, min(50, n))


def max_repair_rounds() -> int:
    """Rondas de reparación por batch antes de ABANDONAR un target. Default 2.
    COVAGENT_MAX_REPAIR_ROUNDS lo fuerza; se acota a [0, 10]."""
    try:
        n = int(os.environ.get("COVAGENT_MAX_REPAIR_ROUNDS") or _DEFAULT_MAX_REPAIR_ROUNDS)
    except ValueError:
        n = _DEFAULT_MAX_REPAIR_ROUNDS
    return max(0, min(10, n))


def ide_dir(state_dir) -> Path:
    """Carpeta del handoff IDE. Default <state>/_llm; override COVAGENT_IDE_DIR."""
    override = os.environ.get("COVAGENT_IDE_DIR")
    return Path(override) if override else Path(state_dir) / "_llm"


def ide_timeout() -> float:
    """Segundos máximos que el proveedor IDE espera la respuesta del IDE."""
    return float(os.environ.get("COVAGENT_IDE_TIMEOUT") or "1800")


def ide_poll_seconds() -> float:
    """Intervalo de polling del archivo de respuesta (testeable)."""
    return float(os.environ.get("COVAGENT_IDE_POLL_SECONDS") or "2")


def ide_interactive() -> bool:
    """Handoff interactivo (el usuario presiona ENTER en la terminal para
    continuar) vs polling silencioso (API/background). COVAGENT_IDE_INTERACTIVE
    fuerza 1/0; por defecto se autodetecta según si stdin es una TTY."""
    ov = os.environ.get("COVAGENT_IDE_INTERACTIVE")
    if ov is not None:
        return ov.strip() == "1"
    try:
        import sys
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False
