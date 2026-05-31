# Classification Agent (DEPRECATED — Phase 7)

Este agente fue consolidado en `agents/repository-intelligence-agent.md`.
No invocar directamente. Mantenido solo por compatibilidad de pipelines externas.

## Migración

- Antes: el LLM clasificaba clases por testabilidad, riesgo y prioridad.
- Ahora: leer `state/classification-index.json` producido por `tools/python/classification_analyzer.py`.

Ver `docs/optimization-roadmap.md` Phase 7.
