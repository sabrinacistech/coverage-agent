# Deterministic Architecture

## Principio rector

El pipeline tiene una división binaria e infranqueable:

| Capa | Ejecutor | Responsabilidad |
|------|----------|-----------------|
| **Python pre-stage** | Código determinista | Leer bytecode, resolver símbolos, construir índices, clasificar clases, planificar |
| **LLM Agents** | Modelos de lenguaje | Producir JSONs de intención estructurada — nunca código Java directamente |
| **Python post-stage** | Código determinista | Aplicar JSONs al sistema de archivos, validar, reportar |

Los agentes LLM **nunca escriben archivos Java**. Todo cambio físico pasa por `test_patch_applier.py`.

---

## Diagrama de flujo de estado

```
[Repositorio Java]
       │
       ▼
 run_pipeline.py  (16 pasos deterministas)
       │
       ├─ pom_parser.py            → state/build-tool-contract.json
       ├─ stack_profile_detector.py → state/stack-profile.json
       ├─ classpath_resolver.py    → state/import-whitelist.json
       ├─ bytecode_scanner.py      → state/symbol-contracts/<fqcn>.json
       ├─ classification_analyzer.py → state/classification-index.json
       ├─ dependency_graph_extractor.py → state/dependency-graph.json
       ├─ fixture_catalog_builder.py → state/fixture-catalog.json
       ├─ coverage_planner.py      → state/batch-plan.json
       └─ context_pack_builder.py  → state/context-packs/<fqcn>.json
                                         │
                                         ▼
                              [Agente LLM recibe SOLO el context-pack]
                                         │
                                         ▼
                              JSON de intención (patch descriptor)
                                         │
                                         ▼
                       test_patch_applier.py  ← aplica físicamente
                                         │
                                         ├─ Inicializa desde templates/*.java
                                         ├─ Inyecta imports validados
                                         ├─ Inyecta @Mock fields
                                         ├─ Inyecta @Test methods
                                         └─ Detecta colisiones de firmas
                                         │
                                         ▼
                              state/generated-tests.json
                                         │
                                         ▼
                              test_linter.py  (G1/G2/G5 pre-compile)
                                         │
                              [Si OK] → mvn test (compilación + JaCoCo)
                              [Si FAIL] → compile_error_parser.py
                                              → state/compile-error-index.json
                                              → Repair Agent (JSON de reparación)
                                              → test_patch_applier.py (re-aplicación)
```

---

## Capas del pipeline Python

### Capa 1 — Discovery (pasos 1-5)

Herramientas: `pom_parser.py`, `archetype_detector.py`, `generated_code_scanner.py`, `classpath_resolver.py`, `stack_profile_detector.py`

Produce el contrato de construcción: qué frameworks están presentes, qué clases son generadas (excluir de SUT), qué imports son legales.

**Invariante**: ningún agente LLM lee el POM directamente. Solo lee `state/build-tool-contract.json`.

### Capa 2 — Bytecode & Symbol (pasos 6-8)

Herramientas: `bytecode_scanner.py`, `source_symbol_enricher.py`, `semantic_index_writer.py`

Produce un contrato por clase SUT con `evidence-id` por símbolo. La autoridad es el bytecode (`javap -p -s`); AST solo como fallback documentado.

**Invariante**: ningún agente LLM invoca `javap`. El contrato en `state/symbol-contracts/<fqcn>.json` es la única fuente de verdad de símbolos.

### Capa 3 — Coverage & Planning (pasos 9-13)

Herramientas: `jacoco_parser.py`, `classification_analyzer.py`, `dependency_graph_extractor.py`, `fixture_catalog_builder.py`, `coverage_planner.py`

Produce el plan de cobertura (`batch-plan.json`) con priorización ROI. Los agentes leen el plan; no leen JaCoCo XML.

### Capa 4 — Context Pack (paso 16)

Herramienta: `context_pack_builder.py`

Produce `state/context-packs/<fqcn>.json`: una rebanada mínima de información suficiente para que el agente genere tests de UN SUT. Ver `docs/token-minimization-strategy.md`.

### Capa 5 — Patch Application (post-LLM)

Herramienta: `test_patch_applier.py`

Recibe el JSON del agente y lo aplica físicamente. Es el único escritor autorizado de archivos Java.

### Capa 6 — Validation & Repair (post-apply)

Herramientas: `test_linter.py`, `compile_error_parser.py`, `ast_patcher.py`, `state_validator.py`

Valida antes y después de compilar. Los errores de compilación se normalizan a tokens unificados y se entregan al Repair Agent como JSON.

---

## Gates anti-alucinación

| Gate | Herramienta que lo aplica | Condición de bloqueo |
|------|--------------------------|----------------------|
| G1   | `test_linter.py` | Import fuera de `import-whitelist.json` |
| G2   | `test_linter.py` | Símbolo sin `evidence-id` en contrato |
| G3   | `bytecode_scanner.py` | `target/classes` existe y no se usó bytecode |
| G4   | `generated_code_scanner.py` | Annotation processors sin `target/generated-sources` |
| G5   | `test_linter.py --stack-profile` | Versión de framework incompatible |
| G6   | Pipeline (pre-`mvn`) | Linter falla antes de compilar |
| G7   | `state/failure-memory.json` | Fix ya falló previamente |
| G8   | `run_pipeline.py` orchestrator | 2 ciclos sin delta o >50% compile-fail |
| G9   | `compile_error_parser.py` | Errores JDT normalizados, no libres |

---

## Contratos de estado (flujo de escritura)

Cada herramienta escribe exactamente un archivo de estado usando `atomic_write_json` (write a `.tmp`, luego `rename`). El hash SHA-256 del archivo se registra en `execution-state.json`.

```
Herramienta Python → <state>.tmp → rename → <state>.json
                                               │
                              state_validator.py valida contra
                              state/_schemas/<state>.schema.json
```

**Nunca** se escribe directamente sobre un archivo de estado activo (evita corrupción parcial en caso de error).

---

## Separación física de responsabilidades

```
tools/python/         ← código determinista (Python, sin LLM)
agents/               ← instrucciones de agentes (producen JSON, nunca archivos Java)
templates/            ← esqueletos Java (llenados por test_patch_applier.py)
state/                ← contratos JSON (escritos por Python, leídos por todos)
state/_patches/       ← JSONs de parches producidos por agentes LLM
state/context-packs/  ← insumos mínimos para agentes LLM
```

---

## Budget enforcement (cycle orchestrator)

`execution-state.json` declara `budget.maxCycles` y `budget.maxMinutesPerCycle`, pero el orchestrator del cycle-loop vive **fuera** de `run_pipeline.py` (típicamente un wrapper externo o el propio agente LLM que invoca ciclos). Para que el budget se aplique por construcción —no por convención— ese orchestrator DEBE invocar `tools/python/budget_enforcer.py` en tres puntos del ciclo:

| Hook | Comando | Comportamiento esperado |
|------|---------|--------------------------|
| **Pre-ciclo** | `python tools/python/budget_enforcer.py check --state state/execution-state.json` | rc=0 → continuar. rc=2 → abortar (presupuesto agotado). rc=3 → state malformado, parar y diagnosticar. |
| **Inicio de ciclo** | `python tools/python/budget_enforcer.py tick --state state/execution-state.json` | Incrementa `cycle` y estampa `cycleStartedAt` antes de iniciar el trabajo del ciclo. |
| **Fin de ciclo** | `python tools/python/budget_enforcer.py reset --state state/execution-state.json` | Limpia `cycleStartedAt` al cerrar el ciclo (éxito o fallo controlado). |

`check` evalúa dos invariantes: `cycle < budget.maxCycles` y `(now - cycleStartedAt) < budget.maxMinutesPerCycle`. Si cualquiera se viola, rc=2 y el orchestrator **MUST** interrumpir; reintentar sin abortar viola G8.

`run_pipeline.py` y `run.py` son del pre-stage y NO invocan estos hooks — corren una sola vez. Son ciclos de Phase 6 (repair) y multi-batch generation los que requieren enforcement.
