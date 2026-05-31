# Repository Intelligence Agent (Phase 7)

> **Estado**: agente consolidado que **sustituye** las antiguas responsabilidades
> de discovery, classification, dependency graph, symbol contract y stack profile.
>
> Los stubs legacy (5 archivos `*-agent.md`) fueron archivados en `agents/_archive/`.
> Toda nueva pipeline debe invocar exclusivamente este agente más el pre-stage
> Python (`tools/python/run_pipeline.py`).

## Responsabilidad

Un único agente que orquesta toda la **inteligencia estructural** del repo
consumiendo el índice semántico (Phase 1). No hace análisis textual: orquesta
consultas determinísticas y materializa los contratos que la fase de Generation
requiere.

## Subáreas (antes agentes independientes)

| Subárea               | Origen (legacy)         | Output                                      |
|-----------------------|-------------------------|---------------------------------------------|
| Indexación            | (nuevo, Phase 1)        | `state/index/*.json` (vía pre-stage Python) |
| Classification        | classification stub     | `state/classification-index.json`           |
| Dependency graph      | dependency graph stub   | `state/dependency-graph.json` (vista filtrada) |
| Framework detection   | parcial de cada stub    | bloque `frameworks` en `classification-index` |
| Contract generation   | symbol contract stub    | `state/symbol-contracts/<fqcn>.json`        |
| Stack profile         | stack profile stub      | `state/stack-profile.json`                  |

## Entradas

- Repositorio + módulos a procesar.
- Pre-stage Python ya ejecutado (`state/index/` y `state/build-tool-contract.json` presentes).
- `state/incremental-map.json` (Phase 3) — restringe el scope si aplica.

## Procedimiento

1. **Verificar índice**: validar `state/index/*.json` contra schemas; si falta o
   está stale → solicitar reindex incremental.
2. **Proyectar classification** desde `index/annotations.json` (Spring, JPA, JAX-RS,
   reactive, etc.) → `classification-index.json`.
3. **Proyectar dependency graph** desde `index/dependencies.json` filtrado por
   módulo y `affectedClasses` (si scope incremental).
4. **Generar contratos** `symbol-contracts/<fqcn>.json` proyectando subset de
   `index/classes.json` + `index/methods.json` + `index/annotations.json`.
5. **Construir stack profile** desde `build-tool-contract.json` + bloque
   `frameworks` del classification.
6. Persistir todo de forma atómica y registrar hashes en `execution-state.json`.

## Reglas

- **Sin LLM** salvo desambiguación final (ej. dos frameworks compatibles).
- **Sin parseo de fuentes**. Todo viene del índice (G3, G4).
- **Idempotente**. Mismo índice + mismo scope ⇒ mismos outputs byte-exact.
- Respeta `affectedClasses` cuando el scope es `incremental` o `single-file`.

## Backward compatibility

- Los stubs legacy fueron archivados en `agents/_archive/` (5 archivos de 11 líneas).
- Pipelines existentes que aún referencien esos nombres deben actualizarse a este
  agente consolidado + el pre-stage Python.
- El orquestador (`coverage-orchestrator.md`) invoca exclusivamente este agente.

## Gates relacionados

- G3 (bytecode-first) — heredado del índice.
- G4 (generated-sources indexados) — heredado del índice.
- G5 (stack-profile válido) — propio.
- G1/G6 — alimentados por los outputs (whitelist, linter).

## Antipatrones

- Invocar los 5 agentes legacy en serie cuando el consolidado cubre todo.
- Reparsear `.java` "por seguridad" cuando el índice ya tiene la info.
- Generar contratos completos cuando el scope incremental requiere solo unos pocos FQCNs.
