# Stack Profile Agent (DEPRECATED — Phase 7)

Este agente fue consolidado en `agents/repository-intelligence-agent.md`.
No invocar directamente. Mantenido solo por compatibilidad de pipelines externas.

## Migración

- Antes: el LLM detectaba versiones exactas de JUnit, Mockito, AssertJ, Spring, FreeBuilder, etc.
- Ahora: leer `state/stack-profile.json` producido por `tools/python/stack_profile_detector.py`.

Ver `docs/optimization-roadmap.md` Phase 7.
