# Coverage Agent

> **v2 en construcción** — se está montando una capa de orquestación autónoma
> (**LiteLLM** gateway + **LangChain** prompts/tools + **LangGraph** workflow,
> **Langfuse** opcional) sobre el núcleo determinista descrito abajo, **sin
> reescribirlo**. Ver [`docs/v2-architecture.md`](docs/v2-architecture.md). El
> baseline determinista previo está etiquetado como **`v0-legacy`**.
>
> 🚀 **¿Cómo correrlo desde cero?** → [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
> (guía para equipos: VS Code + Claude Code, sin API key).
>
> 📦 **Generación por batches (proyectos grandes)** → [`docs/batch-handoff.md`](docs/batch-handoff.md):
> `--generation-mode handoff-batch --batch-size 10`, repair solo para fallidos,
> estado persistente por target, y el budget pausado durante el handoff manual.

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

Los **artefactos generados y los reportes** (JSONs deterministas, context-packs, summaries,
`_summaries/analysis-report.md`, patches, caches, `jacoco-baseline.xml`) se escriben **fuera del
proyecto objetivo Y fuera de esta arquitectura**, en una carpeta externa con la convención
**`coverage_<nombre-proyecto>`**. Ejemplo: si el proyecto es `C:\repo\proyectox`, el análisis va a
`C:\repo\coverage_proyectox`. Los **tests generados sí se escriben dentro del proyecto**
(`<proyecto>/src/test/java/…`); la arquitectura **nunca** toca `src/main`. Aplica igual si la
generación la dispara Copilot (handoff por archivo) o el orquestador LangGraph (v2). El path se
controla con `--out` (pre-stage) / `--state` (patcher y runner). Ver el paso a paso en
[§Ejecución desde VS Code + Copilot](#ejecución-desde-vs-code--copilot-paso-a-paso).

## Modelo de uso: determinista (consola) + agéntico (Copilot/VS Code)

La corrida tiene **dos partes bien separadas**:

1. **Parte determinista — desde consola, SIN agente.** Un único comando
   (`tools/python/run_all_deterministic.py`) ejecuta todo el pre-stage: verifica
   JaCoCo, corre Maven y deja la Fase 0 con el handoff `READY`. No consume tokens
   ni necesita Copilot/Claude Code. **Es más rápido y barato** que pedirle a un
   agente que apruebe comando por comando.
2. **Parte agéntica — desde Copilot o VS Code.** El LLM solo ejecuta `generation`
   y `repair` consumiendo los artefactos que dejó la parte 1 (ver
   [§Ejecución desde VS Code + Copilot](#ejecución-desde-vs-code--copilot-paso-a-paso)).

## Quickstart: prueba desde cero con generación batch

Este es el flujo recomendado para probar el agente desde cero contra un repo Java
local. Ejemplo real usando Git Bash:

```bash
cd /c/repoVC/coverage-agent
```

### 1. Fase determinística limpia

Esto borra y reconstruye el `state-dir`, genera JaCoCo, contracts, context packs,
`batch-plan.json` y el reporte consolidado. No usa LLM.

```bash
python tools/python/run_all_deterministic.py \
  --repo /c/repoVC/multi-clusters/cluster-status-service \
  --state-dir /c/repoVC/coverage_cluster-status-service \
  --module . \
  --clean
```

Salidas principales:

```text
/c/repoVC/coverage_cluster-status-service/_summaries/analysis-report.md
/c/repoVC/coverage_cluster-status-service/batch-plan.json
/c/repoVC/coverage_cluster-status-service/context-packs-compact/
```

Verificá antes de seguir:

```bash
cat /c/repoVC/coverage_cluster-status-service/_summaries/analysis-report.md
cat /c/repoVC/coverage_cluster-status-service/batch-plan.json
```

### 2. Primer batch de generación

Para calibrar, arrancá con un batch chico y un solo batch:

```bash
python tools/python/run_all_deterministic.py \
  --repo /c/repoVC/multi-clusters/cluster-status-service \
  --state-dir /c/repoVC/coverage_cluster-status-service \
  --module . \
  --skip-jacoco \
  --start-cycle-loop \
  --generation-mode handoff-batch \
  --plan-limit 0 \
  --batch-size 3 \
  --max-batches 1 \
  --max-repair-rounds 2
```

Las tres perillas de tamaño son distintas:

- `--plan-limit 0` — rankea **todos** los targets elegibles en `batch-plan.json`
  (`0` = sin límite, default). `N>0` = top N.
- `--batch-size 3` — cuántos targets van **por request al LLM**.
- `--max-batches 1` — cuántos **batches** procesa esta corrida.

> Con `--plan-limit 0` el único freno de longitud es `--max-batches`. Para una
> corrida acotada (calibración) setealo; sin él, se procesan todos los targets.

El runner va a imprimir algo como:

```text
[HANDOFF-BATCH] Falta generar tests para batch batch-001.
Claude Code debe leer:
  C:\repoVC\coverage_cluster-status-service\_llm\runs\run-YYYYMMDD-HHMMSS\batches\batch-001\request-generation.json
y escribir:
  C:\repoVC\coverage_cluster-status-service\_llm\runs\run-YYYYMMDD-HHMMSS\batches\batch-001\response-generation.json
```

### 3. Prompt correcto para Claude Code / Codex

> **Ya no hace falta editar rutas.** El runner imprime el prompt completo con las
> rutas absolutas **ya resueltas** (el `run-YYYYMMDD-HHMMSS` real, no el
> placeholder), entre marcadores `COPIÁ DESDE ACÁ` / `COPIÁ HASTA ACÁ`, y lo guarda
> en `batches/<batch>/handoff-prompt.txt`. Copiá/pegá tal cual: así no se cuela un
> nombre de carpeta mal tipeado que rompa el flujo. El bloque de abajo es la
> referencia del formato (con el placeholder); en tu consola las rutas vienen
> resueltas.

```text
Resolvé el handoff batch de coverage-agent.

Leé este request:
C:\repoVC\coverage_cluster-status-service\_llm\runs\run-YYYYMMDD-HHMMSS\batches\batch-001\request-generation.json

Escribí la respuesta aquí:
C:\repoVC\coverage_cluster-status-service\_llm\runs\run-YYYYMMDD-HHMMSS\batches\batch-001\response-generation.json

Reglas:
- La respuesta debe ser SOLO JSON válido.
- Debe tener schemaVersion "test-generation-batch-response-v1".
- Debe incluir un item por cada target del request.
- Para cada target usar:
  - status "generated" con patchDescriptor válido, o
  - status "skipped" con reason claro, o
  - status "failed" con reason claro.
- No modificar código productivo.
- No inventar imports, métodos, constructores ni clases.
- Respetar target.allowedImports, evidenceIds, context packs y reglas del request.
- patchDescriptor.allowedImports debe ser subconjunto exacto de target.allowedImports.
- Cada method.evidenceIds debe ser subconjunto exacto de target.allowedEvidenceIds.
- Si target.targetEvidenceRequired es true, cada method.evidenceIds debe incluir
  al menos un id de target.targetEvidenceIds.
- Si target.targetEvidenceRequired es true y target.targetEvidenceIds está vacío,
  no generes código: marcá el item como "skipped" o "failed" con reason claro.
- No uses símbolos, métodos, constructores, clases, constantes, exceptions ni
  asserts sin evidencia. Si target.allowedEvidenceIds no alcanza, marcá el item
  como "skipped" o "failed" con reason claro.
- El body Java solo puede llamar métodos del SUT si el nombre aparece en
  target.evidenceRefs con kind="method". Los constructores no autorizan getters
  ni métodos del SUT por sí solos.
- No uses @DisplayName, @Autowired, @SpringBootTest, imports Spring ni excepciones
  de dominio salvo que el FQCN exacto aparezca en target.allowedImports.
- Cada método @Test debe tener // given, // when, // then.

Regla importante para lambdas sintéticas:
- Si el target trae context.syntheticCoverageTargets, NO lo saltees por ser lambda.
- Generá tests para el método padre real indicado en target.method.
- Cubrí la rama interna de la lambda a través del método padre.
- Para Optional.orElseThrow/orElseGet o suppliers similares, preferí al menos:
  1) un test de camino feliz;
  2) un test de fallback/excepción.

Ejemplo esperado para ClusterQueries.requireConfiguredCluster:
- repository.findByAlias(alias) devuelve Optional.of(cluster) => retorna el cluster.
- repository.findByAlias(alias) devuelve Optional.empty() => lanza ClusterNotFoundException.

Contrato obligatorio de patchDescriptor:
- patchDescriptor NO es un archivo completo. No uses operation, targetFile,
  language, content, coveredMethod ni testMethods.
- patchDescriptor debe usar el contrato canonico:
  schemaVersion, patchId, cycle, sut, testClass, testPackage, template,
  allowedImports, methods.
- patchDescriptor.testClass debe ser EXACTAMENTE target.canonicalTestClass.
- No inventes variantes como *CtorTest, *ConstructorTest, *GeneratedTest o
  *UnitTest.
- patchDescriptor.allowedImports debe contener solo imports de target.allowedImports.
- Cada method.evidenceIds debe contener solo ids de target.allowedEvidenceIds.
- Cada method.evidenceIds debe citar también target.targetEvidenceIds cuando el
  target lo exige.
- Si el body llama un método sobre una variable del tipo SUT, ese método debe
  estar listado en target.evidenceRefs con kind="method".
- patchId debe empezar con "patch:".
- methods debe ser una lista no vacia. Cada metodo debe tener:
  name, annotations, body, evidenceIds.
- El body de cada metodo contiene SOLO el cuerpo del metodo Java, no la clase
  completa, no package, no imports.

Ejemplo minimo de shape, adaptando valores y evidenceIds al request real:

{
  "schemaVersion": "test-generation-batch-response-v1",
  "runId": "run-YYYYMMDD-HHMMSS",
  "batchId": "batch-001",
  "role": "generation",
  "items": [
    {
      "targetId": "tgt:0001",
      "status": "generated",
      "patchDescriptor": {
        "schemaVersion": 1,
        "patchId": "patch:abcdef",
        "cycle": 1,
        "sut": "com.acme.ClusterQueries",
        "testClass": "com.acme.ClusterQueriesTest",
        "testPackage": "com.acme",
        "template": "junit5-mockito",
        "allowedImports": [
          "org.junit.jupiter.api.Test"
        ],
        "methods": [
          {
            "name": "requireConfiguredCluster_whenClusterExists_returnsCluster",
            "annotations": ["@Test"],
            "body": "// given\n// when\n// then",
            "evidenceIds": ["sym:com.acme.ClusterQueries#requireConfiguredCluster:12345678"]
          }
        ]
      }
    }
  ]
}

Cuando termines, no expliques nada: solo escribí response-generation.json.
```

Después de que el agente escriba `response-generation.json`, volvé a la terminal
donde quedó pausado el runner y presioná **ENTER**. El runner va a:

1. validar el JSON;
2. aplicar los patches con `test_patch_applier.py`;
3. correr los tests estrechos;
4. crear `request-repair-r1.json` si algo falla.

### 4. Prompt correcto para repair

Si aparece un handoff de repair, usá este prompt:

```text
Resolvé el repair batch de coverage-agent.

Leé este request:
C:\repoVC\coverage_cluster-status-service\_llm\runs\run-YYYYMMDD-HHMMSS\batches\batch-001\request-repair-r1.json

Escribí la respuesta aquí:
C:\repoVC\coverage_cluster-status-service\_llm\runs\run-YYYYMMDD-HHMMSS\batches\batch-001\response-repair-r1.json

Reglas:
- La respuesta debe ser SOLO JSON válido.
- Debe tener schemaVersion "test-repair-batch-response-v1".
- Repará solo los tests generados, nunca src/main.
- Mantené la intención original del test.
- Hacé el cambio mínimo para compilar y pasar.
- Usá exclusivamente failedItem.allowedImports.
- Usá exclusivamente failedItem.allowedEvidenceIds.
- Si failedItem.targetEvidenceRequired es true, cada method.evidenceIds debe
  incluir al menos un id de failedItem.targetEvidenceIds.
- Eliminá imports reportados como no whitelisted; no los reemplaces por otros
  imports inventados.
- Si no hay evidenceIds suficientes para justificar el repair, marcá el item
  como "abandoned" con reason claro.
- El body reparado solo puede llamar métodos del SUT si el nombre aparece en
  failedItem.evidenceRefs con kind="method"; si no, abandoná el item.
- No uses @DisplayName, @Autowired, @SpringBootTest, imports Spring ni excepciones
  de dominio salvo que el FQCN exacto aparezca en failedItem.allowedImports.
- Si no se puede reparar con evidencia, marcá el item como "abandoned" con reason.

Contrato obligatorio de patchDescriptor para repair:
- Cada item reparado debe tener status "repaired" y patchDescriptor canonico.
- patchDescriptor NO es un archivo completo. No uses operation, targetFile,
  language, content, coveredMethod ni testMethods.
- patchDescriptor debe tener schemaVersion, patchId, cycle, sut, testClass,
  testPackage, template, allowedImports, methods.
- patchDescriptor.testClass debe ser EXACTAMENTE failedItem.canonicalTestClass.
- No mantengas ni inventes variantes como *CtorTest, *ConstructorTest,
  *GeneratedTest o *UnitTest.
- patchDescriptor.allowedImports debe contener solo imports de failedItem.allowedImports.
- Cada method.evidenceIds debe contener solo ids de failedItem.allowedEvidenceIds.
- Cada method.evidenceIds debe citar también failedItem.targetEvidenceIds cuando
  failedItem.targetEvidenceRequired sea true.
- Si el body llama un método sobre una variable del tipo SUT, ese método debe
  estar listado en failedItem.evidenceRefs con kind="method".
- En repair, patchId debe empezar con "repair:".
- Cada method debe tener name, annotations, body, evidenceIds.
- Si el error anterior fue "patchDescriptor missing required keys" o
  "full-file patch keys", no repares Java: reescribi la respuesta con el formato
  correcto del patchDescriptor.

Cuando termines, no expliques nada: solo escribí response-repair-r1.json.
```

### 5. Continuar con más batches

Si el primer batch pasa bien, continuá sin `--max-batches 1` o subilo:

```bash
python tools/python/run_all_deterministic.py \
  --repo /c/repoVC/multi-clusters/cluster-status-service \
  --state-dir /c/repoVC/coverage_cluster-status-service \
  --module . \
  --skip-jacoco \
  --start-cycle-loop \
  --generation-mode handoff-batch \
  --batch-size 5 \
  --max-repair-rounds 2
```

Los tests generados quedan en el repo objetivo:

```text
C:\repoVC\multi-clusters\cluster-status-service\src\test\java\...
```

El estado, requests, responses y reportes quedan fuera del repo objetivo:

```text
C:\repoVC\coverage_cluster-status-service\...
```

Cuando el batch runner termina con todos los targets procesados, ejecuta un
post-stage deterministico:

1. vuelve a correr Maven + JaCoCo;
2. compara `jacoco-baseline.xml` contra el nuevo `target/site/jacoco/jacoco.xml`;
3. escribe:

```text
C:\repoVC\coverage_cluster-status-service\_summaries\batch-final-report.md
C:\repoVC\coverage_cluster-status-service\_summaries\batch-final-report.json
```

El reporte incluye tests generados, totals del manifest y delta de cobertura por
lineas/branches.

> Nota: el architecture review (`run_architecture_review.py`) es otro flujo. Sirve
> para analizar arquitectura y generar `architecture-report.md`; no genera tests.

## Pre-stage Python (parte determinista, obligatorio)

Antes de cualquier ciclo LLM, correr el pipeline determinista que produce todos los `state/*.json`. Esto **reduce drásticamente los tokens** consumidos y acelera la generación (ver [`docs/performance-tuning.md`](docs/performance-tuning.md) y [`docs/python-pipeline.md`](docs/python-pipeline.md)).

### Opción recomendada: un solo comando

`run_all_deterministic.py` orquesta todo el pre-stage de punta a punta, sin intervención del agente.

#### Regla de oro — 3 ubicaciones separadas

| Ubicación | Qué vive ahí | Flag |
|-----------|--------------|------|
| **Arquitectura** `coverage-agent/` | El código de los tools. **No** se escribe análisis acá. | `--agent-root` (default `.`) |
| **Proyecto analizado** `…/proyectox/` | **Solo** los tests nuevos (`src/test/java/…`). Nunca análisis, nunca `src/main`. | `--repo` |
| **Carpeta externa** `…/coverage_proyectox/` | **Todo el análisis y los reportes** (`*.json`, context-packs, `_summaries/analysis-report.md`, caches). Externa a las otras dos; borrable. | `--state-dir` |

> El script **rechaza** un `--state-dir` que sea, contenga o esté dentro del proyecto **o** de la arquitectura (y `--clean` se niega a borrar algo que parezca un proyecto real). Así el análisis nunca queda dentro de la arquitectura ni del proyecto.

**Git Bash** (rutas con `/`):
```bash
python tools/python/run_all_deterministic.py \
   --repo      /c/repo/proyectox \
   --state-dir /c/repo/coverage_proyectox \
   --module    . \
   --clean
```

**cmd.exe** (rutas con `\`, todo en una línea o con `^` de continuación):
```bat
python tools\python\run_all_deterministic.py ^
   --repo      C:\repo\proyectox ^
   --state-dir C:\repo\coverage_proyectox ^
   --module    . ^
   --clean
```

Orden interno: **(A)** pre-pasada de contratos (`pom` + `archetype`) → **(B)** verificación JaCoCo (`jacoco_pom_guard`) → **(C)** baseline Maven que genera `target/` + `jacoco.xml` → **(D)** Fase 0 completa con `--jacoco-xml` (los pasos `pom`/`archetype` salen como `[CACHE HIT]`) → **(E)** reporte consolidado en `<state-dir>/_summaries/analysis-report.md`. Flags útiles:

| Flag | Para qué |
|------|----------|
| `--skip-jacoco` | Reusar un `jacoco.xml` ya generado (saltea A/B/C) |
| `--check-jacoco-pom` | Modo solo-reporte: NO escribe el POM. Por **default** la etapa B aplica: inyecta el plugin JaCoCo en `java-8` / no-BGBA sin JaCoCo (requerido para el gate de OpenShift); `java-21` queda heredado (no se toca). |
| `--start-cycle-loop` | Encadenar el ciclo de generación/reparación (provider por handoff IDE) |
| `--coverage-mode` | `coverage` (default) · `branch-coverage` · `mutation-hardening` |
| `--max-cycles` / `--max-minutes-per-cycle` | Presupuesto sembrado en `execution-state.json` |

> **Consolas:** funciona en **cmd.exe** y **Git Bash** (y PowerShell). En Git Bash
> usá barras `/` en las rutas (`/c/repo/…`); en cmd.exe usá `\` (`C:\repo\…`). Maven
> debe estar en el PATH (en Windows el script lo lanza vía `cmd /c mvn`). Corré el
> script con el Python del entorno que tenga las deps (`pip install -r tools/python/requirements.txt`).
>
> **Salida:** al terminar, el reporte consolidado del análisis queda en
> `<state-dir>/_summaries/analysis-report.md` (+ `.json`) — fuera de la arquitectura y del proyecto.

### Alternativa granular (paso a paso)

Si necesitás control fino sobre cada paso, podés correr Maven y el pipeline a mano:

```bash
mvn -q test jacoco:report     # clases compiladas + jacoco.xml (objetivos + baseline del delta)
python tools/python/run_pipeline.py \
   --repo C:/repo/proyectox \
   --out  C:/repo/coverage_proyectox \
   --module <module> \
   --include-fqcn '^com\.acme\.' \
   --jacoco-xml C:/repo/proyectox/target/site/jacoco/jacoco.xml
```

> `--out`/`--state-dir` apuntan **fuera** del proyecto (`coverage_<proyecto>`); los tests, en cambio,
> se escriben dentro del proyecto. Paso a paso completo con Copilot en
> [§Ejecución desde VS Code + Copilot](#ejecución-desde-vs-code--copilot-paso-a-paso).

## Ejecución desde VS Code + Copilot (paso a paso)

Escenario: en VS Code tenés abierta **esta arquitectura** (`coverage-agent`) como workspace, y
disparás la cobertura de un proyecto externo con **un prompt en Copilot Chat**. Todo lo que genera
la arquitectura queda **fuera** del proyecto; solo los tests se escriben **dentro** del proyecto.

> **Recomendado:** corré primero la **parte determinista desde consola** con
> `run_all_deterministic.py` (ver [§Pre-stage Python](#pre-stage-python-parte-determinista-obligatorio)).
> Así el agente arranca directo en `generation` (paso 2 de abajo) y no gasta tokens en el pre-stage.
> El prompt de abajo incluye el paso 1 solo para el caso en que prefieras que el agente lo dispare.

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
