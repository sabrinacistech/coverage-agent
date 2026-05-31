# Planning Agent

## Responsabilidad
Convertir cobertura real + clasificación + (opcional) mutación en `coverage-targets.json` y `batch-plan.json`.

## Skills
- `skills/06-planning/coverage-target-selection.md`
- `skills/06-planning/coverage-roi-planning.md`
- `skills/06-planning/dynamic-batch-sizing.md`

## Entradas
- `target/site/jacoco/jacoco.xml` (baseline del ciclo).
- `state/classification-index.json`.
- `state/symbol-contracts/*.json`, `state/fixture-catalog.json`.
- (`mutation-hardening`) `state/mutation-intelligence.json`.

## Salidas
- `state/coverage-targets.json` (valida `_schemas/coverage-targets.schema.json`).
- `state/batch-plan.json` (valida `_schemas/batch-plan.schema.json`).

## Reglas
- Excluir targets sin `hasContract` o `hasFixtures` (vuelven a fases previas).
- Ordenamiento por ROI según `coverage-roi-planning.md`.
- Tamaño de batch dinámico según `compileFailRate` histórico.
- En `branch-coverage`: nunca dos targets del mismo SUT en el mismo batch.
