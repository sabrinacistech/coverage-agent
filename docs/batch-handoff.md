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

## Comandos

Todos parten de `run_all_deterministic.py` (entrypoint real; corre la fase 0
determinista y después arranca el loop con `--start-cycle-loop`).

### Modo batch recomendado (10 por tirada)

```powershell
.\.venv\Scripts\python.exe tools\python\run_all_deterministic.py `
  --repo       C:\repoVC\coverage_cluster-status-service `
  --state-dir  C:\repoVC\agent-state-cluster `
  --generation-mode handoff-batch `
  --batch-size 10 `
  --max-repair-rounds 2 `
  --start-cycle-loop
```

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

Cuando el runner necesita generar un batch, imprime:

```
========================================================================
[HANDOFF-BATCH] Falta generar tests para batch batch-001.
Claude Code debe leer:
  <state>\_llm\runs\run-YYYYMMDD-HHMMSS\batches\batch-001\request-generation.json
y escribir:
  <state>\_llm\runs\run-YYYYMMDD-HHMMSS\batches\batch-001\response-generation.json

Cuando Claude Code termine, volvé a esta consola y presioná ENTER.
También podés escribir:  skip (saltar este batch) · status (estado) · quit (cortar).
Mientras espera, el budget está PAUSADO (no dispara BUDGET_EXCEEDED).
========================================================================
```

1. En Claude Code, pedile que **lea `request-generation.json` y escriba
   `response-generation.json`** con un item por target. Cada item:
   - `status: "generated"` + `patchDescriptor` (valida contra
     `patch-descriptor.schema.json`), o
   - `status: "skipped"` + `reason` (p.ej. requiere un servicio externo), o
   - `status: "failed"` + `reason`.
   AdemÃ¡s, el `patchDescriptor.testClass` debe coincidir exactamente con
   `target.canonicalTestClass`. El runner rechaza variantes inventadas como
   `*CtorTest`, `*ConstructorTest`, `*GeneratedTest` o `*UnitTest` antes de llegar
   al patcher, para evitar `G6_LINTER_FAIL` por clases de test no canÃ³nicas.
   La misma estrategia aplica para imports: cada target lleva `allowedImports`,
   `forbiddenImports` e `importPolicy`. El runner rechaza cualquier
   `patchDescriptor.allowedImports` que no sea subconjunto de `target.allowedImports`
   y tambiÃ©n anotaciones conocidas que implican imports prohibidos, como
   `@DisplayName`, `@Autowired` o `@SpringBootTest`.
   Para G2, cada target tambiÃ©n lleva `allowedEvidenceIds`, `evidenceRefs` y
   `evidencePolicy`. El runner rechaza cualquier `methods[].evidenceIds` vacÃ­o o
   fuera de `target.allowedEvidenceIds` antes de llegar al patcher. Ademas, cuando
   `target.targetEvidenceRequired` es true, cada test generado debe citar al menos
   un id de `target.targetEvidenceIds`; si esa lista esta vacia, el LLM debe
   marcar el item como `skipped`/`failed` en vez de generar codigo contra un metodo
   no evidenciado.
2. Volvé a la consola y presioná **ENTER**. El runner valida el JSON, **aplica
   cada patch** (gates G1–G8 + presupuesto + seguridad de literales Java, por
   construcción), **corre los tests** y clasifica cada target en PASSED /
   COMPILE_FAILED / TEST_FAILED.
   - `skip` salta este batch (marca los targets SKIPPED y avanza).
   - `status` imprime los totales del run.
   - `quit` corta el run (deja el manifest persistido).
3. **Repair solo para fallidos:** si hay fallos, el runner escribe
   `request-repair-r1.json` con **únicamente** los items fallidos (tipo de falla,
   archivo de test, error, salida de build, source actual) y pide:

   ```
   [HANDOFF-REPAIR] Hay tests fallidos en batch batch-001, repair round 1.
   ```

   Claude Code escribe `response-repair-r1.json` (`repaired` + `patchDescriptor`,
   o `abandoned`/`skipped`/`failed`). El runner re-aplica y re-testea.
   En repair aplica la misma regla: `patchDescriptor.testClass` debe ser
   `failedItem.canonicalTestClass`. Si el intento anterior usÃ³ una variante
   rechazada, queda informada como `failedItem.rejectedTestClass`, pero no debe
   reutilizarse.
   Repair tambiÃ©n recibe `failedItem.allowedImports`; cualquier import fuera de
   esa lista se considera respuesta invÃ¡lida antes del patcher.
   Lo mismo aplica a `failedItem.allowedEvidenceIds`: si el repair no puede citar
   evidencia valida, debe abandonar el item en vez de inventar simbolos.
   Si `failedItem.targetEvidenceRequired` es true, cada metodo reparado tambien
   debe citar `failedItem.targetEvidenceIds`.
   evidencia vÃ¡lida, debe abandonar el item en vez de inventar sÃ­mbolos.
4. Un target que sigue fallando tras `--max-repair-rounds` se marca **ABANDONED** y
   el run continúa.

5. Cuando ya no quedan targets pendientes y el manifest termina en `DONE`, el
   runner ejecuta el post-stage deterministico `batch_final_report.py`: vuelve a
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

## Estado persistente (retomar un run)

Todo el estado del run vive bajo `<state>\_llm\runs\run-YYYYMMDD-HHMMSS\`:

```
manifest.json                      ← modo, batchSize, maxRepairRounds, status, totals,
                                      y el estado de CADA target
batches/batch-001/
  request-generation.json  response-generation.json  validation-result.json
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
```
