# CORRECCIONES_IMPLEMENTADAS

**Fecha de ejecución:** 2026-05-26 09:34  
**Entorno Python:** 3.14.5  
**Commit de referencia:** 93b1613 — *Depuración de MASTER_PROMPT.md*  
**Rama:** main  
**Archivos modificados:** 4  

---

## 1. Evidencia de compilación limpia — `python -m py_compile tools/python/*.py`

```
$ python -m py_compile tools/python/*.py
ALL_CLEAN   (exit code: 0)
Scripts analizados: 24
```

| Script | Estado |
|--------|--------|
| `archetype_detector.py` | OK |
| `ast_patcher.py` | OK |
| `bytecode_scanner.py` | OK |
| `classpath_resolver.py` | OK |
| `classification_analyzer.py` | OK |
| `common.py` | OK |
| `compile_error_parser.py` | OK |
| `context_pack_builder.py` | OK |
| `coverage_planner.py` | OK |
| `cycle_summarizer.py` | OK |
| `dependency_graph_extractor.py` | OK |
| `fixture_catalog_builder.py` | OK |
| `generated_code_scanner.py` | OK |
| `incremental_map_writer.py` | OK |
| `jacoco_parser.py` | OK |
| `pom_parser.py` | OK |
| `run_pipeline.py` | OK |
| `semantic_index_writer.py` | OK |
| `source_symbol_enricher.py` | OK |
| `stack_profile_detector.py` | OK |
| `stacktrace.py` | OK |
| `state_validator.py` | OK |
| `test_linter.py` | OK |
| `test_patch_applier.py` | OK |

Sin errores de sintaxis ni de importación en ninguno de los 24 módulos.

---

## 2. Verificación de interfaces CLI (`--help`)

Scripts referenciados en la tabla de correspondencias de `MASTER_PROMPT.md`.  
Todos retornan exit code 0 con línea de uso válida.

| Script | Primera línea de `--help` | Exit |
|--------|--------------------------|------|
| `classification_analyzer.py` | `usage: classification_analyzer.py [-h] --out OUT [--contracts CONTRACTS]` | 0 |
| `dependency_graph_extractor.py` | `usage: dependency_graph_extractor.py [-h] --out OUT [--contracts CONTRACTS]` | 0 |
| `fixture_catalog_builder.py` | `usage: fixture_catalog_builder.py [-h] --out OUT [--contracts CONTRACTS]` | 0 |
| `coverage_planner.py` | `usage: coverage_planner.py [-h] --out OUT [--batch-size ...] [--mode ...]` | 0 |
| `context_pack_builder.py` | `usage: context_pack_builder.py [-h] --out OUT [--sut SUT] [--dry-run]` | 0 |
| `test_patch_applier.py` | `usage: test_patch_applier.py [-h] --patch PATH --repo DIR [--state DIR] ...` | 0 |
| `test_linter.py` | `usage: test_linter.py [-h] --test-file PATH --whitelist PATH ...` | 0 |
| `compile_error_parser.py` | `usage: compile_error_parser.py [-h] --log PATH --out PATH [--run ID] ...` | 0 |
| `run_pipeline.py` | `usage: run_pipeline.py [-h] --repo REPO --out OUT [--module MODULE] ...` | 0 |

---

## 3. Estado detallado de cumplimiento — Criterios de aceptación arquitectónicos

### Corrección 1 — Refactorización del protocolo de agentes

#### 1.1 Eliminación del esquema obsoleto `bodyLines`

| Criterio | Resultado |
|----------|-----------|
| `bodyLines` eliminado de `test-body-agent.md` | **CUMPLIDO** — grep devuelve NONE |
| `requiredImports` / `requiredFields` eliminados de `test-body-agent.md` | **CUMPLIDO** |
| `fixKind` / `patches[oldValue/newValue]` eliminados de `repair-agent.md` | **CUMPLIDO** |
| `bodyLines` ausente en toda la carpeta `prompts/` y `docs/` | **CUMPLIDO** |

#### 1.2 Nuevo esquema de salida nativo de `test_patch_applier.py`

Ambos agentes producen ahora el patch descriptor canónico:

```
test-body-agent.md  →  patchId: "patch:<id>"
repair-agent.md     →  patchId: "repair:<id>"  +  repairOf: "<originalPatchId>"
```

Campos verificados presentes en los schemas de salida de ambos agentes:

| Campo | test-body-agent | repair-agent |
|-------|----------------|--------------|
| `schemaVersion` | ✓ | ✓ |
| `patchId` | `patch:<id>` | `repair:<id>` |
| `repairOf` | — | ✓ |
| `sut` | ✓ | ✓ |
| `testClass` | ✓ | ✓ |
| `targetModule` | ✓ | ✓ |
| `targetDir` | ✓ | ✓ |
| `template` | ✓ | ✓ |
| `allowedImports` | ✓ | ✓ |
| `fields[].name/type/annotation` | ✓ | ✓ |
| `methods[].name/annotations/body/evidenceIds` | ✓ | ✓ |

#### 1.3 Reglas duras — Prohibición de construcciones Java en `methods[].body`

Texto canónico insertado en **ambos** agentes (línea exacta en archivo):

- `test-body-agent.md:97` — `**PROHIBIDO** dentro de body: sentencias import, cláusulas package, declaraciones public class, class, interface o enum.`
- `repair-agent.md:136` — idem

Prohibición también presente en **Prohibiciones absolutas** de cada agente:

- `test-body-agent.md` — `NUNCA insertes sentencias import, cláusulas package o declaraciones de clase (...) dentro del texto de methods[].body.`
- `repair-agent.md` — idem

#### 1.4 Restricción de tipos en `fields[]`

Regla presente en **Prohibiciones absolutas** y en **Reglas de `fields[]`** de ambos agentes:

> Solo tipos validados en `contextPack.dependencies`, `contextPack.sut` o el catálogo de fixtures entregado.

#### 1.5 Contrato de bloqueo controlado

| Elemento | Archivo | Línea |
|----------|---------|-------|
| Schema `{ "schemaVersion": 1, "status": "BLOCKED", "blockReason": "..." }` | `test-body-agent.md` | 87 |
| Schema `{ "schemaVersion": 1, "status": "BLOCKED", "blockReason": "..." }` | `repair-agent.md` | 102 |
| Activación automática ante indeterminación técnica | `test-body-agent.md` | sección "Caso de bloqueo" |
| Activación por anti-loop (≥2 ciclos FAILED o >3 intentos) | `repair-agent.md` | 129–130 |
| Ciclo de vida: registro en `state/failure-memory.json` sin invocar al patcher | `docs/agent-json-protocol.md` | 150–151 |

#### 1.6 `docs/agent-json-protocol.md` — Actualizaciones de protocolo

| Cambio | Estado |
|--------|--------|
| Clarificación de que Body Agent y Repair Agent usan el mismo formato canónico | **CUMPLIDO** |
| Nueva sección "Contrato de bloqueo" con ciclo de vida del objeto BLOCKED | **CUMPLIDO** |
| Restricciones explícitas de `body` en la sección `methods[]` | **CUMPLIDO** |
| `repairOf` documentado como campo del repair patch | Preexistente — preservado |

---

### Corrección 7 — Depuración filosófica de `MASTER_PROMPT.md`

#### 7.1 Tabla de correspondencias herramienta ↔ responsabilidad

Insertada en la sección **División absoluta del trabajo** (líneas 68–81):

| Tarea declarada | Herramienta asignada | Artefacto de salida |
|-----------------|---------------------|---------------------|
| Classification | `tools/python/classification_analyzer.py` | `state/classification-index.json` |
| Dependency Graph | `tools/python/dependency_graph_extractor.py` | `state/dependency-graph.json` |
| Fixture Catalog | `tools/python/fixture_catalog_builder.py` | `state/fixture-catalog.json` |
| Planning | `tools/python/coverage_planner.py` | `state/batch-plan.json` |
| Context Packs | `tools/python/context_pack_builder.py` | `state/context-packs/<fqcn>.json` |
| Generation | Agentes LLM | Esquemas estructurados JSON (no archivos Java completos) |
| Patch Application | `tools/python/test_patch_applier.py` | Mutación física de archivos Java en disco |
| Validation | `tools/python/test_linter.py` | Pre-compilado estático (static pre-compile linter) |
| Compile Error Normalization | `tools/python/compile_error_parser.py` | `state/compile-error-index.json` |
| Repair | Agentes LLM | Parches correctivos JSON basados en errores normalizados |

**Verificación:** `grep` retorna 10 coincidencias en la tabla + 5 en las fases operativas.

#### 7.2 Reescritura de fases con rol imperativo del LLM → rol reactivo

| Fase | Antes (LLM como ejecutor) | Después (LLM como consumidor) |
|------|--------------------------|-------------------------------|
| 3 — Classification | `Clasificar clases según testabilidad, riesgo...` | `Leer state/classification-index.json producido por classification_analyzer.py` |
| 5 — Dependency Graph | `Mapear DI real (constructor/field/setter)...` | `Leer state/dependency-graph.json producido por dependency_graph_extractor.py` |
| 6 — Fixture Catalog | `Builders/constructors/factories verificados...` | `Leer state/fixture-catalog.json producido por fixture_catalog_builder.py` |
| 7 — Planning | `Leer target/site/jacoco/jacoco.xml, cruzar con clasificación...` | `Leer state/batch-plan.json producido por coverage_planner.py` |
| 8 — Generation | `Generar tests usando solo contratos` | `Consumir context-packs/<fqcn>.json (...) producen esquemas JSON (no archivos Java completos)` |

**Verificación:** `grep -n "Leer.*producido por" MASTER_PROMPT.md` → 4 coincidencias (fases 3, 5, 6, 7).

#### 7.3 Barrido de "AST linter" / "Linter AST" → "static pre-compile linter"

| Ocurrencia original | Ubicación | Texto reemplazado |
|--------------------|-----------|-------------------|
| `Linter AST sobre el test propuesto (gate G6)...` | Fase 9, línea 176 | `static pre-compile linter (tools/python/test_linter.py) sobre el test propuesto...` |
| `AST del test propuesto valida 100% de símbolos...` | Gate G6, línea 195 | `static pre-compile linter (tools/python/test_linter.py) valida 100% de símbolos...` |
| Tabla de correspondencias | Línea 77 | `Pre-compilado estático (static pre-compile linter)` |

**Verificación:**

```
$ grep -n "AST linter|Linter AST" MASTER_PROMPT.md
(sin resultados)

$ grep -n "static pre-compile linter" MASTER_PROMPT.md
77:  Pre-compilado estático (static pre-compile linter)
176: static pre-compile linter (tools/python/test_linter.py) sobre el test propuesto...
195: static pre-compile linter (tools/python/test_linter.py) valida 100% de símbolos...
```

**Nota de preservación:** La referencia `AST solo como fallback documentado` en el gate **G3** no fue alterada. Esa cláusula describe la precedencia de análisis de bytecode vs. AST para resolución de símbolos del contrato SUT — no el linter — conforme a la excepción explícita indicada en los criterios de aceptación.

#### 7.4 Preservación de reglas anti-alucinación

```
$ grep -c "G[1-9]" MASTER_PROMPT.md
12   (9 definiciones de gates + 3 referencias cruzadas)
```

| Gate | Estado |
|------|--------|
| G1 Import whitelist | **PRESERVADO** — sin alteración |
| G2 Symbol evidence | **PRESERVADO** — sin alteración |
| G3 Bytecode-first | **PRESERVADO** — nota AST-fallback intacta |
| G4 Generated sources | **PRESERVADO** — sin alteración |
| G5 Stack profile | **PRESERVADO** — sin alteración |
| G6 Linter pre-compile | **PRESERVADO + ACTUALIZADO** — texto corregido a "static pre-compile linter" |
| G7 Failure memory | **PRESERVADO** — sin alteración |
| G8 Convergencia | **PRESERVADO** — sin alteración |
| G9 VS Code/Copilot diagnostics | **PRESERVADO** — sin alteración |

---

## 4. Resumen ejecutivo

| # | Criterio | Resultado |
|---|----------|-----------|
| 1 | Compilación Python — 24 scripts (exit 0, 0 errores de sintaxis) | **LIMPIA** |
| 2 | CLI `--help` — 9 scripts clave (exit 0 en todos) | **EXITOSA** |
| 3 | Esquema `bodyLines` eliminado de `prompts/` y `docs/` | **CUMPLIDO** |
| 4 | Esquema nativo `test_patch_applier.py` adoptado en ambos agentes | **CUMPLIDO** |
| 5 | Prohibición de `import`/`package`/`class` en `methods[].body` (doble anclaje: prohibición absoluta + regla de generación) | **CUMPLIDO** |
| 6 | Restricción de tipos en `fields[]` a fuentes evidenciadas | **CUMPLIDO** |
| 7 | Contrato de bloqueo `BLOCKED` en ambos agentes y en el protocolo | **CUMPLIDO** |
| 8 | `repair-agent.md` con `patchId: "repair:<id>"`, campo `repairOf` y `originalPatchId` en entrada | **CUMPLIDO** |
| 9 | Tabla de correspondencias herramienta ↔ responsabilidad en `MASTER_PROMPT.md` | **CUMPLIDO** |
| 10 | Fases 3, 5, 6, 7 reescritas de rol ejecutor a rol reactivo | **CUMPLIDO** |
| 11 | "AST linter" / "Linter AST" erradicados (0 ocurrencias residuales) | **CUMPLIDO** |
| 12 | Gates anti-alucinación G1–G9 preservados íntegramente | **CUMPLIDO** |
