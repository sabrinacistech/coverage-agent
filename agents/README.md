# Agents

Catálogo canónico de los agentes vivos del sistema, ordenados por la fase del
ciclo en la que actúan. Las fases puramente deterministas (discovery,
classification, dependency graph, symbol contract, stack profile, planning,
fixtures, validation, repo intelligence, mutation hardening, reporting)
**no tienen agente LLM**: el orquestador invoca directamente `tools/python/*`
y consume `state/*.json`. Los stubs históricos viven en `_archive/`.

## Mapa de fases

| Phase                   | Owner                                  | State producido                                                                |
|-------------------------|----------------------------------------|--------------------------------------------------------------------------------|
| Discovery               | `tools/python/pom_parser.py` + `archetype_detector.py` + `generated_code_scanner.py` | `build-tool-contract.json`, `archetype-profile.json`, `generated-code-index.json` |
| Classpath / whitelist   | `tools/python/classpath_resolver.py`   | `import-whitelist.json`                                                        |
| Stack profile           | `tools/python/stack_profile_detector.py` | `stack-profile.json`                                                         |
| Symbol contracts        | `tools/python/bytecode_scanner.py` + `source_symbol_enricher.py` | `symbol-contracts/<fqcn>.json`                       |
| Coverage targets        | `tools/python/jacoco_parser.py --mode targets` | `coverage-targets.json`                                                |
| Semantic index          | `tools/python/semantic_index_writer.py` | `state/index/*.json`                                                          |
| Classification          | `tools/python/classification_analyzer.py` | `classification-index.json`                                                |
| Dependency graph        | `tools/python/dependency_graph_extractor.py` | `dependency-graph.json`                                                 |
| Repository intelligence | `tools/python/repo_intelligence.py`    | `_summaries/repo-intelligence.json` (wrapper determinístico de stack/contracts/index/classification/deps) |
| Fixtures                | `tools/python/fixture_catalog_builder.py` | `fixture-catalog.json`                                                     |
| Planning                | `tools/python/coverage_planner.py`     | `batch-plan.json`                                                              |
| Incremental scope       | `tools/python/incremental_map_writer.py` | `incremental-map.json`                                                       |
| Context packs           | `tools/python/context_pack_builder.py` | `context-packs/<safe_fqcn>.json` (+ `context-packs-compact/` opcional)         |
| State validation        | `tools/python/state_validator.py`      | (gate; sin artifact)                                                           |
| Orchestration           | `coverage-orchestrator.md`             | `execution-state.json`, `_summaries/cycle-<N>.json`                            |
| Test intent             | `test-intent-agent.md` (LLM)           | JSON intent (validado contra schema)                                           |
| Test body               | `test-body-agent.md` (LLM)             | JSON patch descriptor → `tools/python/test_patch_applier.py`                   |
| Pre-compile lint        | `tools/python/test_linter.py`          | gate G6 (sin artifact)                                                         |
| Narrow validation       | `tools/python/narrow_test_runner.py` + `compile_error_parser.py` | `compile-error-index.json`, `coverage-delta.json`, `_summaries/build-output.log` |
| Repair                  | `repair-agent.md` (LLM, sólo escalados) | nuevo JSON patch; el driver Python aplica `repair-rules/*.rules` antes        |
| Mutation hardening      | `tools/python/mutation_runner.py`      | `mutation-intelligence.json` (opt-in vía `--coverage-mode mutation-hardening`; verifica plugin PIT, ejecuta `mvn`, parsea XML) |
| Cycle summary           | `tools/python/cycle_summarizer.py`     | `_summaries/cycle-<N>.json`                                                    |
| Reporting               | `tools/python/cycle_report_builder.py` | `_summaries/cycle-<N>-report.json` (summary, sutReports, gateStatus, recommendations) |

## Agentes vivos (archivos `.md` en este directorio)

| Archivo                            | Capa     | Notas                                                                                  |
|------------------------------------|----------|----------------------------------------------------------------------------------------|
| `coverage-orchestrator.md`         | control  | Único agente con autoridad para avanzar fases (incluye tabla "Phase → Tool → State").  |
| `test-intent-agent.md`             | LLM      | Emite JSON-only intent. Prompt mínimo (≤60 líneas tras P3).                            |
| `test-body-agent.md`               | LLM      | Emite JSON-only patch descriptor. Prompt mínimo (≤80 líneas tras P3).                  |
| `repair-agent.md`                  | LLM (sólo escalados) | Razonamiento sobre el subset escalado por el driver. No carga ni matchea reglas — eso lo hace `repair_rules_compiler.py` + `ast_patcher.py`. |

## `_archive/`

Stubs DEPRECATED. Sus responsabilidades fueron absorbidas por el pre-stage
Python (`run_pipeline.py` + wrappers determinísticos). Conservados solo para
trazabilidad histórica; no invocar.

| Stub archivado                       | Reemplazo activo                                                |
|--------------------------------------|------------------------------------------------------------------|
| Discovery stub                       | `tools/python/run_pipeline.py` (steps 1–3)                       |
| Classification stub                  | `tools/python/classification_analyzer.py`                        |
| Dependency graph stub                | `tools/python/dependency_graph_extractor.py`                     |
| Symbol contract stub                 | `tools/python/bytecode_scanner.py` + `source_symbol_enricher.py` |
| Stack profile stub                   | `tools/python/stack_profile_detector.py`                         |
| Planning stub                        | `tools/python/coverage_planner.py` + sección "Phase → Tool → State" en `coverage-orchestrator.md` |
| Fixture stub                         | `tools/python/fixture_catalog_builder.py`                        |
| Validation stub                      | `tools/python/narrow_test_runner.py` + `compile_error_parser.py` |
| `repository-intelligence-agent.md`   | `tools/python/repo_intelligence.py` (wrapper de los 5 sub-pasos determinísticos) |
| `mutation-agent.md`                  | `tools/python/mutation_runner.py` (PIT runner determinístico, sin LLM) |
| `reporting-agent.md`                 | `tools/python/cycle_report_builder.py` (cálculo aritmético + plantillas, sin LLM) |

> Los archivos físicos correspondientes están en `agents/_archive/` con el
> nombre histórico `<phase>-agent.md`.
