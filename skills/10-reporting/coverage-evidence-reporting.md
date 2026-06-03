# Coverage Evidence Reporting

> **DETERMINISTA — no es un turno del LLM.** Esta fase la ejecuta el pipeline
> Python (`tools/python/cycle_report_builder.py`); este skill documenta el comportamiento, el
> LLM no lo corre. Ver `skills/00-runtime/02-phase-contracts.md`.


## Objetivo
Garantizar que cada afirmación de cobertura del reporte sea reproducible.

## Reglas
- Adjuntar al reporte:
  - `state/jacoco-baseline.xml` (baseline `--before`, snapshot de `run_pipeline.py`)
  - `target/site/jacoco-batch-<n>/jacoco.xml` (cada ciclo)
  - `state/coverage-delta.json`
- Cada tabla del reporte debe citar el archivo y la coordenada (`<class>/<method>`) usados.
- Si un valor no proviene de un XML ⇒ NO se reporta.

## Validación
Al cerrar el reporte, el Reporting Agent re-computa los totales desde los XML adjuntos y compara con los del cuerpo del reporte. Discrepancia ⇒ el reporte se marca `status: INCONSISTENT` y se aborta el cierre.
