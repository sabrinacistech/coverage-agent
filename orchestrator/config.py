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
}
_ENV_BY_ROLE = {
    "generation": "COVAGENT_MODEL_GENERATION",
    "repair": "COVAGENT_MODEL_REPAIR",
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
