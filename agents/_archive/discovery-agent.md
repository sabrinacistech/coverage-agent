# Discovery Agent (DEPRECATED — Phase 7)

Este agente fue consolidado en `agents/repository-intelligence-agent.md`.
No invocar directamente. Mantenido solo por compatibilidad de pipelines externas.

## Migración

- Antes: el LLM inspeccionaba el repo, detectaba build tool y producía descubrimiento cualitativo.
- Ahora: leer `state/build-tool-contract.json`, `state/archetype-profile.json` y `state/generated-code-index.json` producidos por `tools/python/run_pipeline.py` (que orquesta `pom_parser.py`, `archetype_detector.py` y `generated_code_scanner.py`).

Ver `docs/optimization-roadmap.md` Phase 7.
