# Performance Tuning

Reporte de un caso real: **30 minutos para 3 tests, uno con imports irresolutos**.

Esto pasa cuando los pasos deterministas se ejecutan dentro del LLM (parseo de POM, cálculo de classpath, lectura de javap, etc.) o cuando cada ciclo recompila Maven desde cero. Esta guía elimina ambos.

## Diagnóstico del caso

| Síntoma | Causa raíz |
|---------|-----------|
| 30 min totales | Cada ciclo de generación corre `mvn clean test` y el LLM relee POMs/contratos. |
| 1 test con imports rotos | El símbolo se "infirió" en lugar de citarse desde el contrato. La whitelist no estaba precomputada o el agente no la consultó. |
| Solo 3 tests | El planificador no usa batches y los agentes serializan trabajo que podría ir en paralelo. |

## Plan de optimización

### 1. Pre-stage en Python obligatorio
Antes de cualquier ciclo de generación:

```bash
mvn -q -DskipTests package
python tools/python/run_pipeline.py \
   --repo . \
   --out docs/agents/java-test-coverage-architecture/state \
   --module <module-name> \
   --include-fqcn '^com\.acme\.' \
   --jacoco-xml target/site/jacoco/jacoco.xml \
   --coverage-mode coverage
```

Esto produce `build-tool-contract.json`, `archetype-profile.json`, `generated-code-index.json`, `import-whitelist.json`, `symbol-contracts/<fqcn>.json` y, si hay JaCoCo, `coverage-targets.json`. El LLM solo lee estos.

### 2. Nada de `mvn clean` entre ciclos
- Mantener `target/`.
- Ejecutar `mvn -o -pl <module> -Dtest=A,B,C test` (offline + selección puntual de tests).
- `clean` solo si se cambian dependencias.

### 3. Batches reales
Tamaños por tipo de SUT (ver `skills/06-planning/dynamic-batch-sizing.md`):

| Tipo | Batch máx. |
|------|------------|
| POJO / DTO | 8 |
| Mapper / Validator | 5 |
| Controller / Service | 3 |
| Adapter externo / WebClient / SOAP | 1–2 |
| Resilient (retry, circuit-breaker) | 1 |
| Consumer de tipo generado | 1–2 |

Un único `mvn test` por batch, no por test.

### 4. Lint Python antes de compilar
Cualquier test propuesto pasa primero por `test_linter.py`. Si rompe G1, jamás llega a `javac`. Esto evita el ciclo `compilar → fallar → reparar → recompilar` (3+ minutos cada uno).

### 5. Contratos solo de lo necesario
- `bytecode_scanner.py --include '^com\.acme\.modulo\.'` limita el barrido al package del módulo.
- Para el batch actual, el agente proyecta una vista **mínima** del contrato (solo métodos referenciados) en `state/symbol-contracts/_views/<batchId>.json`. No incrustar el contrato completo en el prompt.

### 6. Paralelizar contratos
`bytecode_scanner.py` ya es por-clase. Para muchos módulos:
```bash
ls modules/ | xargs -P 4 -I{} python tools/python/bytecode_scanner.py --repo . --module {} --out state
```

### 7. Caché agresiva
- Caché centralizada en `state/_summaries/cache.json` (subconjunto `_CACHEABLE_STEPS` de `run_pipeline.py`) activa por defecto.
- No invalida si las entradas del paso (hash) no cambiaron.

### 8. Mensajes a los agentes (presupuesto)
- El prompt de los agentes de generación (`test-intent-agent` + `test-body-agent`) debe entregar **solo**:
  - 1 SUT a la vez (o batch homogéneo)
  - vista mínima de su contrato (`_views/<batchId>.json`)
  - subset relevante de la whitelist (imports candidatos)
  - reglas del arquetipo (`archetype-profile.json#implies`)
- No incluir el POM, el changelog completo, ni el classpath crudo.

### 9. Modo offline para repair
Tras compilar, `compile_error_parser.py` genera `compile-error-index.json`. El `repair-agent` lee solo ese JSON; no relee Surefire reports XML completos.

### 10. Convergencia explícita
Cortar el loop si:
- Dos ciclos consecutivos con delta=0 de cobertura.
- `compileFailRate > 0.5` en un ciclo.
- Mensaje claro al usuario, no seguir gastando tokens "por las dudas".

## KPIs después de la optimización (referencia)

| Métrica | Antes | Esperado |
|---------|-------|---------|
| Tokens promedio por test generado | 25k–50k | 4k–8k |
| Tiempo por test (batch chico) | 10 min | 30–90 s |
| Tests por ciclo | 1–3 | 5–15 (POJO) / 3–5 (Service) |
| Tasa de imports irresolutos | >20% | <2% (G1 bloquea antes) |

## Anti-patrones a evitar

- Llamar a `mvn clean` entre tests del mismo ciclo.
- Pegar `pom.xml` completo en el prompt.
- Pegar `javap` crudo o jars de classpath en el prompt.
- Pedir al LLM que "deduzca" si un import existe en el classpath.
- Generar tests sin pasar por `test_linter.py` primero.
- Ejecutar JaCoCo full-report tras cada test individual; consolidar por batch.
- Reescribir contratos en cada ciclo en vez de cachear por SHA.

## Optimization Roadmap — Phases 1-8

Las optimizaciones anteriores son la base. El roadmap incremental añade:

- **Phase 1 — Semantic Index** (`state/index/`): elimina el reparseo cruzado entre agentes. Ver `docs/semantic-index-architecture.md`.
- **Phase 2 — Determinismo vs LLM**: política estricta sobre qué se computa y qué se prompea. Ver `skills/00-runtime/deterministic-analysis-policy.md`.
- **Phase 3 — Ejecución incremental**: `state/incremental-map.json` propaga `changedFiles → affectedClasses → affectedTests`. Ver `skills/00-runtime/incremental-execution.md`.
- **Phase 4 — Generación quirúrgica (AST patches)**: emitir parches mínimos, no archivos completos. Ver `skills/07-generation/ast-patch-generation.md`.
- **Phase 5 — Plantillas determinísticas**: `templates/*.java` reducen alucinación. El LLM completa cuerpos/asserts, no esqueletos.
- **Phase 6 — Repair determinístico**: `repair-rules/*.rules` resuelven antes de llamar al LLM.
- **Phase 7 — Consolidación**: `tools/python/run_pipeline.py` orquesta de forma determinista discovery/classification/dep-graph/symbol-contract/stack-profile en un solo pre-stage (no es un turno LLM). El descubrimiento de módulos Maven se resuelve una sola vez (`pom_parser` → `build-tool-contract.json`) y el resto de las tools lo reusa vía `find_pom_modules(..., contract=...)`.
- **Phase 8 — LSP**: reutilizar JDT.LS de VS Code en vez de re-resolver símbolos. Ver `skills/00-runtime/lsp-integration.md`.

### KPIs adicionales esperados tras phases 1-8

| Métrica                                | Tras 10 ítems anteriores | Tras Phases 1-8 |
|----------------------------------------|--------------------------|-----------------|
| Tokens por test (Service)              | 4k-8k                    | 1.5k-3k         |
| Reparseos de `.java` por ciclo         | O(agentes × archivos)    | 0 (índice)      |
| `mvn` por edición de un archivo (VS)   | full module              | `-Dtest=<one>`  |
| Tamaño prompt repair (típico)          | 1.5k-3k                  | 0.3k-0.8k       |
| Latencia generación de 1 test (warm)   | 30-90s                   | 5-15s           |

## Trabajo futuro (paralelismo)

Optimizaciones identificadas que **aún no están implementadas** en `tools/python/run_pipeline.py`. Se documentan acá para que cualquier refactor futuro tenga el plan ya escrito; el código actual sigue siendo secuencial por simplicidad y reproducibilidad de logs.

### 1. Pasos independientes en paralelo

Los siguientes pasos no comparten dependencias de I/O sobre `state/*.json` y pueden ejecutarse en paralelo después de Step 1 (POM parsing):

- `archetype_detector.py` ∥ `generated_code_scanner.py` ∥ `classpath_resolver.py`

Beneficio estimado: −2 a −5 segundos por ciclo Phase 0 en repos medianos.

### 2. `bytecode_scanner` N-paralelo por FQCN

`bytecode_scanner.py` ya es por-clase a nivel de output (un `symbol-contracts/<fqcn>.json` por SUT). Una pool de workers (multiprocessing) que reparta los FQCNs por archivo de `target/classes/**/*.class` reduciría wall-clock de O(n) a O(n/workers) en repos con > 50 SUTs candidatos.

Riesgo conocido: contención sobre `state/_cache/` si varios procesos escriben simultáneamente entradas con el mismo SHA. Mitigación: shard del cache por hash-prefix o lock con `fcntl.flock` por entrada.

### 3. Auditoría del cache `state/_cache/`

`state/_cache/` ya está documentado como activo por defecto. Trabajo pendiente: agregar un script de verificación (`tools/python/cache_audit.py`) que recorra los 24 scripts del pipeline y reporte cuáles llaman `cache.lookup()` / `cache.put()` y cuáles re-computan sin consultar. Salida sugerida: `state/_cache/audit.json` con `{script, cacheHits, cacheMisses, bypassed}`.

> Estas optimizaciones son **opt-in futuro**. No se implementan en este refactor porque excederían el alcance de "estructura/documentación" definido en las reglas duras de ejecución.

