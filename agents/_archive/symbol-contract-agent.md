# Symbol Contract Agent (DEPRECATED — Phase 7)

Este agente fue consolidado en `agents/repository-intelligence-agent.md`.
No invocar directamente. Mantenido solo por compatibilidad de pipelines externas.

## Migración

- Antes: el LLM derivaba contratos de símbolos por SUT vía inspección de bytecode/AST.
- Ahora: leer `state/symbol-contracts/<fqcn>.json` producidos por `tools/python/bytecode_scanner.py` y `tools/python/source_symbol_enricher.py` (orquestados por `run_pipeline.py`).

Ver `docs/optimization-roadmap.md` Phase 7.
