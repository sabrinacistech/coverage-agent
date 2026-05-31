# Optimization Roadmap — Phases 1-8

Hoja de ruta **aditiva** sobre la arquitectura existente. Cada fase se puede
adoptar de forma independiente sin romper pipelines legacy.

> Filosofía: el sistema debe comportarse como un **asistente interactivo rápido**,
> no como un pipeline de CI pesado.

---

## Phase 1 — Semantic Index foundation

**Goal**: una sola pasada determinística sobre el repo; todos los agentes consultan.

| Categoría        | Cambios                                                              |
|------------------|----------------------------------------------------------------------|
| Archivos nuevos  | `state/index/{classes,methods,imports,dependencies,annotations}.json`, `state/index/README.md`, `skills/00-runtime/semantic-index.md`, `docs/semantic-index-architecture.md` |
| Archivos modific.| `agents/discovery-agent.md`, `agents/classification-agent.md`, `agents/dependency-graph-agent.md`, `agents/symbol-contract-agent.md` |
| Rationale        | Elimina O(agentes × archivos) → O(archivos). Misma evidencia para todos. |
| Riesgo           | Bajo: archivos legacy siguen siendo autoritativos para Generation. |
| Migración        | Pre-stage Python escribe `state/index/`; agentes lo consultan antes de fallback legacy. |
| Backward compat  | Total — si `state/index/` falta, agentes caen a su flujo original. |

---

## Phase 2 — Deterministic vs LLM separation

**Goal**: reducir tokens delimitando con precisión qué hace el LLM.

| Categoría        | Cambios                                                              |
|------------------|----------------------------------------------------------------------|
| Archivos nuevos  | `skills/00-runtime/deterministic-analysis-policy.md`                |
| Archivos modific.| `MASTER_PROMPT.md` (regla 0), `skills/00-runtime/01-context-control.md`, `agents/repair-agent.md` |
| Rationale        | Operaciones determinísticas no consumen tokens. El LLM solo asserts/edge cases/repair complejo. |
| Riesgo           | Bajo: la política es declarativa; no cambia código ejecutable. |
| Migración        | Cada agente revisa sus prompts contra la política; quita lo prohibido. |
| Backward compat  | Total. |

---

## Phase 3 — Incremental execution

**Goal**: que VS Code nunca dispare pipeline completa.

| Categoría        | Cambios                                                              |
|------------------|----------------------------------------------------------------------|
| Archivos nuevos  | `state/incremental-map.json`, `skills/00-runtime/incremental-execution.md` |
| Archivos modific.| `agents/coverage-orchestrator.md`, `skills/00-runtime/03-runtime-mode.md`, `docs/performance-tuning.md` |
| Rationale        | `changedFiles → affectedClasses → affectedTests → coverageDeltaScope` propagación determinística. |
| Riesgo           | Medio: scope mal calculado puede dejar tests sin re-ejecutar. Mitigado por fingerprints + `--full` flag. |
| Migración        | El orquestador refresca `incremental-map.json` antes de cada fase. |
| Backward compat  | `full` sigue disponible como flag explícito o desde CI. |

---

## Phase 4 — Surgical AST-patch generation

**Goal**: parches mínimos en vez de archivos completos.

| Categoría        | Cambios                                                              |
|------------------|----------------------------------------------------------------------|
| Archivos nuevos  | `skills/07-generation/ast-patch-generation.md`                       |
| Archivos modific.| `agents/generation-agent.md`, `skills/00-runtime/01-context-control.md` |
| Rationale        | Patches `InsertMethod`/`AddImport`/`AddMock`/`ReplaceAssertion`/`AddField`/`AddAnnotation` reducen prompts y diffs. |
| Riesgo           | Medio: el aplicador AST debe ser correcto. Mitigado: G1+G6 sobre la proyección antes de escribir; rollback vía `state/_patches/*.diff`. |
| Migración        | Generation Agent emite patches; aplicador legacy detecta archivos completos como fallback. |
| Backward compat  | Sí — formato detectable; archivos completos siguen aceptándose. |

---

## Phase 5 — Deterministic templates

**Goal**: reducir alucinaciones y repetición.

| Categoría        | Cambios                                                              |
|------------------|----------------------------------------------------------------------|
| Archivos nuevos  | `templates/{junit5-mockito,springboot-test,webmvc-test,reactive-test}.java`, `templates/README.md` |
| Archivos modific.| `agents/generation-agent.md`                                         |
| Rationale        | El LLM solo completa cuerpos `@Test`, asserts y edge cases. Esqueleto fijo. |
| Riesgo           | Bajo: si la plantilla no aplica, el agente cae a generación libre con G6. |
| Migración        | Selección de plantilla determinística desde `classification-index.json`. |
| Backward compat  | Total — plantillas son opcionales. |

---

## Phase 6 — Deterministic repair engine

**Goal**: resolver errores conocidos sin LLM.

| Categoría        | Cambios                                                              |
|------------------|----------------------------------------------------------------------|
| Archivos nuevos  | `repair-rules/{imports,mockito,spring,junit,builders}.rules`, `repair-rules/README.md` |
| Archivos modific.| `agents/repair-agent.md`, `agents/validation-agent.md`               |
| Rationale        | Flujo: rule → recompile → LLM fallback (solo si `escalateToLLM` o sin match). |
| Riesgo           | Medio: regla incorrecta puede romper test. Mitigado: G7 (failure-memory) marca FAILED y no se re-aplica. |
| Migración        | Validation anota `suggestedRule` en `compile-error-index.json`. Repair lo aplica primero. |
| Backward compat  | Sí — sin reglas matcheando, Repair sigue su flujo LLM original. |

---

## Phase 7 — Agent consolidation

**Goal**: menos overhead de orquestación.

| Categoría        | Cambios                                                              |
|------------------|----------------------------------------------------------------------|
| Archivos nuevos  | `agents/repository-intelligence-agent.md`                            |
| Archivos modific.| 5 agentes legacy con nota de Phase 7 (`discovery`, `classification`, `dependency-graph`, `symbol-contract`, `stack-profile`) |
| Rationale        | Un único agente que orquesta indexing + classification + dep-graph + contracts + stack profile. |
| Riesgo           | Bajo: legacy permanece operativa. Adopción por opt-in. |
| Migración        | Orquestador puede invocar el consolidado **o** la secuencia legacy. |
| Backward compat  | Total — agentes legacy no se borran. |

---

## Phase 8 — VS Code + LSP optimization

**Goal**: reutilizar JDT.LS para reactividad interactiva.

| Categoría        | Cambios                                                              |
|------------------|----------------------------------------------------------------------|
| Archivos nuevos  | `skills/00-runtime/lsp-integration.md`                               |
| Archivos modific.| `docs/architecture-overview.md`                                      |
| Rationale        | LSP da símbolos, referencias y diagnostics gratis cuando VS Code está activo. |
| Riesgo           | Bajo: LSP es complementario; índice sigue siendo fuente de verdad reproducible. |
| Migración        | Detección runtime: si LSP disponible, fast-path; si no, índice puro. |
| Backward compat  | Total — sin LSP, comportamiento idéntico al previo. |

---

## Resumen de garantías preservadas

- ✅ G1 (whitelist) — alimentada ahora por `state/index/imports.json`.
- ✅ G2 (test quality) — sin cambios.
- ✅ G3 (bytecode-first) — el índice lo respeta nativamente.
- ✅ G4 (generated-sources indexed) — propio del índice.
- ✅ G5 (stack-profile) — generado por Repository Intelligence (o legacy).
- ✅ G6 (linter AST) — corre sobre patches antes de aplicar.
- ✅ G7 (failure-memory) — extendida con `repair-rules` (no relaja).
- ✅ G8 (convergencia) — sin cambios.
- ✅ Atomicidad (`*.tmp` + rename) — aplica a `state/index/` y a `state/incremental-map.json`.
- ✅ Evidence-ids — toda salida del LLM sigue siendo trazable.

## Orden recomendado de adopción

1. Phase 1 (índice) — base de todo lo demás.
2. Phase 2 (política) — documental; aplicable en cualquier momento.
3. Phase 3 (incremental) — requiere Phase 1.
4. Phase 6 (repair determinístico) — independiente, fuerte ROI.
5. Phase 5 (templates) — independiente, fuerte ROI.
6. Phase 4 (AST patches) — requiere Phase 5 ideal.
7. Phase 8 (LSP) — independiente, fast-path interactivo.
8. Phase 7 (consolidación) — al final, cuando los agentes ya delegan al índice.
