# Dynamic Batch Sizing

## Objetivo
Decidir el tamaño del batch del ciclo para maximizar throughput sin saturar el contexto ni el build.

## Heurísticas
- Tamaño base: `5` objetivos por batch.
- Si `compileFailRate` del ciclo anterior > 0.3 ⇒ reducir a `max(2, base/2)`.
- Si los últimos 2 ciclos tuvieron `delta > 0` y `compileFailRate == 0` ⇒ incrementar `base + 2` (hasta tope `10`).
- Si el módulo es lento (último narrow run > 5 min) ⇒ tope `3`.

## Reglas
- Nunca incluir dos objetivos del mismo SUT en el mismo batch si `mode == branch-coverage` (evita interferencia entre tests del mismo file en G6).
- Nunca incluir un objetivo sin contrato ni fixtures (ya filtrado en target selection; doble-check defensivo).
- Persistir decisión en `state/batch-plan.json` con `sizeChosen`, `reason`.
