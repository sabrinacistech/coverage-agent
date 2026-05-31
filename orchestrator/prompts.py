"""prompts.py — los agentes markdown de v1 como prompts del gateway.

Reutiliza los contratos de prompt ya escritos (`agents/test-body-agent.md`,
`agents/repair-agent.md`) como *system prompt*, sin reescribirlos. El *user
message* es el JSON {contextPack, testCase} que esos agentes ya esperan como
entrada (ver test-body-agent.md §Entrada).
"""
from __future__ import annotations

from functools import lru_cache

from . import config

_ROLE_AGENT_FILE = {
    "generation": "test-body-agent.md",
    "repair": "repair-agent.md",
}


@lru_cache(maxsize=None)
def load_system_prompt(role: str) -> str:
    """Texto del agente markdown correspondiente al rol."""
    fname = _ROLE_AGENT_FILE.get(role)
    if fname is None:
        raise ValueError(f"rol sin agente: {role!r} (esperaba {sorted(_ROLE_AGENT_FILE)})")
    path = config.AGENTS_DIR / fname
    return path.read_text(encoding="utf-8")


def build_messages(role: str, payload: dict) -> list[dict]:
    """Mensajes para gateway.complete: system = agente, user = payload JSON."""
    import json

    return [
        {"role": "system", "content": load_system_prompt(role)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
