# REFACTOR_REPORT

**Fecha:** 2026-05-26
**Rama:** main
**Commit inicial:** `ec312e0` (Agrega .gitignore y excluye __pycache__ del repositorio)
**Commits del refactor:** 12 (`c5d3176` → `6f73d49`)
**Continuación de:** [`CORRECCIONES_IMPLEMENTADAS.md`](CORRECCIONES_IMPLEMENTADAS.md) — alineado con su Phase 7 (Consolidación) y reglas 1–12 verificadas.

---

## Resumen ejecutivo

| Indicador | Antes | Después |
|---|---|---|
| Puntos de entrada de arranque | 2 (`MASTER_PROMPT.md` + `Prompt_inicial.md` con duplicación) | 1 (`BOOT.md`) |
| Agentes en `agents/` | 14 (incluye 5 legacy Phase 7 con procedimientos imperativos + duplicados en `prompts/minimal/`) | 14 (5 stubs DEPRECATED + 9 canónicos, fuente única) |
| Directorio `prompts/` | Existía con 4 versiones canónicas no enlazadas | Eliminado |
| Directorios `skills/05-fixture*` | 2 (singular + plural) | 1 (plural, 6 archivos) |
| `docs/implementation-guide.md` (flujo pre-determinista) | Presente | Eliminado |
| `state/*.json` versionados | 22 seeds vacíos `{schemaVersion:1, []}` | 0 — generados en runtime, `.gitkeep` preservado |
| `tools/python/__pycache__/` en disco | 36 `.pyc` | 0 |
| Schemas en `_schemas/` con falsos positivos en validator | 1 (`patch-descriptor`) | 0 (movido a `_schemas/protocols/`) |
| Schema bootstrap auto-detector | Ausente | `tools/python/bootstrap.py` (168 LOC) |
| Schema-constrained output LLM | Documentado en prosa | Esquema JSON Schema canónico vinculado en ambos agentes |
| Repair deterministico-first | Implícito | Documentado con SLO ≥ 70% y telemetría |

---

## Tabla "Antes / Después" por FASE

### FASE A — Consolidación de la Fuente de Verdad

| Sub-fase | Antes | Después | Commit |
|---|---|---|---|
| A1 | `prompts/minimal/` con 4 agentes canónicos no referenciados; `agents/generation-agent.md` (25-may) coexistía con la pareja nueva test-intent + test-body | `agents/{test-body, test-intent, repair, reporting}-agent.md` canónicos (26-may). `prompts/` eliminado. MASTER_PROMPT Phase 8 + Prompt_inicial + docs activos referencian la nueva pareja. | `c5d3176` |
| A2 | 5 agentes Phase 7 (`discovery`, `classification`, `dependency-graph`, `symbol-contract`, `stack-profile`) con procedimientos imperativos LLM (193 líneas combinadas) | 5 stubs DEPRECATED de ≤15 líneas que apuntan a `repository-intelligence-agent.md`, los `state/*.json` y los `tools/python/*.py` equivalentes (35 líneas combinadas) | `08f808a` |
| A4 | `skills/05-fixture/` (singular, 2 archivos) + `skills/05-fixtures/` (plural, 4) | Único `skills/05-fixtures/` (6 archivos) | `af1e905` |
| A5 | `docs/implementation-guide.md` (51 líneas) describía flujo pre-determinista contradictorio con Regla 0 | Archivo eliminado. Operación día-a-día queda en `README.md` + `docs/developer-guide.md` + `MASTER_PROMPT.md` | `4a34132` |
| A6 | `Prompt_inicial.md` (122 líneas) con bloques STANDALONE/EMBEBIDO + `MASTER_PROMPT.md` Phase 0 duplicada | `BOOT.md` (142 líneas) único, sin variantes. `MASTER_PROMPT.md` queda como contrato técnico puro. `README.md#cómo-arrancar` apunta a `BOOT.md` | `96b34dc` |

### FASE B — Determinismo y Tokens

| Sub-fase | Antes | Después | Commit |
|---|---|---|---|
| B1 | Phase 0 requería usuario armando manualmente `--module`, `--include-fqcn`, `--jacoco-xml` | `tools/python/bootstrap.py` infiere los 3 desde POM raíz (parse_pom + groupId), invoca `run_pipeline.py` y emite bloque JSON. `--dry-run` soporta planificación sin ejecutar | `84ff03e` |
| B2 | Patch Descriptor documentado en prosa; sin JSON Schema; sin "Response Format" en los agentes | `state/_schemas/protocols/patch-descriptor.schema.json` con `oneOf [patch \| BLOCKED]`. Bloque "Response Format" al inicio de `test-body-agent.md` y `repair-agent.md`. Sección "Response Format Hint" en `docs/agent-json-protocol.md` | `94142d1` + `6f73d49` (reubicación) |
| B3 | Procedimiento de repair-agent comenzaba directamente con "razonamiento LLM" sobre `compileError` | Orden obligatorio: 1) cargar `repair-rules/*.rules`, 2) match contra `compile-error-index.json`, 3) LLM solo si no hay regla / `escalateToLLM` / repair previo falló. SLO declarado ≥ 70% sin LLM con contadores `repairsByRule`/`repairsByLLM` en `state/telemetry.json` | `676076c` |

### FASE C — Higiene y Velocidad

| Sub-fase | Antes | Después | Commit |
|---|---|---|---|
| C1 | 36 `.pyc` en `tools/python/__pycache__/` (no tracked) | Directorio eliminado físicamente. `.gitignore` ya excluía `__pycache__/` desde el commit `ec312e0` | sin commit (untracked) |
| C2 | 22 archivos `state/*.json` con shape seed `{schemaVersion: 1, <colección vacía>}` versionados | 22 archivos eliminados; `state/.gitkeep` preserva el directorio; `state/*.json` agregado a `.gitignore` (glob no recursivo: `_schemas/` y `index/` quedan fuera); `BOOT.md` documenta la creación en runtime | `852bb20` |
| C3 | `module-progress.json` sin schema; única referencia era un comentario en `state_validator.py` | Schema fugaz creado y luego retirado (`be00bd3` + `6f73d49`): como C2 ya eliminó el JSON y ningún script lo consume, el schema haría que el validator reportara un [ERR] falso. Decisión: rama "borrar JSON" del plan. Comentario en `state_validator.py` queda como documentación | `be00bd3` (add) → `6f73d49` (remove) |
| C4 | Paralelismo del pipeline no documentado | Sección "Trabajo futuro (paralelismo)" en `docs/performance-tuning.md` enumera: pasos independientes ∥, `bytecode_scanner` N-paralelo por FQCN, auditoría del `state/_cache/`. **No** se modifica código | `ed67299` |

### FASE D — Verificación Final

| Sub-fase | Resultado |
|---|---|
| D1.1 `python -m py_compile tools/python/*.py` | **exit 0** (25 scripts incluyendo `bootstrap.py`) |
| D1.2 `python tools/python/<script> --help` para los 10 CLI clave + `bootstrap.py` | **exit 0** en los 10 |
| D1.3 `python tools/python/state_validator.py --state state` | exit 1 **esperado**: 6 [ERR] de archivos requeridos al pipeline (no ejecutado tras C2), 11 [SKIP], 2 [INFO]. **0 falsos positivos** tras la reubicación de `patch-descriptor` y la retirada de `module-progress` |
| D2 `grep -r "Prompt_inicial"` | 0 matches ✅ |
| D2 `grep -r "implementation-guide"` | 0 matches ✅ |
| D2 `grep -r "prompts/minimal"` | 0 matches ✅ |
| D2 `grep -r "05-fixture/"` (singular) | 0 matches ✅ |
| D2 `grep -r "java-analyse-architecture"` | 0 matches ✅ (no existía en el repo) |

---

## Conteo de archivos

| Operación | Cantidad |
|---|---|
| Archivos eliminados (versionados) | **31** (22 state seeds + 5 archivos `prompts/minimal/` antes y `agents/generation-agent.md` + `Prompt_inicial.md` + `docs/implementation-guide.md` + 1 schema retirado) |
| Archivos movidos (`git mv`) | **6** (4 `prompts/minimal/*` → `agents/`, 2 `skills/05-fixture/*` → `skills/05-fixtures/`) + 1 schema reubicado (`patch-descriptor.schema.json` → `protocols/`) = **7** |
| Archivos creados | **6** (`BOOT.md`, `tools/python/bootstrap.py`, `state/_schemas/protocols/patch-descriptor.schema.json`, `state/_schemas/protocols/README.md`, `state/.gitkeep`, `REFACTOR_REPORT.md`) |
| Archivos modificados | **15** (`MASTER_PROMPT.md`, `README.md`, `.gitignore`, `agents/{discovery,classification,dependency-graph,symbol-contract,stack-profile,repair,reporting,test-body}-agent.md`, `docs/{archetype-policy,developer-guide,performance-tuning,agent-json-protocol}.md`, `state/_schemas/generated-tests.schema.json`) |
| Archivos físicos eliminados sin commit | **36** (`.pyc` en `tools/python/__pycache__/`) |

---

## Hashes SHA-256 de los archivos canónicos finales

| Archivo | LOC | SHA-256 |
|---|---|---|
| `MASTER_PROMPT.md` | 248 | `bbf33f5872bde6885456cf6a61fc7dc5029eb13c536e96a6239a4036d277ee37` |
| `BOOT.md` | 142 | `8a3d03fc18205544c2f83fe49fe3706c3475cce54f7e51590c8b3196832d4971` |
| `agents/test-body-agent.md` | 181 | `1ec3e0700923ec768407e1edaadd0a413bcca4badfc620469158ce334b9103bc` |
| `agents/test-intent-agent.md` | 137 | `8fdca0f7e8c30ed65499be8fcb2147ab0ef5b0efaf8db87eba8c1a2b53aa8c3d` |
| `agents/repair-agent.md` | 226 | `4d280462a03a654f99af1f33887b95c4dff0c8edb4007894d0d316b7d73af60d` |
| `agents/reporting-agent.md` | 219 | `d1e44e62d82582a9728e088215bad86db2c1b7f52d8888955b944dfb8332e429` |

---

## Verificación de gates G1–G9

```
$ grep -c "G[1-9]" MASTER_PROMPT.md
12
```

(9 definiciones de gates + 3 referencias cruzadas.)

| Gate | Estado en `MASTER_PROMPT.md` |
|---|---|
| G1 Import whitelist | **PRESERVADO** literal |
| G2 Symbol evidence | **PRESERVADO** literal |
| G3 Bytecode-first | **PRESERVADO** literal (incluye nota AST-fallback) |
| G4 Generated sources | **PRESERVADO** literal |
| G5 Stack profile | **PRESERVADO** literal |
| G6 Linter pre-compile | **PRESERVADO** literal (texto "static pre-compile linter") |
| G7 Failure memory | **PRESERVADO** literal |
| G8 Convergencia | **PRESERVADO** literal |
| G9 VS Code/Copilot diagnostics | **PRESERVADO** literal |

Ningún gate fue relajado, renombrado, fusionado o reescrito durante el refactor.

---

## Cambios diferidos intencionalmente

| Ítem | Razón del diferimiento | Documento |
|---|---|---|
| Paralelismo en `run_pipeline.py` (pasos 2/3/4 en paralelo) | Excede el alcance "estructura/documentación" definido en las reglas duras del refactor (regla 1: no modificar `tools/python/*.py` salvo cambios explícitos) | `docs/performance-tuning.md` → sección **Trabajo futuro (paralelismo)** ítem 1 |
| `bytecode_scanner` N-paralelo por FQCN | Idem. Requiere multiprocessing pool + mitigación de contención sobre `state/_cache/` | `docs/performance-tuning.md` ítem 2 |
| Auditoría del cache `state/_cache/` (`cache_audit.py`) | Tool nuevo no listado en el plan original; queda como follow-up | `docs/performance-tuning.md` ítem 3 |
| Telemetría real del SLO repair determinista | El contrato JSON está documentado en `agents/repair-agent.md`. La instrumentación efectiva debe vivir en el orchestrator o en una utilidad nueva | `agents/repair-agent.md` → sección **Telemetría** |
| Limpieza adicional de `docs/optimization-roadmap.md` (referencias históricas a `generation-agent`) | Documento histórico de roadmap; las referencias quedan como registro de Phase 4-5. No rompe ningún flujo activo | — |

---

## Coherencia con `CORRECCIONES_IMPLEMENTADAS.md`

- Las 12 correcciones documentadas en `CORRECCIONES_IMPLEMENTADAS.md` permanecen vigentes: este refactor opera *sobre* esa base, no la revierte.
- El esquema `bodyLines` continúa ausente (verificado: el patch descriptor del nuevo schema no incluye el campo).
- El contrato de bloqueo `{ status: "BLOCKED", blockReason }` se eleva a un branch explícito del JSON Schema (`oneOf`).
- Los gates G1–G9 documentados en CORRECCIONES sección 7.4 siguen idénticos.

---

## Commits del refactor

```text
c5d3176 A1: Unifica agentes canónicos bajo agents/ y elimina prompts/minimal
08f808a A2: Convierte 5 agentes legacy Phase 7 en stubs DEPRECATED
af1e905 A4: Consolida skills/05-fixture* en skills/05-fixtures (plural)
4a34132 A5: Elimina docs/implementation-guide.md (flujo pre-determinista)
96b34dc A6: Crea BOOT.md como punto unico de arranque
84ff03e B1: Auto-deteccion de parametros con tools/python/bootstrap.py
94142d1 B2: Schema-constrained output del LLM (Patch Descriptor)
676076c B3: Repair deterministico primero + telemetria SLO
852bb20 C2: Despublica los 22 state/*.json (seeds vacios)
be00bd3 C3: Anade schema state/_schemas/module-progress.schema.json
ed67299 C4: Documenta paralelismo pendiente en performance-tuning.md
6f73d49 D1 fix: Aisla schemas de protocolo del scan de state_validator
```

---

## Criterio de éxito

| Criterio | Resultado |
|---|---|
| 0 referencias rotas tras el refactor | ✅ (D2: 5/5 grep → 0 matches) |
| 24+ scripts Python compilan exit 0 | ✅ (D1.1: 25 scripts, incluyendo el nuevo `bootstrap.py`) |
| Un único punto de entrada de arranque | ✅ (`BOOT.md`) |
| Un único lugar canónico para cada agente | ✅ (`agents/`) |
| Gates G1–G9 preservados literalmente | ✅ (texto idéntico, 12 ocurrencias de `G[1-9]`) |
| `REFACTOR_REPORT.md` generado y consistente con `CORRECCIONES_IMPLEMENTADAS.md` | ✅ (este documento) |
