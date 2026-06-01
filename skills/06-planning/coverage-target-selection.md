# Coverage Target Selection

> **DETERMINISTA — no es un turno del LLM.** Esta fase la ejecuta el pipeline
> Python (`tools/python/jacoco_parser.py (--mode targets)`); este skill documenta el comportamiento, el
> LLM no lo corre. Ver `skills/00-runtime/02-phase-contracts.md`.


## Objetivo
Convertir el JaCoCo XML en una lista priorizada de objetivos en `state/coverage-targets.json`.

## Procedimiento
1. Parsear `target/site/jacoco/jacoco.xml` con DOM/StAX.
2. Por cada `<class>` no excluida (ver `generated-code-policy.md`), por cada `<method>`, calcular:
   - `missedLines`, `coveredLines`, `missedBranches`, `coveredBranches`, `cxty`.
3. Filtrar por modo:
   - `coverage`: `missedLines > 0`.
   - `branch-coverage`: `missedBranches > 0`.
   - `mutation-hardening`: cruce con `mutation-intelligence.json#survivors`.
4. Anotar cada objetivo con:
   - `risk` desde `classification-index.json`,
   - `hasContract: state/symbol-contracts/<fqcn>.json existe`,
   - `hasFixtures: fixture disponible para todos los params`.
5. Excluir objetivos con `hasContract == false` o `hasFixtures == false` (vuelven a fases previas).

## Salida (extracto)

```json
{
  "schemaVersion": 1,
  "mode": "branch-coverage",
  "targets": [
    {
      "id": "tgt:0001",
      "sut": "com.acme.FooService",
      "method": "calc(java.math.BigDecimal)",
      "missedLines": 12,
      "missedBranches": 4,
      "cxty": 6,
      "risk": 0.2,
      "score": 84
    }
  ]
}
```
