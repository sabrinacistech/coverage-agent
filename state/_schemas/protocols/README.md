# state/_schemas/protocols/

Schemas que describen **contratos de mensajería** entre subsistemas (LLM ↔ patcher, agente ↔ agente), no estados persistentes en disco.

A diferencia de `state/_schemas/*.schema.json` (que se mapean 1-a-1 con un `state/<name>.json` y son validados automáticamente por `tools/python/state_validator.py`), los schemas en este subdirectorio:

- No tienen un `state/<name>.json` correspondiente.
- Validan objetos efímeros (responses LLM, payloads transportados).
- Quedan fuera del scan automático del validator (`glob("*.schema.json")` no es recursivo).

## Contenido

### Activos (con productor/consumidor en `tools/python/`)

- `patch-descriptor.schema.json` — formato canónico de patch que producen `test-body-agent` y `repair-agent`, y que consume `tools/python/test_patch_applier.py`. Documentado en [`docs/agent-json-protocol.md`](../../../docs/agent-json-protocol.md).
- `context-pack-compact.schema.json` — input único del LLM por SUT; lo emite `context_pack_builder.py` y lo valida `validate_handoff.py`.
- `handoff-summary.schema.json` — resumen pre-generación que emite `validate_handoff.py` (`READY` / `BLOCKED_PRE_STAGE_*`); el LLM consume **solo** este archivo + el context-pack compacto.
- `llm-budget.schema.json` — presupuesto de tokens por SUT que acumula `context_pack_builder.py` en `state/_summaries/llm-budget.json`.
- `telemetry.schema.json` — telemetría de repair que actualiza `repair_telemetry.py` en `state/telemetry.json`.

### DRAFT — contratos propuestos sin productor (auditoría 2026-05-29)

Conservados como contrato de mensajería propuesto; **ningún tool en `tools/python/` los emite ni valida todavía**. No asumas que están garantizados hasta que tengan productor:

- `artifact-map.schema.json`
- `gate-failure.schema.json`
- `pipeline-run.schema.json`
- `cycle-summary.schema.json` — `cycle_summarizer.py` emite `state/_summaries/cycle-N.json`, pero **no** lo valida contra este schema.
