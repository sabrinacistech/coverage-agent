# Dependency Graph Agent (DEPRECATED — Phase 7)

Este agente fue consolidado en `agents/repository-intelligence-agent.md`.
No invocar directamente. Mantenido solo por compatibilidad de pipelines externas.

## Migración

- Antes: el LLM mapeaba dependencias inyectadas (constructor/field/setter) por clase.
- Ahora: leer `state/dependency-graph.json` producido por `tools/python/dependency_graph_extractor.py`.

Ver `docs/optimization-roadmap.md` Phase 7.
