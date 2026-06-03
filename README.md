# Coverage Agent

> **v2 en construcción** — se está montando una capa de orquestación autónoma
> (**LiteLLM** gateway + **LangChain** prompts/tools + **LangGraph** workflow,
> **Langfuse** opcional) sobre el núcleo determinista descrito abajo, **sin
> reescribirlo**. Ver [`docs/v2-architecture.md`](docs/v2-architecture.md). El
> baseline determinista previo está etiquetado como **`v0-legacy`**.
>
> 🚀 **¿Cómo correrlo desde cero?** → [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
> (guía para equipos: VS Code + Claude Code, sin API key).

---

## Núcleo determinista (v1)

Arquitectura de agentes y skills para generar tests unitarios en microservicios Java con **cero invención de paquetes/clases**, soportando Java 8+, Maven/Gradle, JUnit 4/5, Mockito, AssertJ, JaCoCo y proyectos con FreeBuilder/Lombok/MapStruct/Immutables/AutoValue.

## Principio central

> El agente no inventa símbolos. Solo puede usar clases, imports, constructores, métodos, builders, fixtures y comandos verificados con `evidence-id`. Si no hay evidencia, no se genera el test.

## Flujo

```text
discovery → stack-profile → classification → symbol-contract
        → dependency-graph → fixtures → planning → generation
        → validation → repair → reporting
```

> **Nota (post-audit 2026-05-28):** las fases `discovery → planning` ya **no son
> turnos LLM** — corren como pre-stage determinista en `run_pipeline.py` y se
> validan en un único gate (`validate_handoff.py`). El LLM solo ejecuta
> `generation` y `repair`. `reporting` es Python determinista
> (`cycle_report_builder.py`). Ver [`BOOT.md`](BOOT.md) §Procedimiento.

## Estructura

```text
agents/              Agentes por fase (orchestrator, test-intent, test-body, repair, reporting, ...)
skills/              Procedimientos accionables por dominio
state/_schemas/      JSON Schemas Draft-07 (validación obligatoria) — único contenido versionado bajo state/
docs/                Notas de arquitectura y políticas
tools/python/        Pre-stage determinista (parsea POM/classpath/javap/JaCoCo)
MASTER_PROMPT.md     Prompt principal con gates G1–G9
```

Los **artefactos generados** (JSONs deterministas, context-packs, summaries, patches, caches,
`jacoco-baseline.xml`) se escriben **fuera del proyecto objetivo**, en una carpeta hermana con la
convención **`coverage_<nombre-proyecto>`**. Ejemplo: si el proyecto es `C:\repo\proyectox`, el
estado va a `C:\repo\coverage_proyectox`. Los **tests generados sí se escriben dentro del proyecto**
(`<proyecto>/src/test/java/…`); la arquitectura **nunca** toca `src/main`. Aplica igual si la
generación la dispara Copilot (handoff por archivo) o el orquestador LangGraph (v2). El path se
controla con `--out` (pre-stage) / `--state` (patcher y runner). Ver el paso a paso en
[§Ejecución desde VS Code + Copilot](#ejecución-desde-vs-code--copilot-paso-a-paso).

## Pre-stage Python (obligatorio)

Antes de cualquier ciclo LLM, correr el pipeline determinista que produce todos los `state/*.json`. Esto **reduce drásticamente los tokens** consumidos y acelera la generación (ver [`docs/performance-tuning.md`](docs/performance-tuning.md) y [`docs/python-pipeline.md`](docs/python-pipeline.md)).

```bash
mvn -q test jacoco:report     # clases compiladas + jacoco.xml (objetivos + baseline del delta)
python tools/python/run_pipeline.py \
   --repo C:/repo/proyectox \
   --out  C:/repo/coverage_proyectox \
   --module <module> \
   --include-fqcn '^com\.acme\.' \
   --jacoco-xml C:/repo/proyectox/target/site/jacoco/jacoco.xml
```

> `--out` apunta **fuera** del proyecto (`coverage_<proyecto>`); los tests, en cambio, se escriben
> dentro del proyecto. Paso a paso completo con Copilot en
> [§Ejecución desde VS Code + Copilot](#ejecución-desde-vs-code--copilot-paso-a-paso).

## Ejecución desde VS Code + Copilot (paso a paso)

Escenario: en VS Code tenés abierta **esta arquitectura** (`coverage-agent`) como workspace, y
disparás la cobertura de un proyecto externo con **un prompt en Copilot Chat**. Todo lo que genera
la arquitectura queda **fuera** del proyecto; solo los tests se escriben **dentro** del proyecto.

### Convención de carpetas

```text
C:\repo\proyectox            ← proyecto objetivo (SUT). Acá se escriben SOLO los tests (src/test/java).
C:\repo\coverage_proyectox   ← carpeta de estado de la arquitectura (todo lo demás). Externa, borrable.
C:\repoVC\coverage-agent     ← workspace abierto en VS Code (esta arquitectura; tools/python en la raíz).
```

Regla: `--repo` = el proyecto · `--out`/`--state` = la carpeta `coverage_<proyecto>` externa.

### Prerrequisitos (una vez, en el proyecto objetivo)

El pre-stage necesita clases compiladas y un reporte JaCoCo (de los tests existentes) para calcular
los objetivos de cobertura y el baseline del delta:

```bash
cd C:\repo\proyectox
mvn -q test jacoco:report        # genera target/classes + target/site/jacoco/jacoco.xml
```

Y las dependencias Python de la arquitectura (una vez): `pip install -r tools/python/requirements.txt`.

### Prompt para Copilot Chat (el disparador)

Pegá esto en Copilot Chat con `coverage-agent` como workspace, reemplazando las 3 variables del
encabezado:

```text
Actuá como Orchestrator de la arquitectura coverage-agent (este workspace).

PROYECTO   = C:\repo\proyectox                 # repo objetivo; los tests se escriben acá
ESTADO     = C:\repo\coverage_proyectox        # carpeta EXTERNA; todo lo que genere la arquitectura va acá
MODULO     = <artifactId-del-modulo>           # si es mono-módulo y el repo ES el módulo, usar "."

Reglas duras:
- TODO archivo intermedio (context-packs, batch-plan, baseline, summaries, patches) va a ESTADO, nunca dentro de PROYECTO.
- Los tests se escriben dentro de PROYECTO/src/test/java. NUNCA tocar src/main ni el pom.
- No inventes imports, clases, constructores, métodos ni builders: usá solo evidenceIds del context-pack. Respetá los gates G1–G8.
- Cuerpo de test obligatorio con // given, // when, // then.

Pasos:
1) Pre-stage determinista (sin LLM):
   python tools/python/run_pipeline.py --repo "%PROYECTO%" --out "%ESTADO%" --module %MODULO% --jacoco-xml "%PROYECTO%\target\site\jacoco\jacoco.xml"
2) Leé "%ESTADO%\batch-plan.json". Para cada target, abrí su context-pack en "%ESTADO%\context-packs-compact\<FQCN>.json".
3) Por cada SUT, generá el cuerpo de los métodos @Test citando evidenceIds; armá el patch JSON y aplicalo:
   python tools/python/test_patch_applier.py --patch <patch.json> --repo "%PROYECTO%" --state "%ESTADO%" --templates templates --context-pack "%ESTADO%\context-packs-compact\<FQCN>.json" --whitelist "%ESTADO%\import-whitelist.json" --out "%ESTADO%\generated-tests.json"
   (si un gate bloquea, corregí y reintentá; no escribas a la fuerza)
4) Compilá y corré los tests generados (varios --test-class para batch):
   python tools/python/narrow_test_runner.py --repo "%PROYECTO%" --state "%ESTADO%" --module %MODULO% --test-class <FQCN_Test_1> --test-class <FQCN_Test_2>
5) Informá: tests escritos, gates, y resultado de la corrida.
```

> Mono-módulo: si el `pom.xml` del proyecto está en la raíz de `PROYECTO` (no hay reactor padre),
> usá `MODULO = .` — en `narrow_test_runner` eso selecciona el proyecto actual (`-pl .`).
>
> Alternativa autónoma (v2): la misma corrida puede orquestarla la capa **LangGraph**
> (`orchestrator/`) en lugar del handoff por Copilot — la convención de carpetas (estado externo,
> tests dentro del proyecto) es idéntica. Ver [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

### Qué queda dónde, al terminar

| Ubicación | Contenido |
|-----------|-----------|
| `C:\repo\coverage_proyectox\` | `batch-plan.json`, `context-packs-compact/`, `symbol-contracts/`, `import-whitelist.json`, `jacoco-baseline.xml`, `_summaries/`, `generated-tests.json`, caches |
| `C:\repo\proyectox\src\test\java\…` | **solo** los `*Test.java` generados |

Para limpiar una corrida basta con borrar `C:\repo\coverage_proyectox`; el proyecto objetivo queda
intacto salvo por los tests. Detalle de gates/diagnósticos Copilot en
[`docs/vscode-copilot-execution-guide.md`](docs/vscode-copilot-execution-guide.md).

## BGBA archetypes

Detección automática de `bgba-parent-pom`, `bgba-parent-paas-java-8` y `bgba-parent-paas-java-21` con reglas derivadas (`javax` vs `jakarta`, JaCoCo heredado vs manual, JUnit 4 vs 5). Ver [`docs/archetype-policy.md`](docs/archetype-policy.md) y la skill [`archetype-detection`](skills/01-discovery/archetype-detection.md).

## Código autogenerado

CXF (`wsdl2java`), OpenAPI Generator, Lombok, FreeBuilder, MapStruct, Immutables y AutoValue se detectan y excluyen del universo de SUT vía [`generated-code-exclusion`](skills/01-discovery/generated-code-exclusion.md) → `state/generated-code-index.json`.

## Gates anti-alucinación

| Gate | Qué bloquea |
|------|-------------|
| G1 | Imports fuera de `import-whitelist.json` |
| G2 | Símbolo sin `evidence-id` en contrato |
| G3 | Contratos derivados de regex (forzar bytecode/AST) |
| G4 | Generated sources no indexados con APs declarados |
| G5 | Generation sin `stack-profile.json` válido |
| G6 | Static pre-compile linter sobre el test |
| G7 | Re-aplicación de fix ya fallido |
| G8 | Convergencia (delta=0 dos ciclos o compile-fail-rate > 0.5) |
| G9 | Diagnósticos JDT/compilación normalizados (sin inferencia libre) |

> G4 está reportado como `NOT_IMPLEMENTED` por `gate_runner.py` (pendiente). El
> resto de los gates se hacen cumplir de forma determinista — G1/G2/G5 también
> dentro de `test_patch_applier.py`, que es el único punto que escribe Java.

## Modos

- `coverage` — maximiza líneas.
- `branch-coverage` — maximiza ramas y caminos.
- `mutation-hardening` — endurece tests con PIT sobre mutantes sobrevivientes.

## Validación de estados

Todos los `<state-dir>/*.json` validan contra schemas en `state/_schemas/` (dentro del repo,
no movibles). Escritura atómica (`*.tmp` + rename) y hashes SHA-256 en `<state-dir>/execution-state.json`.

## VS Code + GitHub Copilot

Para ejecutar desde Visual Studio Code, usar la guía [`docs/vscode-copilot-execution-guide.md`](docs/vscode-copilot-execution-guide.md) y las instrucciones de proyecto en `.github/copilot-instructions.md`. La arquitectura ahora incluye controles específicos para diagnósticos JDT/Copilot: imports no resueltos, `new Interface()`, uso directo de `Type_Builder` y setters/métodos inventados.

## Cómo arrancar

1. Leer [`BOOT.md`](BOOT.md) — punto único de entrada (parámetros, Phase 0, reglas duras, procedimiento).
2. Leer `MASTER_PROMPT.md` — contrato técnico (gates G1–G9, schemas, división del trabajo).
3. Correr el pre-stage Python apuntando `--out` a la carpeta externa `coverage_<proyecto>` (auto-detección: `python tools/python/bootstrap.py --repo <proyecto> --out C:/repo/coverage_<proyecto>`; overrides manuales con `run_pipeline.py`). Flujo VS Code + Copilot completo en [§Ejecución desde VS Code + Copilot](#ejecución-desde-vs-code--copilot-paso-a-paso).
4. Ejecutar Orchestrator con `mode` y `budget` pegando `BOOT.md` en el chat (o cargándolo desde el agente).
5. Validar cada test generado con `tools/python/test_linter.py` antes de compilar.
6. Inspeccionar `state/execution-state.json` y los `state/_summaries/cycle-*.json` para progreso.
7. Reporte final emitido determinísticamente por `tools/python/cycle_report_builder.py` (Python determinista; no requiere turno LLM).

Para detalles operativos del día a día, ver [`docs/developer-guide.md`](docs/developer-guide.md).
