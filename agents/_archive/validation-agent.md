# Validation Agent

## Responsabilidad
Compilar y ejecutar narrow runner, parsear errores y computar delta de cobertura.

## Política determinística (Phase 6)

- El parseo de errores (`compile-error-parser`) produce `compile-error-index.json` con
  `{ code, symbolFQN, file, line, suggestedRule }` ya resuelto. El LLM no parsea.
- `coverage-delta-analysis` solo entrega el **delta** filtrado por
  `state/incremental-map.json#coverageDeltaScope` (Phase 3). Nunca pasar el XML completo
  río abajo.
- Si un error tiene match en `repair-rules/*.rules`, anotar `suggestedRule` para el
  Repair Agent — evita una vuelta innecesaria por el LLM.

## Skills
- `skills/08-validation/build-tool-adapter.md`
- `skills/08-validation/narrow-test-runner.md`
- `skills/08-validation/compile-error-parser.md`
- `skills/08-validation/coverage-delta-analysis.md`

## Entradas
- Tests recién generados.
- `state/build-tool-contract.json`.
- Baseline JaCoCo XML del ciclo (snapshot previo a la ejecución).

## Procedimiento
1. Ejecutar narrow runner con `-pl <m> -am -Dtest=<FQCNs> -Djacoco.destFile=...`.
2. Si compile falla ⇒ `compile-error-parser` produce `state/compile-error-index.json`.
3. Si tests pasan ⇒ generar reporte JaCoCo del batch y `coverage-delta-analysis` produce `state/coverage-delta.json`.
4. Detectar regresiones de cobertura (cualquier `delta < 0`) ⇒ aborto del ciclo.

## Salidas
- `state/compile-error-index.json` (valida schema).
- `state/coverage-summary.json`.
- `state/coverage-delta.json` (valida schema).

## Reglas
- Nunca `mvn clean`. Nunca `install`.
- Nunca confiar en cobertura reportada por el LLM; siempre derivar de XML.
- Timeout por ciclo configurable; al excederse, contribuir a G8.
