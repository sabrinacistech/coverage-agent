# Final Reporting

## Objetivo
Emitir el reporte de cierre con evidencia citable y reproducible.

## Contenido mínimo
- `repo`, `commit` (hash), `branch`, `timestamp`.
- `mode`, ciclos ejecutados, criterio de parada (G8 / budget / objetivo alcanzado).
- Cobertura `before` y `after` con paths a los JaCoCo XML adjuntos.
- Tabla por clase: `lines/branches before/after/delta`, tests añadidos.
- Lista de tests generados con su `evidence-ids`.
- Lista de tests descartados con `reason` (`G1_*`, `G2_*`, `TQG_*`, etc.).
- Lista de fixes aplicados (`failure-memory` exitosos del run).
- Riesgos pendientes y recomendaciones.

## Reglas
- Cobertura siempre derivada de los XML adjuntos, no auto-reportada por el LLM.
- Si hay regresiones de cobertura ⇒ marcar el reporte como `status: REGRESSED` y enumerar.
- Adjuntar `state/execution-state.json` final como anexo.
