# Coverage Orchestrator Agent

## Responsabilidad
Coordinar el flujo completo, validar gates G1–G9 entre fases y mantener `state/execution-state.json` (atomicidad + recuperación). Es el único agente con autoridad para avanzar de fase.

## Ejecución incremental (Phase 3)

- Por defecto, el orquestador opera en scope `single-file` o `incremental` (ver `skills/00-runtime/incremental-execution.md`).
- Antes de cualquier fase, refrescar `state/incremental-map.json` si `git HEAD` cambió.
- Compilación, validación y JaCoCo se narrowean a `affectedTests` / `affectedClasses`.
- `full` requiere flag explícito; nunca es default desde VS Code.

## Entradas
- Repositorio Java.
- Modo (`coverage` | `branch-coverage` | `mutation-hardening`).
- Budget (`maxCycles`, `maxMinutesPerCycle`).

## Salidas
- `state/execution-state.json`
- `state/_summaries/cycle-<n>.json`
- Reporte final delegado a `tools/python/cycle_report_builder.py` (Python determinístico).

## Reglas
1. El LLM ejecuta **únicamente** los turnos Generation (Phase 8) y Repair-LLM
   (Phase 10b). Las fases 1-7 son deterministas (Python) y llegan colapsadas en
   `validate_handoff.py`: antes de Generation, exigir handoff `READY` y consumir
   sólo `handoff-summary.json` + el context-pack compacto del SUT. **Prohibido**
   re-ejecutar 1-7 como turnos LLM o re-leer los nueve JSONs originales (ver
   `skills/00-runtime/02-phase-contracts.md` y `BOOT.md`).
2. Antes de pasar a Generation, exigir:
   - G3 (bytecode-first si `target/classes` existe),
   - G4 (`target/generated-sources` indexado si hay APs),
   - G5 (`stack-profile.json` válido),
   - `symbol-contracts/<sut>.json` para cada SUT del batch,
   - `fixture-catalog.json` con fixtures para los tipos requeridos.
3. Antes de compilar, exigir G1 (whitelist) y G6 (static pre-compile linter) sobre cada test propuesto.
4. Antes de despachar a `repair-agent`, invocar
   `gate_runner.py --patch <patch> --context-pack <pack> --state state/ --auto-repair --test-file <FooTest.java>`.
   El gate runner evalúa G1/G5/G6/G7/G8 y, con `--auto-repair`, si G6 falla
   invoca primero `repair_dispatch.py` (etapa 10a determinista,
   `repair-rules/*.rules`) y re-corre G6. Sólo las violaciones que el
   dispatcher escala (`_escalateReason`) se le pasan al `repair-agent` LLM
   (etapa 10b). **El LLM nunca cuenta intentos** ni decide cuándo escalar —
   thresholds canónicos en `_G7_MAX_FAILED_ATTEMPTS`,
   `_G7_MAX_TESTCASE_ATTEMPTS`, `_G8_MAX_ZERO_DELTA_CYCLES`,
   `_G8_MAX_COMPILE_FAIL_RATE` dentro de `gate_runner.py`.
5. Envolver cada ciclo en `cycle_loop.py` — **el único dueño del loop**. Es la
   forma sancionada de correr un ciclo: aplica el budget (`maxCycles`,
   `maxMinutesPerCycle` de `execution-state.json`) **y** la convergencia G8 **por
   construcción**. No invocar `gate_runner`/`test_patch_applier` "a pelo" fuera
   de este wrapper: si no se tickea `cycle`, el backstop de budget del patcher
   queda inerte (audit C1/C2).
   ```bash
   python tools/python/cycle_loop.py \
       --state     state/execution-state.json \
       --state-dir state/ \
       -- <comando-de-un-ciclo: generation→patch→validation que (re)escribe state/coverage-delta.json>
   ```
   Por cada iteración `cycle_loop`: (1) `tick` (incrementa `cycle` 1-based +
   estampa inicio), (2) `check` de budget de ciclos/minutos (abort `rc=2` si
   excedido), (2b) `check_token_budget` del presupuesto de costo/tokens —
   `state/_summaries/llm-budget.json`; si algún context-pack de SUT excede
   `maxTokensIn` ⇒ abort `rc=2` **antes** del dispatch (el pack over-budget
   nunca llega al LLM, cero Java escrito), (3) corre el comando del ciclo, (4) deriva y escribe los DOS campos que `gate_g8` lee
   (`consecutiveZeroDeltaCycles`, `compileFailRateWindow`) desde
   `coverage-delta.json` y el exit code, (5) `reset` de `cycleStartedAt`, (6)
   evalúa `gate_g8` y para con `rc=5` ante un stall. **El comando envuelto debe
   producir `state/coverage-delta.json`** (vía `narrow_test_runner` + JaCoCo);
   sin él, `cycle_loop` asume delta cero y G8 dispararía un falso stall. El
   `--auto-repair` de `gate_runner` vive *dentro* de ese comando de ciclo.
6. Escritura atómica en `state/` (`*.tmp` + rename); actualizar
   `checkpoints[]` con SHA-256.
7. Particionar trabajo paralelo por SUT (nunca dos agentes sobre el mismo
   archivo de estado).

## Compresión de historial de ciclos (Phase 5)

Al **finalizar cada ciclo** (después de Reporting), invocar:

```bash
python tools/python/cycle_summarizer.py --state state/ --cycle <N> --mode <mode>
```

Esto escribe `state/_summaries/cycle-<N>.json` con un resumen compacto.

**Regla de contexto**: en ciclos posteriores, el Orchestrator carga únicamente:
- Los últimos **2** summaries (`cycle-N.json`, `cycle-(N-1).json`).
- El estado completo del ciclo **actual** solamente.
- **Nunca** los archivos crudos de ciclos anteriores (generated-tests.json, compile-error-index.json, coverage-delta.json de ciclos pasados).

Esto mantiene el presupuesto de contexto O(1) independiente del número de ciclos.

## Rollback via patches (Phase 4)

Patches en `state/_patches/` son escritos por `tools/python/ast_patcher.py` antes de
modificar cada test. Si la validación falla: `ast_patcher.py --rollback <diff>`.

## Criterios de parada
Todos los códigos de parada los devuelve `cycle_loop.py` (dueño único del loop):
- `rc=5` (`RC_CONVERGENCE_STALL`): G8 activado (delta=0 dos ciclos seguidos, o
  compile-fail-rate > 0.5) — thresholds canónicos en `gate_runner.gate_g8`.
- `rc=2` (`RC_BUDGET_EXCEEDED`): budget agotado — `maxCycles` /
  `maxMinutesPerCycle` de `budget_enforcer`, **o** un context-pack que excede
  `maxTokensIn` (`budget_enforcer.check_token_budget` sobre `llm-budget.json`).
- `rc=0` (`RC_DONE`): el comando de ciclo señaló "sin más targets"
  (`--done-exit-code`, default 7) o se alcanzó el objetivo de cobertura del modo.
- Aborto manual.

## Phase → Tool → State

Tabla canónica del trabajo determinista (fases que NO son turnos LLM). Cada fila
describe la fase, la herramienta Python que la materializa y el estado que produce.
El orquestador invoca las herramientas; no hay agente LLM intermedio.

| Phase                | Tool (`tools/python/`)            | State producido                                                  |
|----------------------|-----------------------------------|------------------------------------------------------------------|
| Discovery            | `pom_parser.py`                   | `state/build-tool-contract.json`                                 |
| Archetype            | `archetype_detector.py`           | `state/archetype-profile.json`                                   |
| Generated code       | `generated_code_scanner.py`       | `state/generated-code-index.json`                                |
| Classpath / whitelist| `classpath_resolver.py`           | `state/import-whitelist.json`                                    |
| Repo intelligence    | `repo_intelligence.py` (wrapper)  | `state/_summaries/repo-intelligence.json` + outputs de stack/contracts/index/classification/deps |
| Stack profile        | `stack_profile_detector.py`       | `state/stack-profile.json`                                       |
| Symbol contracts     | `bytecode_scanner.py` + `source_symbol_enricher.py` | `state/symbol-contracts/<fqcn>.json`               |
| Coverage targets     | `jacoco_parser.py --mode targets` | `state/coverage-targets.json`                                    |
| Semantic index       | `semantic_index_writer.py`        | `state/index/{classes,methods,imports,dependencies,annotations}.json` |
| Classification       | `classification_analyzer.py`      | `state/classification-index.json`                                |
| Dependency graph     | `dependency_graph_extractor.py`   | `state/dependency-graph.json`                                    |
| Fixtures             | `fixture_catalog_builder.py`      | `state/fixture-catalog.json`                                     |
| Planning             | `coverage_planner.py`             | `state/batch-plan.json`                                          |
| Incremental scope    | `incremental_map_writer.py`       | `state/incremental-map.json`                                     |
| State validation     | `state_validator.py`              | (no artifact; gate before LLM stage)                             |
| Context packs        | `context_pack_builder.py`         | `state/context-packs/<safe_fqcn>.json` (+ `-compact/` opcional)  |
| Generation (LLM)     | `test-intent-agent` + `test-body-agent` | patch JSON → `tools/python/test_patch_applier.py`          |
| Pre-compile lint     | `gate_runner.py` → `test_linter.py` (G6-quality ON por default) | `state/linter-violations.json` (violaciones G1/G2/G5/G6-quality estructuradas) + `state/_summaries/gates.json` |
| Narrow validation    | `narrow_test_runner.py` + `compile_error_parser.py` | `state/_summaries/build-output.log` + `state/compile-error-index.json` + `state/coverage-delta.json` |
| Mutation hardening   | `mutation_runner.py` (sólo `--coverage-mode mutation-hardening`) | `state/mutation-intelligence.json` |
| Repair (deterministic, 10a) | `repair_dispatch.py` (auto-invocado por `gate_runner.py --auto-repair`) | Aplica `repair-rules/*.rules` con `ast_patcher.py` y emite `state/_summaries/repair-dispatch.json` (counts: repaired, escalated, skipped) |
| Repair (LLM, 10b)    | `repair-agent`                    | Consume sólo `escalated[]` del repair-dispatch → nuevo patch JSON |
| Cycle reporting      | `cycle_report_builder.py`         | `state/_summaries/cycle-<N>-report.json` (summary, sutReports, gateStatus, recommendations) |
| Cycle summary        | `cycle_summarizer.py`             | `state/_summaries/cycle-<N>.json`                                |

Reglas de planning / fixtures / validation que el orquestador centraliza (hoy en
Python determinista, sin turnos LLM intermedios):

- **Planning**: excluir targets sin `hasContract` o `hasFixtures`; ordenar por
  ROI (`skills/06-planning/coverage-roi-planning.md`); batch dinámico por
  `compileFailRate` histórico; en `branch-coverage` nunca dos targets del mismo
  SUT en un batch.
- **Fixtures**: estrategia en orden builder verificado → constructor → factory
  → mock pasivo; variantes mínimas `default`, `boundary` (solo
  `branch-coverage`), `null-optional`, `empty-collections`; nada de
  `LocalDateTime.now()` sin `Clock` controlado.
- **Validation**: nunca `mvn clean` ni `install`; siempre derivar cobertura del
  XML; cualquier `delta < 0` aborta el ciclo; timeout configurable contribuye a
  G8.
