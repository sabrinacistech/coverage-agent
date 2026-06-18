# `prompts/` — plantillas de handoff editables por humanos

Esta carpeta contiene los **prompts de handoff** que `coverage-agent` le entrega a un
LLM (Claude Code / Codex) cuando trabaja en modo `ide` / `handoff-batch`. El runner
(`orchestrator/batch_runner.py`) **lee estas plantillas `.md`, las completa con las
rutas reales del batch y las imprime/escribe** junto a cada request (en
`handoff-prompt.txt`).

El objetivo es que cualquier persona pueda **mejorar el prompt que guía al LLM** —
para que genere mejores tests— **sin tocar código Python**. Editá el `.md`, guardá, y
el siguiente batch usará el texto nuevo.

## Archivos

| Archivo                  | Cuándo se usa                                    |
|--------------------------|--------------------------------------------------|
| `handoff-generation.md`  | Generación de tests de un batch (`generation`).  |
| `handoff-repair.md`      | Reparación de los tests que fallaron (`repair`). |

## Variables disponibles (las completa Python)

Las plantillas usan la sintaxis `${VARIABLE}` de
[`string.Template`](https://docs.python.org/3/library/string.html#template-strings).
Python las sustituye con `safe_substitute`, así que **las llaves `{ }` del JSON de
ejemplo quedan intactas** y un `${...}` desconocido no rompe el render.

| Variable           | Significado                                                              |
|--------------------|--------------------------------------------------------------------------|
| `${REQUEST_PATH}`  | Ruta absoluta del `request-*.json` que el LLM debe **leer**.             |
| `${RESPONSE_PATH}` | Ruta absoluta del `response-*.json` que el LLM debe **escribir**.        |
| `${SCHEMA_VERSION}`| `schemaVersion` que debe declarar la respuesta.                          |
| `${RUN_ID}`        | Id del run (`run-YYYYMMDD-HHMMSS`).                                      |
| `${BATCH_ID}`      | Id del batch (`batch-001`, …).                                          |
| `${REPAIR_ROUND}`  | Número de ronda de reparación (solo `handoff-repair.md`).                |

Cualquier `${...}` que no esté en esta tabla se deja literal — agregá variables nuevas
en `orchestrator/prompts.py` (`render_handoff_prompt`) si las necesitás.

## Reglas para editar (no romper el contrato)

El runner **valida la respuesta del LLM contra el esquema**, así que el prompt puede
mejorar el *cómo* pero no debe contradecir el *qué*:

1. Mantené el `schemaVersion` y la matriz de estados por target/item.
2. No pidas devolver `patchDescriptor`/`testSource` en generación (el runner los
   construye). En repair, sí se devuelve `patchDescriptor`.
3. Conservá la regla de aislamiento (no leer el repositorio) y
   `method.evidenceIds ⊆ target.allowedEvidenceIds`.
4. Mantené los marcadores `COPIÁ DESDE ACÁ` / `COPIÁ HASTA ACÁ` si querés que el bloque
   sea fácil de copiar desde la consola.

Si una plantilla falta o no se puede leer, el runner usa un prompt mínimo embebido
como fallback para no cortar el run.
