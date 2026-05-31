# Arquitectura v2 — orquestación autónoma

> Estado: **en construcción**. Esta capa se monta sobre el núcleo determinista
> (`tools/python`, gates G1-G8, presupuesto por construcción) sin reescribirlo.
> El baseline previo está etiquetado como `v0-legacy`.

## Por qué

El sistema v1 era un pipeline determinista completo, pero **no tenía un cliente
LLM autónomo in-tree**: las fases 8 (generación) y 10b (repair-LLM) las disparaba
a mano un orquestador externo (Claude Code) cargando prompts markdown.
`cycle_loop.py` ya sabía orquestar un ciclo y recibe un *comando "un ciclo"*
(generación→patch→validación), pero ese comando no existía. La v2 lo construye.

## El stack (qué habilita cada pieza)

| Pieza | Rol | Dónde |
|-------|-----|-------|
| **LiteLLM** (SDK) | Gateway único a modelos con control central de costo/tokens | `orchestrator/llm_gateway.py` |
| **LangChain** | Prompts (`agents/*.md` → templates) y abstracción de tools | `orchestrator/prompts.py`, `orchestrator/nodes.py` |
| **LangGraph** | Orquestación, estado, memoria, workflow del ciclo | `orchestrator/graph.py`, `orchestrator/state.py` |
| **FastAPI** | Exponer el servicio (arrancar/consultar runs) | `app/main.py` |
| **Langfuse** *(opcional)* | Observabilidad + prompt management | hooks tras `LANGFUSE_ENABLED` |

## Regla de oro

**LangGraph orquesta; NO adjudica gates.** Los gates G1-G8 y el presupuesto
(maxCycles / maxMinutes / maxTokensIn) siguen siendo Python determinista que
bloquea con exit codes. El LLM solo produce un `patch-descriptor` JSON validado
contra `state/_schemas/protocols/patch-descriptor.schema.json`; nunca se le
delega "¿pasó el gate?". Cada fase reutiliza el módulo Python existente — no se
reimplementa lógica de gates/budget (una sola definición de cada cosa).

## Módulos del núcleo reutilizados (sin tocar)

- `tools/python/cycle_loop.py` — dueño del ciclo: tickea budget, checkea
  token-budget antes del dispatch, escribe los campos G8 y evalúa `gate_g8`.
- `tools/python/budget_enforcer.py` — `tick` / `check` / `check_token_budget` / `reset`.
- `tools/python/gate_runner.py` — `gate_g1`..`gate_g8` (funciones importables).
- `tools/python/test_patch_applier.py` — aplica el patch atómicamente (CLI).
- `tools/python/repair_dispatch.py` + `ast_patcher.py` — repair determinista (10a).
- `tools/python/run_pipeline.py` — fase 0 (pre-stage, 16 pasos).

## Milestones

- **M0** — repo + backup `v0-legacy` + scaffolding. ✅
- **M1** — driver LLM autónomo: `llm_gateway` + `one_cycle`, orquestado por `cycle_loop`.
- **M2** — grafo LangGraph (recursión gobernada por budget + G8).
- **M3** — fachada FastAPI.
- **M4** — Langfuse (opcional).

## Entorno

Python **3.11–3.12** (LangChain/LangGraph aún no soportan 3.14). Crear venv con
`py -3.12`. Dependencias en `pyproject.toml`. Secretos por `.env` (ver `.env.example`).
