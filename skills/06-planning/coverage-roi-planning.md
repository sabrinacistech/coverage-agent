# Coverage ROI Planning

> **DETERMINISTA — no aplicable al LLM.**
> Esta lógica corre en [`tools/python/coverage_planner.py`](../../tools/python/coverage_planner.py)
> y emite `state/batch-plan.json` (schema: `state/_schemas/batch-plan.schema.json`).
> El LLM **no calcula ROI** ni decide priorización — recibe `batch-plan.json` ya ordenado.

## Contrato (lo que el LLM puede asumir)

`batch-plan.json` es una lista ordenada por ROI descendente. El primer ítem
es el de mayor retorno; el último, el de menor. Cada entry:

```json
{ "targetId": "...", "sut": "...", "method": "...", "score": 0.0, "template": "..." }
```

## Fórmula (referencia técnica para mantenedores de coverage_planner.py)

```
roi         = expectedGain / (estimatedCost · riskPenalty)
expectedGain = missedLines (coverage) | missedBranches (branch-coverage) | survivorCount (mutation-hardening)
estimatedCost = 1 + dependencies + cxty/10
riskPenalty   = 1 + risk
```

Empates: menor `cxty` primero; luego mayor `hasContract && hasFixtures`.
SUTs con fallas previas en `failure-memory.json` ⇒ factor 0.5 al ROI.
