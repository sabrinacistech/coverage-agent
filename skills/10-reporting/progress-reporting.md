# Progress Reporting

## Frecuencia
Al final de cada ciclo el Orchestrator emite un snapshot.

## Estructura
```json
{
  "cycle": 5,
  "mode": "coverage",
  "batchSize": 5,
  "compiled": 4,
  "passed": 4,
  "discarded": 1,
  "coverageDelta": { "lines": 18, "branches": 6 },
  "convergence": { "consecutiveZeroDeltaCycles": 0, "compileFailRate": 0.0 },
  "nextActions": ["continue", "increase batch to 7"]
}
```

## Reglas
- Snapshot persistido en `state/_summaries/cycle-<n>.json`.
- Si `convergence` dispara G8 ⇒ el siguiente snapshot indica `stop: true` con motivo.
