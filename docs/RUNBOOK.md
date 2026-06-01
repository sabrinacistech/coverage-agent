# Runbook — coverage-agent (etapa 1: LLM vía VS Code + Claude Code, sin API key)

Guía paso a paso para correr el agente de cobertura **desde cero** contra un
repositorio Java, generando tests con la ayuda de un LLM accedido desde **VS Code
(Claude Code o GitHub Copilot)** — sin necesidad de una API key.

> Audiencia: cualquier equipo que quiera usar el agente. No asume conocimiento
> previo del sistema.

---

## 1. Qué hace y cómo (en 1 minuto)

El sistema analiza un proyecto Java, decide qué clases/métodos faltan cubrir, y
**genera tests unitarios** para subir la cobertura. El núcleo es **determinista**
(Python): arma el contexto, aplica compuertas de calidad anti-alucinación
(**gates G1–G8**) y controla un **presupuesto** de ciclos/tiempo/tokens. La parte
creativa —escribir el cuerpo del test— la pone un **LLM**.

En **etapa 1** ese LLM es **Claude Code (o Copilot) dentro de VS Code**: el agente
**no llama a ninguna API**. Cuando necesita un test, deja un *pedido* en un archivo
y **espera**; vos le pedís a Claude Code que lo resuelva y escriba la respuesta en
otro archivo (el "handoff"). El agente valida esa respuesta contra un schema y la
aplica solo si pasa los gates.

```
Fase 0 (análisis)  →  ciclo:  [genera pedido] → (Claude Code responde) → aplica+compila+mide  → repite
                                     handoff por archivo                gates G1–G8 + presupuesto
```

---

## 2. Prerrequisitos

| Herramienta | Versión | Para qué |
|---|---|---|
| **Python** | **3.11 o 3.12** (no 3.14) | el agente; LangChain/LangGraph no soportan 3.14 |
| **JDK** | 17+ (21 ok) | compilar/analizar el repo Java (`javap`) |
| **Maven** | 3.9+ | build, tests y reporte JaCoCo del repo objetivo |
| **VS Code + Claude Code** | actual | resolver el handoff (el "LLM" de etapa 1) |
| **git** | actual | clonar repos / versionar |

El repo Java objetivo necesita JaCoCo con **dos propósitos**: la *medición* del
agente (por línea de comandos, sin tocar el POM) y el *gate de despliegue* (JaCoCo
en el build → según arquetipo: heredado en java-21, **plugin en POM requerido** en
java-8). Gobernado por
[`docs/archetype-policy.md`](archetype-policy.md) y
[`skills/01-discovery/jacoco-bootstrap.md`](../skills/01-discovery/jacoco-bootstrap.md)
(ver paso 5.1).

---

## 3. Modelo mental: 3 ubicaciones distintas

| | Qué es | Ejemplo |
|---|---|---|
| **Repo del agente** | este proyecto (`coverage-agent`) | `C:\repo\coverage-agent` |
| **Repo objetivo** | el proyecto Java al que se le generan tests | `C:\repo\multi-clusters\cluster-status-service` |
| **state-dir** | carpeta de trabajo del agente (análisis, handoff, métricas) | `C:\repo\agent-state-<proyecto>` |

> **Elegí el state-dir FUERA de ambos repos** para no ensuciar git.

### ¿Dónde queda cada salida?

| Salida | Ubicación | ¿Se versiona? |
|---|---|---|
| **Tests generados** (suben la cobertura) | `<repo-objetivo>\src\test\java\...` | **Sí**, en el repo objetivo |
| Reporte JaCoCo | `<repo-objetivo>\target\site\jacoco\jacoco.xml` | No (target/) |
| Métricas/deltas | `<state-dir>\coverage-delta.json`, `coverage-targets.json`, `_summaries\` | No |
| Handoff con el IDE | `<state-dir>\_llm\` (`request-*.md/.json`, `response-*.json`, `_done\`) | No |
| Estado del pipeline | `<state-dir>\` (`batch-plan.json`, `context-packs[-compact]\`, `execution-state.json`) | No |

**En resumen:** los **tests** quedan dentro del **repo objetivo** (los commiteás
ahí); las **mediciones y el estado** quedan en el **state-dir**.

---

## 4. Puesta a punto (una vez)

```powershell
# 4.1 Clonar el agente
git clone https://github.com/sabrinacistech/coverage-agent.git
cd coverage-agent

# 4.2 Crear el entorno Python 3.12 e instalar dependencias (incluye la API)
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,api]"

# 4.3 (opcional) Verificar que todo está sano
.\.venv\Scripts\python.exe -m pytest tests\orchestrator tools\python\tests -q
```

> Todos los comandos del agente se corren **desde la raíz de `coverage-agent`**
> (para que resuelva el paquete `orchestrator`).

---

## 5. Correr contra un repo Java — paso a paso

Ejemplo con `multi-clusters` (módulo `cluster-status-service`). Adaptá las rutas.

### 5.1 Preparar el repo objetivo (build + JaCoCo)

JaCoCo cumple **dos propósitos distintos** (ver
[`docs/archetype-policy.md`](archetype-policy.md) §"dos propósitos"):

1. **Medición del agente:** el `jacoco.xml` que la Fase 0 usa para saber qué falta
   cubrir. Se genera por **línea de comandos sin tocar el POM** (opción b).
2. **Gate de despliegue (OpenShift):** el pipeline corre JaCoCo y **bloquea el deploy
   si el branch coverage < 80%**. Para eso JaCoCo debe estar **en el build
   committeado** → según el arquetipo (opción c).

> ⚠️ El agente **nunca** toca `src/main`. La **única** modificación permitida en la
> app es agregar el plugin de JaCoCo al `pom.xml` cuando el arquetipo lo requiere.

**a) Clonar el repo objetivo**

```powershell
git clone https://github.com/sabrinacistech/multi-clusters.git C:\repo\multi-clusters
cd C:\repo\multi-clusters\cluster-status-service
```

**b) Generar el `jacoco.xml` para el agente, SIN tocar el POM** — corré el plugin por
línea de comandos (bootstrap CLI). Sirve para la **medición** de la Fase 0:

```powershell
mvn -q -DfailIfNoTests=false `
  org.jacoco:jacoco-maven-plugin:0.8.13:prepare-agent `
  test `
  org.jacoco:jacoco-maven-plugin:0.8.13:report
# → target\site\jacoco\jacoco.xml (sin cambios en el pom.xml)
```

(Para Gradle, ver el equivalente en `skills/01-discovery/jacoco-bootstrap.md`.)

**c) Asegurar JaCoCo en el POM para el despliegue — SEGÚN EL ARQUETIPO** (ver
[`skills/01-discovery/archetype-detection.md`](../skills/01-discovery/archetype-detection.md)):

| Arquetipo | Acción en el POM |
|---|---|
| **java-21** (`bgba-parent-paas-java-21`) | **Nada** — JaCoCo heredado del parent; prohibido agregarlo. |
| **java-8** (`bgba-parent-paas-java-8`) / sin herencia | **Agregar el plugin (requerido)** para pasar el gate de OpenShift. |

Para el caso java-8, usá el **bloque canónico** (versión 0.8.13 + `report` + `check`
de branch ≥ 0.80) definido —fuente única— en
[`docs/archetype-policy.md`](archetype-policy.md) §"Bloque JaCoCo canónico". Es la
**única** modificación permitida en la app; commiteala en el repo objetivo.

> Si no hay `jacoco.xml`, la Fase 0 produce un `batch-plan` vacío
> ("no uncovered targets") y no hay nada que generar. Ver Troubleshooting.

### 5.2 Fase 0 — análisis (desde la raíz de `coverage-agent`)

```powershell
cd C:\repo\coverage-agent
.\.venv\Scripts\python.exe tools\python\run_pipeline.py `
  --repo C:\repo\multi-clusters\cluster-status-service `
  --out  C:\repo\agent-state-multiclusters `
  --module . `
  --jacoco-xml C:\repo\multi-clusters\cluster-status-service\target\site\jacoco\jacoco.xml `
  --coverage-mode coverage
```

> `--module .` (repo apunta al módulo) es **necesario**: sin él, los
> `symbol-contracts/` quedan vacíos y el handoff se **BLOQUEA**. Para multi-módulo
> con `--repo` en el parent, pasá el nombre del módulo.

Esto deja en el state-dir: `symbol-contracts/` (poblado), `batch-plan.json`,
`context-packs-compact/`, `_summaries/llm-budget.json`, etc.

**Verificá antes de seguir:** la salida termina en `[OK] handoff ready: N contracts…`
y el comando sale con código 0. Si dice `BLOCKED_PRE_STAGE_MISSING` (p.ej.
`symbol-contracts/ (empty)`), la Fase 0 falló — **no arranques el ciclo** hasta
resolverlo (lo más común: faltó `--module .` o `target/classes`).

### 5.3 (opcional) Fijar el presupuesto

Si no creás `execution-state.json`, se crea solo con presupuesto por defecto
(**maxCycles=20**, **maxMinutesPerCycle=10**). Para fijarlo (p.ej. una prueba corta
de 3 ciclos), creá `C:\repo\agent-state-multiclusters\execution-state.json`:

```json
{
  "schemaVersion": 1, "cycle": 0, "phase": "generation",
  "budget": { "maxCycles": 3, "maxMinutesPerCycle": 10 },
  "consecutiveZeroDeltaCycles": 0, "compileFailRateWindow": [], "checkpoints": []
}
```

### 5.4 Arrancar el ciclo

El proveedor de LLM por defecto es **`ide`** (handoff a Claude Code). Para dejarlo
explícito: `$env:COVAGENT_LLM_PROVIDER = "ide"`.

**Opción A — CLI (recomendada para empezar):**

```powershell
$env:COVAGENT_LLM_PROVIDER = "ide"
.\.venv\Scripts\python.exe tools\python\cycle_loop.py `
  --state     C:\repo\agent-state-multiclusters\execution-state.json `
  --state-dir C:\repo\agent-state-multiclusters `
  -- .\.venv\Scripts\python.exe -m orchestrator.one_cycle `
       --state-dir C:\repo\agent-state-multiclusters `
       --repo C:\repo\multi-clusters\cluster-status-service
```

**Opción B — API FastAPI (arranque manual):**

```powershell
$env:COVAGENT_LLM_PROVIDER = "ide"
.\.venv\Scripts\python.exe -m uvicorn app.main:app
# en otra terminal:
#   POST http://127.0.0.1:8000/runs   body: {"repo":"C:\\repo\\multi-clusters\\cluster-status-service","state_dir":"C:\\repo\\agent-state-multiclusters"}
#   GET  http://127.0.0.1:8000/runs/<runId>   → status + coverageDelta + pendingIdeRequest
```

### 5.5 Resolver el handoff con Claude Code

Cuando el agente necesita un test, **se pausa** e imprime (o expone en
`GET /runs/{id}` como `pendingIdeRequest`) la ruta de un archivo:

```
<state-dir>\_llm\request-<cycle>-<rol>.md      ← instrucciones + responsePath
<state-dir>\_llm\request-<cycle>-<rol>.json    ← el prompt (system + contexto compacto)
```

**El paso lo manejás VOS desde la terminal** (no se congela en silencio). Al
pausarse, la terminal imprime instrucciones y queda esperando con un prompt:

```
[handoff] ENTER = ya dejé la respuesta · 'skip' = saltar este target · Ctrl+C = cortar todo >
```

Procedimiento:
1. En el chat de **Claude Code** (VS Code), pedile:
   > *"Leé `<state-dir>\_llm\request-<...>.md` y su `.json` hermano. Generá el
   > patch-descriptor (valida contra `patch-descriptor.schema.json`) y escribí
   > **solo el JSON** en el `responsePath` que indica el request."*
2. Volvé a la terminal y presioná **ENTER**. El agente valida la respuesta contra
   el schema y, si pasa, aplica el test (gates G1–G8 + presupuesto), compila, lo
   corre y recalcula `coverage-delta.json`; luego sigue con el próximo target.
   - Si el JSON es inválido, te avisa y volvés a presionar ENTER tras corregir.
   - **`skip`** salta ese target (lo marca BLOCKED) y avanza.
   - **Ctrl+C** corta todo el run.

> **Regla de UX (etapa 1):** cada paso es **interventable por el usuario desde la
> terminal** — el agente no continúa solo ni se queda mudo. En modo API/background
> (sin TTY) el handoff usa polling con latido + timeout (`COVAGENT_IDE_TIMEOUT`) y
> se resuelve dejando el `response-*.json` (o vía el endpoint de resume).

### 5.6 Fin del run

El ciclo para con un código de salida:

| rc | Significado |
|---|---|
| **0** | DONE — no quedan objetivos por cubrir |
| **2** | Presupuesto agotado (maxCycles / minutos / tokens) |
| **5** | G8 — convergencia estancada (sin progreso o demasiados fallos de compilación) |

### 5.7 Revisar y commitear los resultados

- **Cobertura nueva** → revisá los tests generados en
  `C:\repo\multi-clusters\cluster-status-service\src\test\java\...`, corré
  `mvn test` y commiteálos **en el repo `multi-clusters`** (idealmente en una rama).
- **Mediciones** → `C:\repo\agent-state-multiclusters\coverage-delta.json` y
  `_summaries\`.

---

## 6. Garantías (por qué es seguro dejar que un LLM genere tests)

El LLM **solo propone** un patch; **nunca decide** si entra. Antes de tocar disco,
el escritor sancionado (`test_patch_applier.py`) aplica de forma determinista:

- **G1** imports ⊆ whitelist del context-pack · **G2** todo símbolo citado existe
  (anti-alucinación) · **G5** stack conocido · **G6** linter de calidad
  (post-escritura, con rollback) · **G7** anti-loop de reparación · **G8**
  finitud/convergencia. Más el **presupuesto** (maxCycles / minutos / tokens).

Un patch que falla un gate **no se escribe** (exit 3); fuera de presupuesto, **no
se llama al LLM** (exit 2).

---

## 7. Configuración (variables de entorno)

| Variable | Default | Qué hace |
|---|---|---|
| `COVAGENT_LLM_PROVIDER` | `ide` | `ide` (handoff Claude Code/Copilot) · `litellm` (API, etapa 2) |
| `COVAGENT_IDE_TIMEOUT` | `1800` | segundos que el agente espera la respuesta del IDE |
| `COVAGENT_IDE_DIR` | `<state>\_llm` | carpeta del handoff |
| `COVAGENT_MODEL_GENERATION` / `_REPAIR` | (Claude) | modelo por rol (solo aplica con `litellm`) |

Presupuesto: en `execution-state.json` (ver 5.3).

---

## 8. Troubleshooting

| Síntoma | Causa / solución |
|---|---|
| `batch-plan` vacío, "no uncovered targets" | Falta `jacoco.xml`. Generalo por CLI sin tocar el POM (paso 5.1b) y pasá `--jacoco-xml` a la Fase 0. Ver `skills/01-discovery/jacoco-bootstrap.md`. |
| `Python 3.9+ not found` / errores de langgraph | Usá **Python 3.12** para el venv (`py -3.12 -m venv .venv`). |
| `Maven not found` | Agregá Maven al PATH (o `mvn.cmd` en Windows). |
| El ciclo "se cuelga" | Está esperando el **handoff**: resolvé el `request-*.md` con Claude Code, o subí/ bajá `COVAGENT_IDE_TIMEOUT`. |
| `[BLOCKED] G1_NO_PERIMETER` | Falta el context-pack; re-corré la Fase 0 (genera `context-packs\`). |
| Patch rechazado (exit 3) | El test propuesto viola un gate (import no permitido, símbolo inexistente, lint). Pedile a Claude Code que corrija según el `blockReason`. |
| `target\classes` no existe | Construí el repo objetivo: `mvn -DskipTests package` (o `mvn test`). |

---

## 9. Etapa 2 (futuro): camino autónomo sin humano

Cambiando `COVAGENT_LLM_PROVIDER=litellm` y configurando credenciales del modelo
(Anthropic API, o Amazon Bedrock / Google Vertex vía LiteLLM), el mismo flujo corre
**sin handoff**: el agente llama al modelo por su cuenta. El resto (gates,
presupuesto, LangGraph, FastAPI) no cambia.
