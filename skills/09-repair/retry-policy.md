# Retry Policy

> **DETERMINISTA — no es un turno del LLM.** Esta fase la ejecuta el pipeline
> Python (`tools/python/gate_runner.py (G7/G8)`); este skill documenta el comportamiento, el
> LLM no lo corre. Ver `skills/00-runtime/02-phase-contracts.md`.


Los thresholds están codificados en
[`tools/python/gate_runner.py`](../../tools/python/gate_runner.py) — esta doc
sólo explica el "por qué". Cualquier cambio numérico se hace en el código.

## Reglas (enforced por gate_runner.py)

- **G7 — por triplet `(errorCode, symbolFQN, fixId)`**:
  `_G7_MAX_FAILED_ATTEMPTS = 2`. Tras 2 fallos del mismo hash, el siguiente
  intento se bloquea con `G7_HASH_OVER_BUDGET`. No se le pide al LLM.
- **G7 — por `testCaseId`**:
  `_G7_MAX_TESTCASE_ATTEMPTS = 3` intentos *acumulativos*. Cubre el caso de
  triplets distintos que rotan sobre el mismo testCase sin converger.
- **G8 — cobertura sin delta**:
  `_G8_MAX_ZERO_DELTA_CYCLES = 2`. Tras 2 ciclos consecutivos que **midieron**
  cobertura y quedaron planos (delta 0 de JaCoCo), se aborta el ciclo
  (`G8_NO_DELTA`). Un ciclo que NO produjo medición fresca (skip/block
  estructural, baseline ausente o compile-fail) PRESERVA el contador y nunca
  cuenta como stall (M3); ese conteo lo deriva `cycle_loop.record_outcome`.
- **G8 — compile-fail rate**:
  `_G8_MAX_COMPILE_FAIL_RATE = 0.5`. Si la última entrada de
  `compileFailRateWindow` (registrada por el cycle-orchestrator) supera 0.5,
  se aborta con `G8_COMPILE_FAIL_RATE`.

## Flujo

- Entre intentos: re-correr G1 + G6 antes de gastar build.
- Tras el último intento permitido: test descartado en `discardedTests[]`,
  objetivo de vuelta a planning con `revisit: true`.

## Prohibido

- Retries ciegos sin parser de errores.
- Bajar el `assert` para "hacer pasar" un test.
- Marcar `@Disabled`/`@Ignore` como reparación.
