# Telemetría física y FinOps

Sistema de auditoría que mide, de forma empírica, **cuánto** procesa nuestra
arquitectura determinista (batches JSON mínimos) frente a lo que procesaría un LLM
con acceso libre al repositorio vía Agent Skills/Tools, y **cuánto cuesta** cada
interacción con el modelo (tokens, USD, tiempo).

Tres ejes:

1. **Volumetría de workspace** — compresión de contexto (demostración de eficiencia).
2. **FinOps** — tokens + costo USD por ítem y por ronda de reparación.
3. **Rendimiento** — tiempo invertido por interacción, con precisión de milisegundos.

Módulos:

| Módulo | Rol |
|---|---|
| [`orchestrator/workspace_volumetry.py`](../orchestrator/workspace_volumetry.py) | Tamaños físicos de disco + tabla de eficiencia |
| [`orchestrator/cost_telemetry.py`](../orchestrator/cost_telemetry.py) | Pricing, extracción de `usage`, persistencia de `costs-telemetry.json` |
| [`orchestrator/batch_runner.py`](../orchestrator/batch_runner.py) | Cableado en la ruta activa (handoff por lote) |
| [`orchestrator/providers.py`](../orchestrator/providers.py) | Cableado en la ruta de API real (`LiteLLMProvider`) |

---

## Por qué medido vs estimado (las dos rutas de LLM)

El proyecto tiene **dos** rutas de interacción con el modelo, y la telemetría es
honesta sobre cuál produjo cada número:

- **Handoff por archivo** (`batch_runner`, **ruta activa**): el LLM corre **fuera**
  de este proceso (Claude Code / Codex lee `request-*.json` y escribe
  `response-*.json`). No hay payload HTTP, así que **no hay `usage` nativo**.
  - Si la respuesta trae un bloque `usage`, se usa → **medido**.
  - Si no, los tokens se **estiman** por tamaño del payload (~4 chars/token) y la
    interacción queda marcada `"estimated": true` / `"source": "size_estimate"`.
- **`LiteLLMProvider`** (API real, dormida por defecto): se intercepta `resp.usage`
  → siempre **medido** (`"source": "api_usage"`).

> Regla de oro: **una estimación nunca se presenta como medición**. El campo
> `source`/`estimated` de cada interacción permite filtrar en cualquier reporte.

---

## 1. Volumetría de workspace (eficiencia de contexto)

Al **iniciar** el run, `batch_runner` mide el tamaño físico real del repo SUT
analizado. Al **finalizar**, mide el contexto realmente enviado (los
`request-*.json`) y la carpeta de salida completa, y emite en STDOUT:

```
+-------------------------------------------------------------+
| METRICA DE EFICIENCIA DE CONTEXTO (COMPRESIÓN DE WORKSPACE) |
+-------------------------------------------------------------+
| Tamaño Total del Repositorio Real:   120.00 MB              |
| Tamaño del Contexto Enviado (Lote):  12.00 KB               |
| Factor de Reducción de Ruido:        10240.0 x              |
+-------------------------------------------------------------+
[efficiency] carpeta de salida del run: 0.45 MB → <run_dir>
```

- **Repositorio Real**: `directory_size_bytes(repo)` — suma recursiva excluyendo
  `.git`, `node_modules`, `target`, `build`, `dist`, `.gradle`, `__pycache__`,
  `.venv`, `.claude`, etc. (ver `DEFAULT_EXCLUDES`).
- **Contexto Enviado (Lote)**: suma de los `request-*.json` del run — lo único que
  viajó al LLM.
- **Factor de Reducción de Ruido**: `repo / contexto`.

**Tolerancia a fallos:** `directory_size_bytes` usa `os.walk(onerror=...)` y
`try/except` por archivo, salta symlinks y devuelve lo acumulado ante cualquier
`OSError` de permisos/carrera. **Nunca levanta** — la métrica jamás rompe el pipeline.

---

## 2. FinOps — `costs-telemetry.json`

Por cada handoff (o llamada a la API) se agrega una entrada al archivo
`<run_dir>/costs-telemetry.json` (escritura atómica tmp+rename), acumulando los
totales del run. Esquema completo en
[`state/_schemas/protocols/costs-telemetry.schema.json`](../state/_schemas/protocols/costs-telemetry.schema.json).

```json
{
  "schemaVersion": 1,
  "runId": "run-20260617-184002",
  "total_accumulated_usd": 0.026265,
  "total_prompt_tokens": 1772,
  "total_completion_tokens": 875,
  "total_duration_seconds": 60.5,
  "interactions": [
    {
      "targetId": "tgt:0001",
      "role": "generation",
      "round": 0,
      "tokens_in": 414,
      "tokens_out": 13,
      "cost_usd": 0.007185,
      "duration_seconds": 21.25,
      "model": "anthropic/claude-opus-4-8",
      "source": "size_estimate",
      "estimated": true,
      "ts": "2026-06-17T18:40:02+00:00"
    },
    {
      "targetId": "tgt:0001",
      "role": "repair",
      "round": 1,
      "tokens_in": 1245,
      "tokens_out": 850,
      "cost_usd": 0.016485,
      "model": "anthropic/claude-sonnet-4-6",
      "duration_seconds": 18.0,
      "source": "api_usage",
      "estimated": false,
      "ts": "2026-06-17T18:40:02+00:00"
    }
  ]
}
```

### Atribución por target en el handoff por lote

Un handoff genera N targets en una sola ida y vuelta, sin tokens por target. Por eso:

- El **total** (in/out) es **medido** si la respuesta trae `usage`; si no, se
  estima del payload completo (request → input, response → output).
- Ese total se **reparte por target** en proporción al tamaño de su porción del
  request/response (suma exacta, sin pérdida por redondeo).
- La **duración** se reparte en partes iguales (suma exacta al wall-clock del handoff).

### Pricing (configurable)

Tarifas en USD por **millón** de tokens `(input, output)` en
`cost_telemetry._PRICING`:

| Clave (substring del modelo) | Input | Output |
|---|---|---|
| `opus`   | 15.00 | 75.00 |
| `sonnet` | 3.00  | 15.00 |
| `haiku`  | 0.80  | 4.00  |
| `gpt-4o-mini` | 0.15 | 0.60 |
| `gpt-4o` | 2.50 | 10.00 |
| `gpt-4.1` | 2.00 | 8.00 |

- Un modelo desconocido cae al **fallback conservador** (Opus) para no **sub**-estimar.
- Override por entorno, sin tocar código:
  `COVAGENT_PRICE_<KEY>_IN` / `COVAGENT_PRICE_<KEY>_OUT` (USD/Mtok).
  Ejemplo: `COVAGENT_PRICE_OPUS_IN=9 COVAGENT_PRICE_OPUS_OUT=40`.

### `usage` reconocido

`extract_usage` acepta ambos vocabularios y formatos (dict u objeto SDK):

- Anthropic: `input_tokens` / `output_tokens`
- OpenAI: `prompt_tokens` / `completion_tokens`
- anidado en `usage` o al tope del objeto.

Para alimentar números **medidos** en la ruta de handoff, basta con que el
`response-*.json` incluya un bloque `"usage": { "input_tokens": N, "output_tokens": M }`.

---

## 3. Rendimiento — tiempo por interacción

Cada interacción se envuelve con `time.perf_counter()` (request armado → JSON
completo recibido) y registra `duration_seconds` con precisión de milisegundos,
tanto en el log de consola como en el objeto de `interactions[]`.

> **Matiz honesto:** en la ruta de **handoff**, `duration_seconds` es el wall-clock
> del handoff e **incluye** el tiempo de Claude Code / del humano (el LLM corre
> fuera del proceso). En la ruta de **API** (`LiteLLMProvider`) es latencia pura de
> la llamada.

Salida de consola por handoff:

```
[finops] generation r0: 2 target(s) · in=527 out=25 tok · $0.0098 · 42.500s · size_estimate (estimado)
[finops] repair r1: 1 target(s) · in=1245 out=850 tok · $0.0165 · 18.000s · api_usage
```

Y el resumen al cierre del run:

```
[finops] run run-20260617-184002: $0.0263 · in=1772 out=875 tok · 3 interacción(es) · costs-telemetry.json
```

---

## Garantías

- **No rompe el pipeline:** la volumetría nunca levanta; los registros FinOps van
  envueltos en `try/except` en los llamadores (`_record_llm_telemetry`,
  `_record_api_telemetry`) y solo emiten un warning ante un fallo.
- **Escritura atómica:** `costs-telemetry.json` se escribe con tmp+rename — un
  lector concurrente nunca ve un JSON a medio escribir.
- **Determinista y testeable:** ver `tools/python/tests/test_finops_telemetry.py`
  (14 casos: volumetría, pricing, override de entorno, `usage`, acumulación,
  atribución por target medido vs estimado).
