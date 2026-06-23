# Handoff incremental por batches

Esta guía describe el flujo de generación de tests por **batches** y los tres
modos de generación. Resuelve dos problemas del handoff de a un target:

1. **El handoff manual consumía presupuesto.** El budget por ciclo
   (`maxMinutesPerCycle`) medía tiempo de reloj desde el inicio del ciclo, así que
   el tiempo que Claude Code tardaba en generar el JSON —y el que vos tardabas en
   volver a la consola y presionar ENTER— **contaba contra el presupuesto**. Con un
   valor bajo (default 10 min) saltaba `BUDGET_EXCEEDED` mientras pensabas. Ahora la
   espera del handoff **pausa el budget** (ver §"Pausa de budget").
2. **Un handoff por test no escala.** En proyectos de 100+ clases, un request/
   response por target es inviable. El modo `handoff-batch` agrupa **hasta
   `--batch-size` targets en un único request**, aplica todos, corre los tests y
   pide reparación **solo para los fallidos**.

---

## Los tres modos (`--generation-mode`)

| Modo | Para qué | input()/ENTER | Budget durante espera |
|---|---|---|---|
| `handoff-single` | **debug** — un target por handoff (flujo histórico, vía `cycle_loop` + `one_cycle`). Default. | sí (TTY) | **pausado** |
| `handoff-batch` | **recomendado** — hasta `--batch-size` targets por request, con rondas de repair para los fallidos. | sí (TTY) | **pausado** |
| `auto` | autónomo — sin handoff por archivo, sin `input()`. Requiere un provider de modelo configurado (`COVAGENT_LLM_PROVIDER=litellm` + credenciales). | no | n/a |

`auto` con el provider por defecto (`ide`, que es handoff manual) **falla con un
error claro**: *"auto generation mode is not configured…"*. No hay fallback
silencioso a un handoff.

---

## Tres perillas de tamaño (no las confundas)

| Flag | Quién lo usa | Qué controla |
|---|---|---|
| `--plan-limit` | `coverage_planner.py` (fase 0) | Cuántos targets **rankea el plan** (`batch-plan.json`). `0` = **todos** los elegibles (default). `N>0` = top N por score. |
| `--batch-size` | `orchestrator.batch_runner` | Cuántos targets van **por request al LLM**, consumidos del plan (default 10). |
| `--max-batches` | `orchestrator.batch_runner` | Cuántos **batches** procesa esta corrida. Default: sin tope. |

`--plan-limit` es el tamaño del **plan**; `--batch-size` es el tamaño **operativo**
de cada handoff. Antes ambos compartían el nombre `--batch-size` y el planner
recortaba el plan a 10 por default, dejando inalcanzables los targets restantes.
Hoy el plan calcula y rankea **todo**, y el runner decide cuánto procesar.

> **⚠️ Backstop:** con `--plan-limit 0` (default) el plan ya no tiene el viejo tope
> implícito de 10. Como `--max-batches` default es *sin tope*, una corrida sin
> `--max-batches` procesará **todos** los targets en batches sucesivos. Para una
> corrida acotada (calibración), seteá `--max-batches` explícitamente.

En `coverage_planner.py`, `--batch-size` quedó como **alias deprecado** de
`--plan-limit` (emite warning); `--plan-limit` gana si se pasan ambos.

El `batch-plan.json` ahora incluye metadata auditable: `totalEligibleTargets`,
`planLimit`, `sizeChosen`, `rankingStrategy` y `note`.

## Comandos

Todos parten de `run_all_deterministic.py` (entrypoint real; corre la fase 0
determinista y después arranca el loop con `--start-cycle-loop`).

### Modo batch recomendado (10 por tirada)

```powershell
.\.venv\Scripts\python.exe tools\python\run_all_deterministic.py `
  --repo       C:\repoVC\coverage_cluster-status-service `
  --state-dir  C:\repoVC\agent-state-cluster `
  --generation-mode handoff-batch `
  --plan-limit 0 `
  --batch-size 10 `
  --max-repair-rounds 2 `
  --start-cycle-loop
```

`--plan-limit 0` rankea **todos** los targets; `--batch-size 10` manda 10 por
request. Sin `--max-batches` la corrida procesa todos los batches hasta agotar el
plan (o el budget).

### Modo calibración (proyecto nuevo / con muchos fallos)

```powershell
.\.venv\Scripts\python.exe tools\python\run_all_deterministic.py `
  --repo C:\repoVC\coverage_cluster-status-service --state-dir C:\repoVC\agent-state-cluster `
  --generation-mode handoff-batch --batch-size 3 --max-batches 1 --max-repair-rounds 1 `
  --start-cycle-loop
```

### Modo debug (un target por handoff)

```powershell
.\.venv\Scripts\python.exe tools\python\run_all_deterministic.py `
  --repo C:\repoVC\coverage_cluster-status-service --state-dir C:\repoVC\agent-state-cluster `
  --generation-mode handoff-single --start-cycle-loop
```

> Si ya corriste la fase 0 antes, agregá `--skip-jacoco` para reutilizar el
> `jacoco.xml` existente y no reconstruir el baseline de Maven.

---

## Flujo `handoff-batch`, paso a paso

Cuando el runner necesita generar un batch, imprime el banner informativo **y un
prompt listo para copiar/pegar** con las rutas absolutas YA resueltas (el `run_id`
y `batch_id` reales, nunca el placeholder `run-YYYYMMDD-HHMMSS`). Así no hay que
editar la ruta a mano — un nombre de carpeta mal tipeado rompía y confundía al
agente:

```
========================================================================
[HANDOFF-BATCH] Falta generar tests para batch batch-001.
Claude Code debe leer:
  <state>\_llm\runs\run-20260616-164748\batches\batch-001\request-generation.json
y escribir:
  <state>\_llm\runs\run-20260616-164748\batches\batch-001\response-generation.json

Cuando Claude Code termine, volvé a esta consola y presioná ENTER.
También podés escribir:  skip (saltar este batch) · status (estado) · quit (cortar).
Mientras espera, el budget está PAUSADO (no dispara BUDGET_EXCEEDED).

───────────── COPIÁ DESDE ACÁ (pegar en Claude Code / Codex) ─────────────
Resolvé el handoff batch de coverage-agent.

Leé este request:
<state>\_llm\runs\run-20260616-164748\batches\batch-001\request-generation.json

Escribí la respuesta acá:
<state>\_llm\runs\run-20260616-164748\batches\batch-001\response-generation.json
... (reglas) ...
───────────── COPIÁ HASTA ACÁ ─────────────
========================================================================
```

El mismo prompt se escribe también a disco en
`batches/<batch>/handoff-prompt.txt` (vía `RunPaths.handoff_prompt`), para abrirlo
y copiarlo sin scrollear la consola. Para repair, el prompt usa
`request-repair-rN.json` / `response-repair-rN.json` con el round real.

El **texto** del prompt no está hardcodeado: vive en plantillas `.md` editables por
humanos en [`prompts/`](../prompts/README.md) (`handoff-generation.md`,
`handoff-repair.md`). El runner las carga (`orchestrator/prompts.render_handoff_prompt`)
y las completa con las rutas reales del batch (`${REQUEST_PATH}`, `${RESPONSE_PATH}`,
`${SCHEMA_VERSION}`, `${RUN_ID}`, `${BATCH_ID}`, `${REPAIR_ROUND}`). Para mejorar el
prompt que guía al LLM, editá el `.md` —sin tocar Python—; si la plantilla falta, el
runner usa un prompt mínimo embebido como fallback.

1. En Claude Code, pedile que **lea `request-generation.json` y escriba
   `response-generation.json`** con un target por target del request. **El LLM NO arma el
   `patchDescriptor`** (ver §"Contrato de generación"): por target devuelve solo
   - `status: "generated"` + `methods[]` (cada método `{name, annotations, body,
     evidenceIds}`), o
   - `status: "skipped"` + `reason` (p.ej. requiere un servicio externo), o
   - `status: "failed"` + `reason`, o
   - `status: "NEED_MORE_CONTEXT"` + `missingSymbols` + `reason` (ver §"`contextPolicy`").

   Python hidrata el `patchDescriptor` canónico desde el target: fija
   `schemaVersion`, `patchId`, `sut`, `testClass` (= `target.canonicalTestClass`),
   `testPackage`, `template` y `allowedImports` (= whitelist del target). El modelo
   nunca elige la clase de test ni los imports, así que desaparecen las variantes
   inventadas (`*CtorTest`, etc.) y el error histórico `patchDescriptor missing
   required keys`.
   Para G2, cada target lleva `allowedEvidenceIds`, `evidenceRefs` y
   `evidencePolicy`. La hidratación rechaza, **por item**, cualquier
   `methods[].evidenceIds` vacío o fuera de `target.allowedEvidenceIds`. Cuando
   `target.targetEvidenceRequired` es true, cada test generado debe citar al menos
   un id de `target.targetEvidenceIds`; si esa lista está vacía, el LLM debe
   marcar el item como `skipped`/`failed` en vez de generar código contra un método
   no evidenciado. El body Java solo puede llamar métodos del SUT cuando el nombre
   aparece en `target.evidenceRefs` con `kind="method"`; los constructores no
   autorizan getters/métodos del SUT por sí solos.
2. Volvé a la consola y presioná **ENTER**. El runner valida el JSON, **aplica
   cada patch** (gates G1–G8 + presupuesto + seguridad de literales Java, por
   construcción), **corre los tests** y clasifica cada target en PASSED /
   COMPILE_FAILED / TEST_FAILED.
   - `skip` salta este batch (marca los targets SKIPPED y avanza).
   - `status` imprime los totales del run.
   - `quit` corta el run (deja el manifest persistido).
3. **Repair solo para fallidos:** si hay fallos, el runner escribe
   `request-repair-r1.json` con **únicamente** los targets fallidos (tipo de falla,
   archivo de test, error, salida de build, source actual, `repairCause` estructurado)
   y pide:

   ```
   [HANDOFF-REPAIR] Hay tests fallidos en batch batch-001, repair round 1.
   ```

   Claude Code escribe `response-repair-r1.json` (`repaired` + `patchDescriptor`,
   o `abandoned`/`skipped`/`failed`). **Igual que en generación, Python hidrata el
   descriptor** desde el `failedItem` antes de validarlo: rellena `schemaVersion`,
   `sut`, `testClass`/`testPackage`, `template` y `allowedImports` cuando el modelo
   los omite, así un repair al que le falta `schemaVersion`/`sut` ya **no corta la
   ronda** (ver §"Contrato de repair"). El runner re-aplica y re-testea.
   En repair aplica la misma regla: `patchDescriptor.testClass` debe ser
   `failedItem.canonicalTestClass`. Si el intento anterior usó una variante
   rechazada, queda informada como `failedItem.rejectedTestClass`, pero no debe
   reutilizarse.
   Repair también recibe `failedItem.allowedImports`; cualquier import fuera de
   esa lista se descarta al hidratar, y **los imports wildcard / de paquete
   completo (`pkg.*`) están prohibidos** — el runner los elimina y nunca llegan al
   linter (que los rechazaría como `IMPORT_PKG_NOT_WHITELISTED` / `G6_LINTER_FAIL`).
   Lo mismo aplica a `failedItem.allowedEvidenceIds`: si el repair no puede citar
   evidencia válida, debe abandonar el item en vez de inventar símbolos.
   Si `failedItem.targetEvidenceRequired` es true, cada método reparado también
   debe citar `failedItem.targetEvidenceIds`. El body reparado solo puede llamar
   métodos del SUT que aparezcan en `failedItem.evidenceRefs` con `kind="method"`.
4. Un target que sigue fallando tras `--max-repair-rounds` se marca **ABANDONED** y
   el run continúa.

5. Cuando ya no quedan targets pendientes y el manifest termina en `DONE`, el
   runner ejecuta el post-stage determinístico `batch_final_report.py`: vuelve a
   correr Maven + JaCoCo, calcula `coverage-delta.json` contra
   `state/jacoco-baseline.xml` y escribe `_summaries/batch-final-report.md` +
   `_summaries/batch-final-report.json`.

### Reglas de avance entre batches

Tras aplicar + testear (y reparar):

| Pass rate del batch | Acción |
|---|---|
| 100% | continuar |
| ≥ 80% | reparar fallidos, luego continuar |
| 50–80% | reparar antes de continuar |
| < 50% | **frenar** (recomienda bajar `--batch-size`); no avanza solo |
| error de compilación global | reparar antes de avanzar |

---

## Contrato de generación: el LLM NO arma el `patchDescriptor`

El contrato evolucionó de *"el LLM devuelve un `patchDescriptor` completo"* a
*"el LLM devuelve una completion mínima y **Python hidrata** el `patchDescriptor`
canónico"*. Motivo: en pruebas reales el modelo omitía campos obligatorios
(`schemaVersion`, `patchId`, `sut`) y eso fallaba el item — y, peor, **arrastraba
todo el batch** a `GENERATION_FAILED`.

Reglas:

- **El LLM no debe devolver `patchDescriptor` ni `testSource`.** Devuelve solo
  `status`, `methods`, `reason` y `missingSymbols`.
- **Python es la única fuente de verdad** de todo metadato estructural:
  `schemaVersion` (=1), `patchId` (`patch:<slug>`), `sut` (=`target.sut`),
  `testClass` (=`target.canonicalTestClass`), `testPackage`, `template`
  (=`target.template`) y `allowedImports` (=whitelist concreta del target, **sin
  wildcards/imports de paquete**; el patcher resuelve los imports que falten desde
  los bodies y poda los no usados, dejando el bloque de imports mínimo).
- **Validación por item, no por batch.** Un item inválido queda
  `GENERATION_FAILED` solo él; los `generated` válidos, `skipped` y
  `NEED_MORE_CONTEXT` hermanos no se ven afectados. Solo un envelope roto
  (schemaVersion/role/batchId/`targets` no-lista) aborta el batch.
- **Compat de transición:** si el modelo todavía manda `patchDescriptor`, se usa
  **solo** `patchDescriptor.methods`; el resto de su metadata se ignora.

`request-generation.json` lleva un `responseCompletionContract` con el `targetShape`
esperado. Ejemplo de `response-generation.json` (formato nuevo):

```json
{
  "schemaVersion": "test-generation-batch-response-v1",
  "runId": "run-...",
  "batchId": "batch-001",
  "role": "generation",
  "targets": [
    {
      "targetId": "tgt:0001",
      "status": "generated",
      "methods": [
        {
          "name": "shouldReturnValue_whenInputIsValid",
          "annotations": ["@Test"],
          "body": "// given\n...\n// when\n...\n// then\n...",
          "evidenceIds": ["sym:com.acme.Foo#bar:abcd1234"]
        }
      ]
    }
  ]
}
```

`patchDescriptor` que Python hidrata para ese target (lo que llega al patcher):

```json
{
  "schemaVersion": 1,
  "patchId": "patch:tgt-0001",
  "sut": "com.acme.Foo",
  "testClass": "com.acme.FooTest",
  "testPackage": "com.acme",
  "template": "unit",
  "allowedImports": ["org.junit.jupiter.api.Test"],
  "fields": [],
  "methods": [ { "name": "shouldReturnValue_whenInputIsValid", "annotations": ["@Test"], "body": "...", "evidenceIds": ["sym:com.acme.Foo#bar:abcd1234"] } ]
}
```

`validation-result.json` **se escribe siempre** (incluso si la respuesta es
inválida o hay targets omitidos), con `counts` (incluye `received`, `generatedValid`,
`generatedInvalid`, `applied`, `passed`, `failed`, `compile`, `skipped`,
`needMoreContext`, `omitted`) y `targets[]` con el motivo por target. Razones de
fallo por target: `COMPLETION_SCHEMA_ERROR`, `PATCH_DESCRIPTOR_HYDRATION_ERROR`,
`UNKNOWN_TARGET_ID`, `DUPLICATED_TARGET_ID`, `MISSING_METHODS`,
`INVALID_EVIDENCE_ID`, `TARGET_EVIDENCE_REQUIRED`, `OMITTED_FROM_RESPONSE`.

---

## Contrato de repair: Python también hidrata el `patchDescriptor`

El flujo de **repair** sigue el mismo principio que generación: **Python es la
fuente de verdad de la metadata estructural**. El LLM escribe `response-repair-rN
.json` con `status` (`repaired`/`abandoned`/`skipped`/`failed`/`NEED_MORE_CONTEXT`)
y, para `repaired`, los `methods` corregidos dentro de `patchDescriptor`. Antes de
validar, `validate_repair_response` pasa cada item por `hydrate_repair_descriptor`,
que reconstruye desde el `failedItem`:

- `schemaVersion` → siempre `1`.
- `sut` → se copia del `failedItem` si falta o viene vacío.
- `patchId` → se sintetiza (`repair:<slug>:r<round>`) **solo si falta**. Un
  `patchId` presente con prefijo incorrecto (p. ej. `patch:`) se sigue rechazando.
- `testClass`/`testPackage`/`template` → backfill desde `failedItem.canonicalTestClass`
  / `failedItem.template` cuando el modelo los omite.
- `allowedImports` → whitelist concreta del `failedItem`, **sin wildcards** (`pkg.*`);
  el patcher resuelve los imports faltantes desde los bodies y poda los no usados.

El descriptor hidratado se escribe de vuelta en el item, así el patcher aplica la
versión canónica. Resultado: el error histórico `patchDescriptor missing required
keys: ['schemaVersion', 'sut']` ya **no aparece** y un modelo que olvida un campo
estructural ya no rompe la ronda de repair.

> **Guardrails que se mantienen:** un valor **presente pero incorrecto** (sut,
> testClass o patchId con prefijo equivocado, variante `*CtorTest`, import fuera de
> whitelist, evidencia inválida) se sigue rechazando — la hidratación solo rellena
> lo **ausente**, nunca pisa una decisión explícita errónea del modelo.

---

## Pre-flight evidence gate (task 2)

**Antes de cualquier llamada al LLM**, el runner evalúa si cada target tiene
suficiente evidencia en sus propios metadatos para ser generado batch-only. Un
target sin evidencia de constructores/métodos (o cuyo método-bajo-prueba requiere
evidencia que no fue encontrada) se **salta antes del handoff** en lugar de ser
enviado al modelo y luego rechazado por G2 (un handoff desperdiciado).

```
[preflight] 3 target(s) saltados por falta de evidencia (no se envían al LLM).
```

Motivo persistido: `"Falta de evidencia de tipos/parámetros en metadatos"`.

Otros motivos de skip pre-flight:
- `CLINIT_WITHOUT_ENUM_CONSTANTS` — un `<clinit>` sin evidencia de constantes de
  enum, **o** el constructor sintético de un enum (`<init>` en un SUT enum) sin un
  método testeable proyectado (`values`/`valueOf`/getter público). Un constructor
  de **clase normal** nunca se ve afectado por esta regla.
- `TARGET_METHOD_BODY_MISSING` — el cuerpo del método-bajo-prueba no viaja en la
  proyección `sutSourceCode`.

**Artefacto en disco:** `batches/<batch>/preflight-result.json` — lista de targets
saltados con su motivo, disponible para auditoría.

```json
{
  "batchId": "batch-001",
  "skipped": [
    { "targetId": "com.acme.Foo#bar", "sut": "com.acme.Foo",
      "reason": "Falta de evidencia de tipos/parámetros en metadatos" }
  ]
}
```

La tasa de avance entre batches se calcula sobre los targets que SÍ se enviaron al
LLM (`sendableIds`), para que los skips de preflight no penalicen el pass rate.

---

## `contextPolicy` — scope batch-only (task 3)

Cada `request-generation.json` y `request-repair-rN.json` lleva al tope:

```json
"_IMPORTANT_WARNING": "ISOLATED ENTITY. Operate ONLY on the information in THIS JSON. ...",
"contextPolicy": {
  "scope": "batch_only",
  "allowRepositoryRead": false,
  "allowProductionCodeRead": false,
  "onMissingContext": "NEED_MORE_CONTEXT"
},
"missingContextPolicy": {
  "allowedStatus": "NEED_MORE_CONTEXT",
  "rule": "If a constructor, method, getter/setter, ... needed to write the test is NOT present in this request, answer with status NEED_MORE_CONTEXT...",
  "responseShape": { "status": "NEED_MORE_CONTEXT", "missingSymbols": [], "reason": "" }
},
"selfContainedPolicy": { ... }
```

Cuando el LLM responde `NEED_MORE_CONTEXT` para un target:
- En **generación**: el target se marca `SKIPPED` con `reason: "MISSING_CONTEXT: <motivo>"` y `missingSymbols` persistidos para auditoría.
- En **repair**: el target se marca `ABANDONED` con el mismo patrón.

`NEED_MORE_CONTEXT` **nunca falla la validación del batch** — es una respuesta válida del protocolo.

### `structuredContext` por target

Cada target en el request incluye un bloque `structuredContext` con:

| Campo | Contenido |
|---|---|
| `targetSource.sourceCode` | Cuerpos de métodos/constructores del SUT (hermético) |
| `dependencySources` | Firmas públicas de colaboradores del proyecto |
| `allowedApi` | `evidenceRefs` agrupados |
| `existingRelatedTests` | Nombres de métodos `@Test` ya existentes en el test file del SUT |
| `expectedBehavior` | Hints del planner: `generationHint` + descripciones de `syntheticCoverageTargets` |
| `missingContextPolicy` | `{"allowedStatus": "NEED_MORE_CONTEXT"}` |

`existingRelatedTests` se extrae directamente de `src/test/java/<SUT>Test.java` (si
existe); nunca está vacío porque el SUT todavía no tenga tests — simplemente queda
como lista vacía. `expectedBehavior` viene del campo `context` del plan item, sin
requerir lectura del repo.

### Payload hermético: `sutSourceCode` + `dependencySignatures`

El SUT viaja dentro del request como cuerpos de métodos/constructores
(`target.sutSourceCode`). Los colaboradores del proyecto viajan como firmas públicas
(`target.dependencySignatures`). De esta forma **el generador nunca necesita leer el
working tree** para entender el comportamiento del SUT.

Límites aplicados:
- `sutSourceCode`: hasta 60 KB de cuerpos (truncado con marcador si excede).
- `dependencySignatures`: hasta 25 colaboradores, 200 KB por archivo.

### Limitación declarativa (task 4)

El `contextPolicy` y el `selfContainedPolicy` son **directivas declarativas**: el
runner no puede bloquear programáticamente las herramientas del IDE cuando Claude
Code opera en modo handoff manual (el generador tiene acceso libre al working tree).

La mitigación actual:
- `_IMPORTANT_WARNING` al tope de cada request JSON (primera clave, visible al abrir el archivo).
- `contextPolicy.allowRepositoryRead: false` + `selfContainedPolicy.forbiddenActions`.
- Primer `rules[0]` es `SELF_CONTAINED_RULE` verbatim.

Una mitigación programática completa requeriría cambiar la arquitectura de
`providers.py` (pasar `tools=[]` a litellm, o usar un subagente sandboxed sin
acceso al FS). Documentado como riesgo pendiente.

---

## Strict repair loop — admission gate (task 6)

Antes de escribir `request-repair-rN.json`, el runner evalúa si cada target fallido
es **accionable** para un nuevo handoff. Los targets no accionables se abandonan
directamente sin gastar tokens.

### Reglas de abandono

| Código | Condición |
|---|---|
| `REPEATED_FAILURE_SIGNATURE` | La firma del fallo es idéntica a la del round anterior → el LLM ya intentó sin éxito esta causa exacta |
| `PATCHER_REJECTED_WITHOUT_DIAGNOSTICS` | El patcher devolvió rc=3 pero no hay `patcherErrorDetails` ni `compilerErrorDetails` → sin causa semántica para reparar |
| `NO_ACTIONABLE_LOGS` | No hay logs del compilador/patcher ni build output, y el resumen es genérico (`COMPILATION_ERROR`, `PATCH_REJECTED`, etc.) |
| `NO_PROGRESS_AFTER_REPAIR` | El round de repair no re-aplicó ningún patch (el modelo saltó/falló todos los targets) |
| `MISSING_CONTEXT` | El LLM respondió `NEED_MORE_CONTEXT` en repair |

**`TEST_FAILURE` siempre recibe un round:** tiene reporte surefire y `currentTestSource`
para razonar. Si sus logs son débiles, el runner verifica con `weak_diagnostics()` y
le permite **exactamente un round**; si persiste el fallo, se abandona.

### Failure signature

```
SHA-1(failureKind || errorSummary || first-5-compiler-lines || [BLOCKED]-lines)[:16]
```

Hex corto de 16 chars. Se persiste en `manifest.targets[id].lastFailureSignature`
entre rounds para detectar causas idénticas.

---

## `repairCause` estructurado (task 7)

Cada item en `request-repair-rN.json` lleva un campo `repairCause`:

```json
{
  "kind": "COMPILER_ERROR | NAMING_OR_QUALITY_RULE | IMPORT_RULE | EVIDENCE_RULE | ASSERTION_OR_RUNTIME | PATCHER_GATE | UNKNOWN",
  "summary": "línea concreta del error o resumen del lifecycle",
  "stdout": "build output capturado",
  "stderr": "compiler error details verbatim",
  "patcherDiagnostics": ["[BLOCKED] G2_SYMBOL_WITHOUT_EVIDENCE"],
  "failedRules": ["E_CONSTRUCTOR_UNRESOLVED"],
  "rejectedFiles": ["src/test/java/com/acme/FooTest.java"],
  "rejectedMethods": ["com.acme.FooCtorTest"],
  "previousFailureSignature": "abc1234567890def"
}
```

Reemplaza el anterior "patcher rc=3" sin información.

---

## Path management — `RunPaths` (task 5)

`RunPaths` es la **única fuente de verdad** para todos los artefactos on-disk de un run:

```python
paths = RunPaths(state_dir, run_id)

paths.run_dir                            # <state>/_llm/runs/<run_id>/
paths.manifest()                         # run_dir/manifest.json
paths.batch_dir("batch-001")             # run_dir/batches/batch-001/
paths.request_generation("batch-001")   # .../request-generation.json
paths.response_generation("batch-001")  # .../response-generation.json
paths.validation_result("batch-001")    # .../validation-result.json
paths.preflight_result("batch-001")     # .../preflight-result.json
paths.request_repair("batch-001", 1)    # .../request-repair-r1.json
paths.response_repair("batch-001", 1)   # .../response-repair-r1.json
paths.validation_result_repair("batch-001", 1)  # .../validation-result-r1.json
paths.handoff_prompt("batch-001")       # .../handoff-prompt.txt (copy-paste prompt)
```

`paths.assert_consistent(batch_id)` valida que ningún path derivado se salga de
`run_dir` ni carezca del `run_id`/`batch_id` en sus partes — protege contra el bug
`run-XXXX` vs `run-XXXXS` (sufijo stray).

`batch_final_report.py` recibe `--run-id` y `--run-dir` del runner; cuando ambos
están presentes valida que apunten al mismo directorio (guard de consistencia).

---

## Estado persistente (retomar un run)

Todo el estado del run vive bajo `<state>\_llm\runs\run-YYYYMMDD-HHMMSS\`:

```
manifest.json                      ← modo, batchSize, maxRepairRounds, status, totals,
                                      y el estado de CADA target
batches/batch-001/
  request-generation.json  response-generation.json  validation-result.json
  preflight-result.json    (targets saltados antes del LLM)
  request-repair-r1.json   response-repair-r1.json    validation-result-r1.json
```

Estados por target: `PENDING → GENERATION_REQUESTED → GENERATED → APPLIED →
{PASSED | COMPILE_FAILED | TEST_FAILED} → REPAIR_REQUESTED → {REPAIRED→PASSED |
ABANDONED}` (más `SKIPPED`, `GENERATION_FAILED`, `PATCH_FAILED`).

Los targets ya procesados se anotan en `_summaries/processed-targets.json`, así
que **si el proceso se corta, al re-lanzar el runner retoma por el primer target
pendiente** (no repite los terminados). El `manifest.json` te dice qué batch
estaba en curso y los totales (`pending / generated / passed / failed / skipped /
abandoned`).

---

## Pausa de budget (el fix principal)

El budget de minutos mide **solo el trabajo automático** del runner: selección de
targets, I/O de request/response, aplicación de patches, ejecución de tests,
análisis de errores y armado del request de repair. **No** mide la espera del
handoff manual.

En los logs:

```
[budget] paused: manual handoff: generation batch-001
[handoff] waiting for response JSON: response-generation.json
[budget] resumed
```

Implementación: `budget_enforcer.pause/resume` (+ el context manager
`budget_enforcer.paused(...)`) congelan el reloj del ciclo durante la espera y
desplazan `cycleStartedAt` hacia adelante por el lapso pausado al volver. Resultado:
**`BUDGET_EXCEEDED` solo puede ocurrir durante trabajo automático, nunca mientras
el proceso espera que vuelvas con ENTER.**

---

## Recomendación para proyectos grandes

1. Correr primero con `--batch-size 3 --max-batches 1` (calibración).
2. Si pasa bien, subir a `--batch-size 10`.
3. No avanzar si hay fallas globales de compilación: reparar antes de seguir.
4. Abandonar targets que fallan más de `--max-repair-rounds` (default 2) y seguir.
5. Si un proyecto falla mucho (pass rate < 50%), el runner frena solo: bajá el
   `--batch-size` y volvé a correr.

---

## Priorización de targets

El planner (`coverage_planner.py`) ya ordena los targets por **score** de cobertura
descendente y **penaliza** las clases de alto `testabilityRisk` (config Spring
completa, security filters, repos reales), además de excluir el código
autogenerado. Por eso los primeros batches tienden a traer utils, mappers,
validators y services con dependencias mockeables, dejando la lógica compleja para
después. Las reglas de generación que se envían en cada `request-generation.json`
refuerzan esto (evitar levantar contexto Spring completo, mockear dependencias
externas, edge cases para sanitizers/encoders/parsers, y **seguridad de literales
Java**).
