# Coverage Delta Analysis

## Objetivo
Calcular delta real de cobertura entre baseline y ejecución del ciclo, por clase y método.

## Entradas
- Baseline (`--before`): `state/jacoco-baseline.xml` — snapshot determinista del reporte pre-generación, producido por `run_pipeline.py` (paso 8) copiando el `--jacoco-xml`. **Fuente única**: si falta, no se puede medir delta.
- Final (`--after`): `target/site/jacoco-batch-<n>/jacoco.xml` (después del ciclo).

## Procedimiento
1. Parsear ambos XML con DOM/StAX. Para cada `<class>`/`<method>` capturar contadores `LINE`, `BRANCH`, `INSTRUCTION`, `METHOD`, `COMPLEXITY`.
2. Calcular delta = `final.covered - baseline.covered` por contador.
3. Atribuir delta a tests del batch cruzando con `state/generated-tests.json`.
4. Detectar regresiones: si algún contador bajó ⇒ marcar `regression: true` y reportar.

## Salida: `state/coverage-delta.json`

```json
{
  "schemaVersion": 1,
  "cycle": 3,
  "mode": "branch-coverage",
  "totals": {
    "lines":   { "before": 1240, "after": 1305, "delta": 65 },
    "branches":{ "before": 410,  "after": 438,  "delta": 28 }
  },
  "perClass": [
    {
      "fqcn": "com.acme.FooService",
      "lines":   { "before": 40, "after": 58, "delta": 18 },
      "branches":{ "before": 12, "after": 18, "delta": 6 },
      "attributedTests": ["com.acme.FooServiceTest"]
    }
  ],
  "regressions": []
}
```

## Reglas
- Si `totals.lines.delta == 0` en 2 ciclos consecutivos que **midieron** cobertura ⇒ activar G8 (`G8_NO_DELTA`). Un ciclo sin medición fresca (sin `coverage-delta.json`: skip/block estructural, baseline ausente, compile-fail) PRESERVA el contador y no cuenta (M3, `cycle_loop.record_outcome`).
- Si hay `regressions` ⇒ abortar ciclo y reportar.
- Nunca reportar delta calculado por el LLM; siempre derivar de los XML.
