"""prompts.py — los agentes markdown de v1 como prompts del gateway.

Reutiliza los contratos de prompt ya escritos (`agents/test-body-agent.md`,
`agents/repair-agent.md`) como *system prompt*, sin reescribirlos. El *user
message* es el JSON {contextPack, testCase} que esos agentes ya esperan como
entrada (ver test-body-agent.md §Entrada).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

from . import config

_ROLE_AGENT_FILE = {
    "generation": "test-body-agent.md",
    "repair": "repair-agent.md",
}

# Plantillas .md del prompt de handoff (las que un humano edita en prompts/). El
# runner las completa con las rutas reales del batch vía render_handoff_prompt.
_HANDOFF_TEMPLATE_FILE = {
    "generation": "handoff-generation.md",
    "repair": "handoff-repair.md",
}


@lru_cache(maxsize=None)
def load_system_prompt(role: str) -> str:
    """Texto del agente markdown correspondiente al rol."""
    fname = _ROLE_AGENT_FILE.get(role)
    if fname is None:
        raise ValueError(f"rol sin agente: {role!r} (esperaba {sorted(_ROLE_AGENT_FILE)})")
    path = config.AGENTS_DIR / fname
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _load_handoff_template(kind: str) -> str | None:
    """Texto de la plantilla .md del handoff (kind ∈ {generation, repair}).

    Devuelve None si el archivo no existe o no se puede leer, para que el runner
    use su prompt mínimo embebido como fallback en vez de cortar el run."""
    fname = _HANDOFF_TEMPLATE_FILE.get(kind)
    if fname is None:
        raise ValueError(f"kind sin plantilla: {kind!r} (esperaba {sorted(_HANDOFF_TEMPLATE_FILE)})")
    path = Path(config.PROMPTS_DIR) / fname
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def render_handoff_prompt(
    kind: str,
    *,
    request_path: str,
    response_path: str,
    schema_version: str,
    run_id: str,
    batch_id: str,
    repair_round: int | None = None,
) -> str | None:
    """Completa la plantilla .md del handoff con las rutas reales del batch.

    Usa string.Template.safe_substitute, así que las llaves del JSON de ejemplo en
    la plantilla quedan intactas y un ``${...}`` desconocido no rompe el render.
    Devuelve None si la plantilla no está disponible (el runner usa su fallback)."""
    template = _load_handoff_template(kind)
    if template is None:
        return None
    return Template(template).safe_substitute(
        REQUEST_PATH=request_path,
        RESPONSE_PATH=response_path,
        SCHEMA_VERSION=schema_version,
        RUN_ID=run_id,
        BATCH_ID=batch_id,
        REPAIR_ROUND=str(repair_round if repair_round is not None else 1),
    )


def build_messages(role: str, payload: dict) -> list[dict]:
    """Mensajes para gateway.complete: system = agente, user = payload JSON."""
    import json

    return [
        {"role": "system", "content": load_system_prompt(role)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
