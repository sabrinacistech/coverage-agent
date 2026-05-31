# Coverage Evidence Reporting

## Objetivo
Garantizar que cada afirmación de cobertura del reporte sea reproducible.

## Reglas
- Adjuntar al reporte:
  - `target/site/jacoco-baseline/jacoco.xml`
  - `target/site/jacoco-batch-<n>/jacoco.xml` (cada ciclo)
  - `state/coverage-delta.json`
- Cada tabla del reporte debe citar el archivo y la coordenada (`<class>/<method>`) usados.
- Si un valor no proviene de un XML ⇒ NO se reporta.

## Validación
Al cerrar el reporte, el Reporting Agent re-computa los totales desde los XML adjuntos y compara con los del cuerpo del reporte. Discrepancia ⇒ el reporte se marca `status: INCONSISTENT` y se aborta el cierre.
