# Agents

Catálogo canónico de los agentes vivos del sistema, ordenados por la fase del
ciclo en la que actúan. Las fases puramente deterministas (discovery,
classification, dependency graph, symbol contract, stack profile, planning,
fixtures, validation, repo intelligence, mutation hardening, reporting)
**no tienen agente LLM**: el orquestador invoca directamente `tools/python/*`
y consume `state/*.json`.

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

> El trabajo de las fases deterministas lo hacen los módulos `tools/python/*`
> listados en la tabla "Mapa de fases"; no hay agentes LLM para esas fases.
