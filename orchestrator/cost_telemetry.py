"""cost_telemetry.py — FinOps: tokens + costo USD por interacción con el LLM.

Acumula, por run, un `costs-telemetry.json` con el desglose por ítem y por ronda
de reparación, más los totales del run. Pensado para dos rutas:

  * API real (LiteLLMProvider): se interceptan los `usage` exactos del payload de
    respuesta (input/output tokens) → costo MEDIDO.
  * Handoff por archivo (batch_runner, ruta activa): el LLM corre fuera de este
    proceso (Claude Code escribe el JSON), así que no hay payload HTTP. Si la
    respuesta trae un bloque `usage`, se usa (medido); si no, se ESTIMA por tamaño
    del payload (~4 chars/token) y la interacción queda marcada `estimated: true`
    / `source: "size_estimate"`. Nunca se presenta una estimación como medición.

Precios configurables (USD por 1M de tokens) por modelo, con override por entorno:
    COVAGENT_PRICE_<KEY>_IN / COVAGENT_PRICE_<KEY>_OUT   (ej. COVAGENT_PRICE_OPUS_IN)

Escritura atómica (tmp + rename) para que un lector concurrente nunca vea un JSON
a medio escribir. Las funciones de IO son tolerantes; aun así, los llamadores en
el orquestador envuelven en try/except para que la telemetría jamás rompa el run.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TELEMETRY_FILENAME = "costs-telemetry.json"

# USD por 1.000.000 de tokens: (input, output). La clave se busca como substring
# del id de modelo resuelto (se ignora el prefijo "<provider>/"). Las claves más
# largas tienen prioridad (gpt-4o-mini antes que gpt-4o). Tarifas de lista
# vigentes (Anthropic / OpenAI) — ajustables por entorno sin tocar el código.
_PRICING: dict[str, tuple[float, float]] = {
    "opus":         (15.00, 75.00),   # Claude Opus (4.x / 3)
    "sonnet":       (3.00, 15.00),    # Claude Sonnet
    "haiku":        (0.80, 4.00),     # Claude Haiku
    "gpt-4o-mini":  (0.15, 0.60),
    "gpt-4o":       (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1":      (2.00, 8.00),
    "o3":           (2.00, 8.00),
}
# Modelo desconocido → tarifa conservadora (la más cara) para no SUB-estimar costo.
_FALLBACK = (15.00, 75.00)

_CHARS_PER_TOKEN = 4  # heurística estándar para texto en/es (~4 bytes/token).


# ── Pricing ──────────────────────────────────────────────────────────────────────

def _env_override(key: str) -> tuple[float, float] | None:
    norm = re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")
    lo = os.environ.get(f"COVAGENT_PRICE_{norm}_IN")
    hi = os.environ.get(f"COVAGENT_PRICE_{norm}_OUT")
    if lo is None and hi is None:
        return None
    base_in, base_out = _PRICING.get(key, _FALLBACK)
    try:
        return (float(lo) if lo is not None else base_in,
                float(hi) if hi is not None else base_out)
    except ValueError:
        return None


def price_for_model(model: str | None) -> tuple[float, float]:
    """(USD/Mtok input, USD/Mtok output) para *model*. Tolerante a None/desconocido."""
    m = (model or "").lower()
    # Clave más específica primero (longitud desc) para evitar que "gpt-4o" gane
    # sobre "gpt-4o-mini".
    for key in sorted(_PRICING, key=len, reverse=True):
        if key in m:
            return _env_override(key) or _PRICING[key]
    # Sin match: permití override genérico bajo la clave del fallback ("opus").
    return _env_override("opus") or _FALLBACK


def compute_cost_usd(model: str | None, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = price_for_model(model)
    cost = (tokens_in / 1_000_000.0) * p_in + (tokens_out / 1_000_000.0) * p_out
    return round(cost, 6)


# ── Usage / token estimation ───────────────────────────────────────────────────

def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def extract_usage(obj: Any) -> tuple[int, int] | None:
    """(input_tokens, output_tokens) desde un payload de respuesta, o None.

    Acepta tanto un dict (respuesta JSON del handoff, con `usage` o claves al tope)
    como un objeto estilo SDK (litellm/openai `resp.usage`). Reconoce los dos
    vocabularios: input_tokens/output_tokens (Anthropic) y
    prompt_tokens/completion_tokens (OpenAI)."""
    if obj is None:
        return None
    src = obj
    # dict: puede venir anidado en "usage".
    if isinstance(obj, dict):
        src = obj.get("usage", obj)

    def _get(name: str):
        if isinstance(src, dict):
            return src.get(name)
        return getattr(src, name, None)

    tin = _as_int(_get("input_tokens"))
    if tin is None:
        tin = _as_int(_get("prompt_tokens"))
    tout = _as_int(_get("output_tokens"))
    if tout is None:
        tout = _as_int(_get("completion_tokens"))
    if tin is None and tout is None:
        return None
    return (tin or 0, tout or 0)


def estimate_tokens(obj: Any) -> int:
    """Estimación de tokens por tamaño de payload (~4 chars/token), mínimo 1."""
    if obj is None:
        return 0
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


# ── Persistencia atómica del costs-telemetry.json ────────────────────────────────

def _base(run_id: str) -> dict:
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "total_accumulated_usd": 0.0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_duration_seconds": 0.0,
        "interactions": [],
    }


def _load(path: Path, run_id: str) -> dict:
    if not path.exists():
        return _base(run_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "interactions" not in data:
            return _base(run_id)
        return data
    except (OSError, json.JSONDecodeError):
        return _base(run_id)


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def telemetry_path(run_dir: str | os.PathLike) -> Path:
    return Path(run_dir) / TELEMETRY_FILENAME


def record_interaction(
    run_dir: str | os.PathLike,
    *,
    run_id: str,
    target_id: str | None,
    role: str,
    rnd: int,
    tokens_in: int,
    tokens_out: int,
    duration_seconds: float,
    model: str | None,
    source: str = "api_usage",
    estimated: bool = False,
) -> dict:
    """Agrega una interacción al costs-telemetry.json del run y reacumula totales.

    ``rnd`` es la ronda (0 = generación, 1.. = reparación); se persiste como
    ``round`` en el JSON. Devuelve el objeto interacción recién agregado."""
    path = telemetry_path(run_dir)
    state = _load(path, run_id)

    cost = compute_cost_usd(model, tokens_in, tokens_out)
    interaction = {
        "targetId": target_id,
        "role": role,
        "round": int(rnd),
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cost_usd": cost,
        "duration_seconds": round_f(duration_seconds),
        "model": model,
        "source": source,
        "estimated": bool(estimated),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    state["interactions"].append(interaction)
    state["total_prompt_tokens"] = int(state.get("total_prompt_tokens", 0)) + int(tokens_in)
    state["total_completion_tokens"] = int(state.get("total_completion_tokens", 0)) + int(tokens_out)
    state["total_accumulated_usd"] = round(float(state.get("total_accumulated_usd", 0.0)) + cost, 6)
    state["total_duration_seconds"] = round_f(
        float(state.get("total_duration_seconds", 0.0)) + max(0.0, duration_seconds))
    state["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_write(path, state)
    return interaction


def round_f(x: float) -> float:
    """Segundos con precisión de milisegundos."""
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return 0.0


# ── Atribución por target en el handoff por lote ─────────────────────────────────

def _request_slices(request: dict) -> dict[Any, dict]:
    """targetId → su porción del request (targets[] en generación, failedItems[] en repair)."""
    items = request.get("targets")
    if not isinstance(items, list):
        items = request.get("failedItems")
    out: dict[Any, dict] = {}
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                out[it.get("targetId")] = it
    return out


def _response_items(response: dict) -> dict[Any, dict]:
    out: dict[Any, dict] = {}
    items = response.get("targets") if isinstance(response, dict) else None
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                out[it.get("targetId")] = it
    return out


def _distribute(total: int, weights: dict[Any, int], target_ids: list) -> dict[Any, int]:
    """Reparte *total* entre target_ids proporcional a *weights* (entero, suma exacta)."""
    if not target_ids:
        return {}
    wsum = sum(max(0, weights.get(t, 0)) for t in target_ids)
    out: dict[Any, int] = {}
    running = 0
    for i, t in enumerate(target_ids):
        if i == len(target_ids) - 1:
            out[t] = max(0, total - running)  # el último absorbe el redondeo
        else:
            frac = (weights.get(t, 0) / wsum) if wsum > 0 else (1.0 / len(target_ids))
            out[t] = int(round(total * frac))
            running += out[t]
    return out


def record_handoff(
    run_dir: str | os.PathLike,
    *,
    run_id: str,
    role: str,
    rnd: int,
    request: dict,
    response: dict,
    target_ids: list,
    duration_seconds: float,
    model: str | None,
) -> list[dict]:
    """Contabiliza UN handoff por lote: una interacción por target.

    Los totales (tokens in/out) son MEDIDOS si la respuesta trae `usage`; si no, se
    estiman por tamaño del payload completo (request → input, response → output) y
    cada interacción queda marcada `estimated: true`. El total se reparte por target
    en proporción al tamaño de su porción del request/response. La duración se
    reparte en partes iguales (suma exacta al wall-clock del handoff)."""
    target_ids = [t for t in (target_ids or [])]
    if not target_ids:
        return []

    req_slices = _request_slices(request)
    resp_items = _response_items(response)
    in_weights = {t: estimate_tokens(req_slices.get(t)) for t in target_ids}
    out_weights = {t: estimate_tokens(resp_items.get(t)) for t in target_ids}

    usage = extract_usage(response)
    if usage is not None:
        total_in, total_out = usage
        source, estimated = "api_usage", False
    else:
        total_in = estimate_tokens(request)    # incluye el overhead compartido (reglas, envelope)
        total_out = estimate_tokens(response)
        source, estimated = "size_estimate", True

    in_by = _distribute(total_in, in_weights, target_ids)
    out_by = _distribute(total_out, out_weights, target_ids)
    per_dur = round_f(max(0.0, duration_seconds) / len(target_ids))

    recorded: list[dict] = []
    for t in target_ids:
        recorded.append(record_interaction(
            run_dir, run_id=run_id, target_id=t, role=role, rnd=rnd,
            tokens_in=in_by.get(t, 0), tokens_out=out_by.get(t, 0),
            duration_seconds=per_dur, model=model,
            source=source, estimated=estimated,
        ))
    return recorded


# ── Reporte de tokens para STDOUT (cuadro tipo "métrica de eficiencia") ──────────

def _box(title: str, rows: list[str]) -> str:
    """Caja ASCII de ancho fijo (mismo estilo que workspace_volumetry)."""
    inner = max([len(title)] + [len(r) for r in rows]) if rows else len(title)
    bar = "+" + "-" * (inner + 2) + "+"
    out = [bar, f"| {title.ljust(inner)} |", bar]
    out += [f"| {r.ljust(inner)} |" for r in rows]
    out.append(bar)
    return "\n".join(out)


def aggregate_by_role(interactions: list) -> dict[str, dict]:
    """Suma tokens/costo/conteo por rol (generation/repair) desde las interacciones."""
    agg: dict[str, dict] = {}
    for it in interactions or []:
        if not isinstance(it, dict):
            continue
        role = str(it.get("role") or "?")
        a = agg.setdefault(role, {"in": 0, "out": 0, "usd": 0.0, "n": 0})
        a["in"] += _as_int(it.get("tokens_in")) or 0
        a["out"] += _as_int(it.get("tokens_out")) or 0
        try:
            a["usd"] += float(it.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        a["n"] += 1
    return agg


def format_token_summary_table(telemetry: dict) -> str:
    """Cuadro ASCII con el desglose de tokens IN/OUT del run (lee un costs-telemetry
    ya cargado). Tolerante: un dict vacío produce un cuadro con ceros."""
    tele = telemetry if isinstance(telemetry, dict) else {}
    interactions = tele.get("interactions") or []
    tin = _as_int(tele.get("total_prompt_tokens")) or 0
    tout = _as_int(tele.get("total_completion_tokens")) or 0
    total = tin + tout
    try:
        usd = float(tele.get("total_accumulated_usd") or 0.0)
    except (TypeError, ValueError):
        usd = 0.0
    any_estimated = any((i or {}).get("estimated") for i in interactions if isinstance(i, dict))
    if not interactions:
        source = "sin datos"
    else:
        source = "estimado (size_estimate)" if any_estimated else "medido (usage)"

    lw = 30  # ancho de etiqueta para alinear los valores
    rows = [
        f"{'Tokens de entrada (in):'.ljust(lw)}{tin:,} tok",
        f"{'Tokens de salida (out):'.ljust(lw)}{tout:,} tok",
        f"{'Tokens totales:'.ljust(lw)}{total:,} tok",
        f"{'Interacciones LLM:'.ljust(lw)}{len(interactions)}",
        f"{'Costo acumulado:'.ljust(lw)}${usd:.4f}",
        f"{'Fuente de tokens:'.ljust(lw)}{source}",
    ]
    agg = aggregate_by_role(interactions)
    if agg:
        rows.append("- por rol " + "-" * (lw - 10))
        for role in sorted(agg):
            a = agg[role]
            rows.append(
                f"{(role + ':').ljust(lw)}in={a['in']:,} out={a['out']:,} "
                f"(${a['usd']:.4f}, {a['n']} int.)"
            )
    return _box("RESUMEN FINOPS - TOKENS POR RUN (costs-telemetry.json)", rows)


def main(argv: list[str] | None = None) -> int:
    """CLI para ver el cuadro de tokens de cualquier run sin re-ejecutar el pipeline:
        python -m orchestrator.cost_telemetry <run_dir | costs-telemetry.json>
    """
    import argparse

    ap = argparse.ArgumentParser(description="Reporte de tokens IN/OUT de un run (FinOps).")
    ap.add_argument("path", help="run_dir o ruta directa a costs-telemetry.json")
    args = ap.parse_args(argv)
    p = Path(args.path)
    if p.is_dir():
        p = telemetry_path(p)
    if not p.exists():
        print(f"[finops] no existe: {p}")
        return 1
    try:
        tele = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[finops] no se pudo leer {p}: {exc}")
        return 1
    print(format_token_summary_table(tele))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
