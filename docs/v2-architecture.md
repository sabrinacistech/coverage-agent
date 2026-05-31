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

## Milestones (etapa 1 — LLM por IDE, sin API key)

- **E0** — repo + backup `v0-legacy` + scaffolding. ✅
- **E1.1** — gateway con proveedores: `ide` (handoff por archivo a Claude Code/Copilot,
  default) + `litellm` (autónomo, dormido). ✅
- **E1.2** — LangGraph orquestador; recursión gobernada por budget + G8 reusando las
  funciones de `cycle_loop` (paridad probada). ✅
- **E1.3** — LangChain: mínimo (ver nota abajo). ✅
- **E1.4** — fachada FastAPI de arranque manual. ✅
- **E1.5** — Langfuse (opcional, detrás de `LANGFUSE_ENABLED`). pendiente.

### Nota LangChain (E1.3)
En etapa 1 los prompts se arman con **dicts planos** (`prompts.py`) y funcionan; no se
introduce acoplamiento a `ChatPromptTemplate`/tools de LangChain hasta que aporte valor
real (p.ej. multi-tool en el camino autónomo). Evita inconsistencia "core vs uso real".

## Cómo se accede al LLM en etapa 1 (handoff IDE, sin key)

`COVAGENT_LLM_PROVIDER=ide` (default). En la fase de generación el sistema:
1. Escribe `state/_llm/request-<cycle>-<rol>.md` (+ `.json` con el prompt y el compact-pack).
2. **Queda esperando** (polling, timeout `COVAGENT_IDE_TIMEOUT`).
3. En VS Code le pedís a **Claude Code / GitHub Copilot** que resuelva ese request y
   escriba el JSON del patch en `state/_llm/response-<...>.json`.
4. El sistema valida la respuesta contra el schema y sigue; el patcher aplica con gates +
   presupuesto por construcción. El request/response consumidos se archivan en `_llm/_done/`.

## Uso (arranque manual)

```bash
# 1) Fase 0 (con JaCoCo para tener targets):
python tools/python/run_pipeline.py --repo <java> --out <state> --jacoco-xml <xml> --coverage-mode coverage

# 2a) Vía CLI (cycle_loop conduce one_cycle):
python tools/python/cycle_loop.py --state <state>/execution-state.json --state-dir <state> \
    -- python -m orchestrator.one_cycle --state-dir <state> --repo <java>

# 2b) Vía grafo + API (FastAPI, arranque manual):
uvicorn app.main:app           # luego: POST /runs {repo, state_dir}; GET /runs/{id}
```

## Entorno

Python **3.11–3.12** (LangChain/LangGraph aún no soportan 3.14). Crear venv con
`py -3.12`. Dependencias en `pyproject.toml` (extras: `api` para FastAPI, `observability`
para Langfuse). Secretos por `.env` (ver `.env.example`).
