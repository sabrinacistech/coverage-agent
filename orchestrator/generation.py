"""generation.py — fase 8: producir un patch-descriptor validado.

Llama al gateway con el prompt del agente, extrae el JSON de la respuesta y lo
valida contra `state/_schemas/protocols/patch-descriptor.schema.json` ANTES de
que toque el patcher. Una salida que no cumple el schema se rechaza aquí
(PatchSchemaError) — nunca llega a disco.
"""
from __future__ import annotations

import json
from functools import lru_cache

import jsonschema

from . import config, llm_gateway, prompts

_PATCH_SCHEMA_PATH = config.SCHEMAS_DIR / "protocols" / "patch-descriptor.schema.json"


class PatchSchemaError(ValueError):
    """La salida del modelo no es un patch-descriptor válido."""


@lru_cache(maxsize=1)
def _patch_schema() -> dict:
    return json.loads(_PATCH_SCHEMA_PATH.read_text(encoding="utf-8"))


def extract_json(text: str) -> dict:
    """Extrae el objeto JSON de la respuesta del modelo.

    Tolera vallas de código ```json ... ``` o texto accidental alrededor: toma
    desde la primera '{' hasta la última '}'. El agente tiene instruido emitir
    JSON puro, pero no confiamos en ello — validamos después contra el schema.
    """
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.lstrip("`")
        if s.startswith("json"):
            s = s[4:]
    first, last = s.find("{"), s.rfind("}")
    if first == -1 or last == -1 or last < first:
        raise PatchSchemaError("la respuesta del modelo no contiene un objeto JSON")
    try:
        return json.loads(s[first : last + 1])
    except json.JSONDecodeError as exc:
        raise PatchSchemaError(f"JSON inválido en la respuesta del modelo: {exc}") from exc


def validate_patch(obj: dict) -> dict:
    """Valida contra el patch-descriptor schema; devuelve el objeto si pasa."""
    try:
        jsonschema.validate(obj, _patch_schema())
    except jsonschema.ValidationError as exc:
        raise PatchSchemaError(f"patch-descriptor inválido: {exc.message}") from exc
    return obj


def generate_patch(
    *,
    state_dir,
    context_pack: dict,
    test_case: dict,
    role: str = "generation",
) -> dict:
    """Genera y valida un patch-descriptor para un (contextPack, testCase).

    El control de presupuesto de tokens lo aplica el gateway antes de llamar.
    """
    messages = prompts.build_messages(role, {"contextPack": context_pack, "testCase": test_case})
    raw = llm_gateway.complete(messages, role=role, state_dir=state_dir)
    return validate_patch(extract_json(raw))
