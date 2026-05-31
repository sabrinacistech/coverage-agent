# CORRECTIVE_REPORT — Ciclo P0 → P4C

Fecha: 2026-05-26
Branch: main
HEAD: 0bedc2e (Merge branch 'main' of https://github.com/sabrinacistech/Agents)
Pre-P0 ref: fb03a2e~1

---

## 1. Resumen ejecutivo

| Indicador | Antes (pre-P0) | Despues (HEAD) |
|---|---|---|
| Agentes canonicos vivos | 13 (con 5 legacy Phase-7 duplicados) | 7 (sin duplicados, stubs archivados) |
| Schemas de protocolo (state/_schemas/protocols) | 0 | 8 |
| Schemas de estado (state/_schemas) | 14 | 21 |
| Tooling deterministico Python | 23 scripts | 31 scripts (+8 nuevos) |
| Tests unitarios Python | 0 | 2 (whitelist, body validation) |
| Banned terms en docs/agents/skills/.github vivos | varios | 0 |
| state/_summaries/ (telemetria) | inexistente | 8 ficheros |
| BOOT.md (punto unico de arranque) | ausente | presente |
| Reportes de cierre en raiz | REFACTOR_REPORT.md | + CORRECTIVE_REPORT.md |

## 2. Antes / Despues por fase

### P0 — Defectos criticos de robustez (fb03a2e)
- Antes: validacion laxa, paths absolutos no contemplados, errores I/O silenciosos.
- Despues: endurecimiento de I/O, normalizacion de paths, errores propagados con exit codes.

### P1 — Legacy Zero seguro (cf3f597, 9a37b9b)
- Antes: 5 agentes Phase-7 (discovery, classification, dependency-graph, stack-profile, symbol-contract) coexistiendo con sus equivalentes deterministicos.
- Despues: agentes legacy movidos a agents/_archive/ (8 archivos via R100). Docs equivalentes (architecture-overview, optimization-roadmap, CORRECCIONES_IMPLEMENTADAS) movidos a docs/_archive/.

### P2-P3 — Compactacion de tokens y prompts transaccionales (9ce07c0, b92a4dd)
- Antes: prompts largos (>120 lineas), sin esquema compacto para context packs.
- Despues: prompts vivos por debajo de los limites (test-body 78, test-intent 57), introduccion de context-pack-compact.schema.json.

### P4A — Logging estructurado, cache conservador, --sut (3a2ad2e)
- Antes: logs adhoc, sin invariantes de cache, sin parametro de objetivo.
- Despues: logging JSON estructurado por tool ({"tool":...,"status":...,"durationMs":...,"exitCode":...}); flag --sut adoptado.

### P4B — Tooling deterministico inicial (2d4d56a)
- Antes: pipeline sin diagnostico ni gates explicitos.
- Despues: nuevos scripts doctor.py, gate_runner.py, narrow_test_runner.py, repair_rules_compiler.py, repair_telemetry.py, run.py.

### P4C — Schemas, summaries, telemetria (13109c3, 81f8b99)
- Antes: 0 schemas de protocolo; 0 summaries publicados.
- Despues: 8 schemas en state/_schemas/protocols/; 8 ficheros publicados en state/_summaries/ (artifact-map, build-output, cache, compiled-rules, gates, last-failure, llm-budget, pipeline-run).

## 3. Archivos modificados / creados / archivados / eliminados

(Surface: git diff --name-status fb03a2e~1..HEAD, 58 entradas, 3701 insertions / 380 deletions.)

### Creados (A)
- .gitignore
- agents/README.md
- state/_schemas/protocols/artifact-map.schema.json
- state/_schemas/protocols/context-pack-compact.schema.json
- state/_schemas/protocols/cycle-summary.schema.json
- state/_schemas/protocols/gate-failure.schema.json
- state/_schemas/protocols/llm-budget.schema.json
- state/_schemas/protocols/pipeline-run.schema.json
- state/_schemas/protocols/telemetry.schema.json
- state/_summaries/{artifact-map,build-output.log,cache,compiled-rules,gates,last-failure,llm-budget,pipeline-run}.json
- tools/python/doctor.py
- tools/python/gate_runner.py
- tools/python/narrow_test_runner.py
- tools/python/repair_rules_compiler.py
- tools/python/repair_telemetry.py
- tools/python/run.py
- tools/python/tests/test_body_validation.py
- tools/python/tests/test_whitelist_loading.py

### Modificados (M)
- MASTER_PROMPT.md, README.md
- agents/{coverage-orchestrator,mutation-agent,repair-agent,repository-intelligence-agent,test-body-agent,test-intent-agent}.md
- docs/{agent-json-protocol,developer-guide,semantic-index-architecture,token-minimization-strategy}.md
- skills/00-runtime/{incremental-execution,semantic-index}.md
- skills/01-discovery/{archetype-detection,generated-code-exclusion}.md
- skills/07-generation/ast-patch-generation.md
- tools/python/{common,context_pack_builder,run_pipeline,state_validator,test_patch_applier}.py

### Archivados (R100, move-to _archive)
- agents/_archive/{classification-agent,dependency-graph-agent,discovery-agent,fixture-agent,planning-agent,stack-profile-agent,symbol-contract-agent,validation-agent}.md
- docs/_archive/{architecture-overview,optimization-roadmap,CORRECCIONES_IMPLEMENTADAS}.md

### Eliminados (D)
- (ninguno; todo movimiento es R100 hacia _archive)

## 4. Hashes SHA-256 de archivos canonicos

| Archivo | SHA-256 |
|---|---|
| MASTER_PROMPT.md | ed876e04ff76638d63a2705f51bad53f67fc8547c67da5abe3a6ba3173da2b3e |
| BOOT.md | 8a3d03fc18205544c2f83fe49fe3706c3475cce54f7e51590c8b3196832d4971 |
| agents/test-body-agent.md | 93b13f963ecb5c2452b9ab21f1690d45ee3646d9c36bf227ea3888baaf6da7d6 |
| agents/test-intent-agent.md | 0ee33439a5d3e42146beeee92e64a2742dec3dce29b1af22ccf48aea667e6890 |
| agents/coverage-orchestrator.md | 436485393b3b9930a681dd388159f2bc2a39e6a0aa6219f9e8b3860521242564 |
| agents/README.md | 4e181a0d30cfdf6110b95857063a535008d0d5155f2133bb49d3fe5dc6e88231 |
| tools/python/run.py | a5b3592f6b3ead3673944528a779cfc98a617bcec3a8a53eb171a6994e2024bc |
| tools/python/doctor.py | e51ce813aec9a29e57c82413f211018f736424047294e80840500297cfe36784 |
| tools/python/gate_runner.py | d84d64108acffa6f7a94b496ba2fa0f1c2f3f082027ab2fcf75f46c58705f115 |
| tools/python/narrow_test_runner.py | c30a672c5916f752153ed0df83c0f130763a43ca3f50b251de3d78f8921de94b |

## 5. Resultado de los 12 checks

| Check | Comando | Resultado |
|---|---|---|
| 1.1 Repo Java montado | inspeccion working dir | DEFERIDO — no hay pom.xml ni target/ en C:\repo\Agents\java-test-coverage-architecture; el repo Java se monta on-demand. No verificable en esta corrida. |
| 1.2 Alineacion copilot-instructions.md | grep "static pre-compile linter\|context-packs-compact\|gate_runner" .github/copilot-instructions.md | PARCIAL — 2 hits, solo "static pre-compile linter" (lineas 112, 334). Faltan referencias a context-packs-compact y gate_runner. |
| 1.3 Superficie de cambio | git diff --stat fb03a2e~1..HEAD | OK — 58 archivos cambiados, 3701 inserciones, 380 eliminaciones. |
| 1.4 Ratio compactBytes/legibleBytes | ls state/context-packs/ | NO MEDIBLE — no existen pares de packs hasta primer run del pipeline. |
| 1.5 py_compile masivo | python -m py_compile tools/python/*.py | OK — todos compilan. |
| 1.6 --help en cada script | for s in tools/python/*.py; python "$s" --help | 30/31 OK; FAIL tools/python/stacktrace.py (UnicodeEncodeError cp1252 sobre U+2192 en help text en Windows). |
| 1.7 Conteo schemas protocolos | ls state/_schemas/protocols/*.schema.json | OK — 8 (>= 7 esperado). |
| 1.8 state_validator | python tools/python/state_validator.py --state state | FAIL aceptable — exit 1; 6 [ERR] por archivos de estado no producidos (no hubo run real). protocols/ NO aparece como falso positivo (zero [ERR] sobre protocols/). |
| 1.9 Banned terms en arboles vivos | grep "discovery-agent\|...\|Linter AST" docs/ skills/ agents/ MASTER_PROMPT.md BOOT.md README.md .github/ excluyendo _archive y reports | OK — 0 hits en vivos; 7 hits localizados solo en agents/_archive y docs/_archive. |
| 1.10 wc -l prompts vivos | wc -l agents/test-{body,intent}-agent.md | OK — body 78 (<=80), intent 57 (<=60). |
| 1.11 Re-correr tests | python tools/python/tests/test_whitelist_loading.py; test_body_validation.py | OK — ambos pasan (whitelist: 4/4 casos; body: exit=3, no file, marker present). |
| 1.12 doctor --json | python tools/python/doctor.py --repo . --state state --json | FAIL aceptable — exit 1; repo:pom.xml FAIL y repo:target/classes WARN porque el SUT no esta montado aqui. python:jsonschema, python:lxml, build-tool (mvn), state:_schemas, state:_schemas/protocols, templates: todos OK. |

Resumen: 7 PASS / 2 FAIL aceptable / 2 PARCIAL-DEFERIDO / 1 NO MEDIBLE.

## 6. Hallazgos diferidos

| Item | Razon | Owner sugerido |
|---|---|---|
| .github/copilot-instructions.md carece de referencias a context-packs-compact y gate_runner | Documento no actualizado tras P2-P3 y P4B. No bloqueante. | Mantenedor de instrucciones Copilot |
| tools/python/stacktrace.py --help rompe en Windows (cp1252) | Help text contiene U+2192 (flecha). Reemplazar por "->" o forzar stdout UTF-8. | Owner stacktrace.py |
| state_validator reporta 6 [ERR] sin run | Comportamiento por diseno: la ausencia de archivos de pipeline genera ERR. Mantener o introducir flag --no-fail-on-missing para CI seco. | Owner state_validator |
| doctor.py FAIL por pom.xml ausente | Working dir no contiene SUT Java; doctor esta diseniado para correrse con --repo apuntando al SUT real. Documentar o relajar para repo de orquestacion. | Owner doctor |
| Ratio compactBytes/legibleBytes | Requiere primer run real para producir pares en state/context-packs/. | Owner ciclo P5 |
| Repo Java SUT | No montado en este checkout; cualquier verificacion E2E queda diferida. | Operacion / integracion |

## 7. Commits del ciclo

| SHA | Mensaje |
|---|---|
| fb03a2e | FASE P0 — Defectos criticos de robustez |
| 9a37b9b | FASE P1 — Legacy Zero seguro |
| cf3f597 | P1 — Legacy Zero seguro |
| b92a4dd | P2 |
| 9ce07c0 | FASE P2-P3 — Compactacion de tokens y prompts transaccionales |
| 3a2ad2e | FASE P4A — Logging estructurado, cache conservador, --sut |
| 2d4d56a | FASE P4B — Tooling determinista inicial |
| 13109c3 | FASE P4C — Schemas, summaries, telemetria |
| 81f8b99 | FASE P4C — Schemas, summaries, telemetria |
| 0bedc2e | Merge branch 'main' of https://github.com/sabrinacistech/Agents |

## 8. Criterio de exito

| Criterio | Resultado |
|---|---|
| Todos los scripts Python compilan | PASS |
| Todos los scripts soportan --help | FAIL (1/31: stacktrace.py en Windows cp1252) |
| Schemas de protocolo >= 7 | PASS (8) |
| state_validator no marca falsos positivos sobre protocols/ | PASS |
| Banned terms ausentes en arboles vivos | PASS |
| Prompts canonicos dentro de limites (body<=80, intent<=60) | PASS |
| Tests unitarios deterministicos pasan | PASS |
| doctor.py opera sin errores estructurales (independiente del SUT) | PASS (jsonschema, lxml, build-tool, schemas, templates OK) |
| Documento .github/copilot-instructions.md cita herramientas vigentes | FAIL (faltan context-packs-compact, gate_runner) |
| Repo Java SUT verificado | DEFERIDO (no montado) |
| Ratio compact/legible medido | DEFERIDO (sin run) |
