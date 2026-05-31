# Phase Contracts

> **Post-audit 2026-05-28**: las fases 1-7 (Discovery → Planning) **no son turnos
> del LLM**. Las produce el pipeline determinista (`run_pipeline.py`) y las colapsa
> en una única validación, `validate_handoff.py` (ver `BOOT.md` y `MASTER_PROMPT.md`).
> El LLM consume **sólo** `state/_summaries/handoff-summary.json` +
> `state/context-packs-compact/<safe_fqcn>.json`, y está **prohibido** re-leer los
> nueve JSONs originales. Los **únicos** turnos del LLM son **Generation (8)** y
> **Repair-LLM (10b)**.

## Contratos de runtime (lo que el Orchestrator bloquea entre turnos)

Cada contrato declara entradas, salidas, precondición y criterio de avance. El
Orchestrator bloquea el avance si alguna falla. `Tipo` marca quién lo ejecuta:
**LLM** (turno del agente) o `DET` (Python determinista, sin turno LLM).

| Fase | Tipo | Entradas | Salidas | Precondición | Criterio de avance |
|------|------|----------|---------|--------------|---------------------|
| 0 Pre-stage + Handoff | `DET` | repo | los `state/*.json` + `handoff-summary.json` | repo accesible | `validate_handoff.py` ⇒ `READY` |
| 8 Generation | **LLM** | handoff-summary, context-pack-compact | patch JSON, `generated-tests.json` | handoff `READY` (G1/G2/G5) | tests con `evidence-ids` |
| 9 Validation | `DET` | patch aplicado | `compile-error-index.json`, `coverage-delta.json` | G6 PASS | build narrow ejecutado |
| 10a Repair (det.) | `DET` | compile-error-index | tests reparados | `repair-rules` aplicable | error resuelto o escalado |
| 10b Repair (LLM) | **LLM** | `escalated[]` del dispatcher | patch JSON repair | G7 (no en failure-memory) | error resuelto o test descartado |
| 11 Reporting | `DET` | todos los estados | `cycle-<N>-report.json` | ciclo cerrado | XML JaCoCo referenciado |

## Referencia — fases 1-7 (DETERMINISTAS, colapsadas en `validate_handoff.py`)

Producidas por Python en Phase 0; el LLM no las ejecuta. La tabla canónica
herramienta → artefacto vive en `MASTER_PROMPT.md` (sección "Reference 1-7 —
outputs Python") y **no se re-declara aquí** (regla de fuente única, ver
`docs/canonical-prohibitions.md`). Su resultado agregado llega al LLM vía
`handoff-summary.json`.

## Regla global
- Sólo **Generation (8)** y **Repair-LLM (10b)** son turnos del LLM.
- Avanzar a Generation sin handoff `READY` ⇒ FAIL del Orchestrator (no continuar).
- Avanzar sin precondición en cualquier contrato de runtime ⇒ FAIL del Orchestrator.
