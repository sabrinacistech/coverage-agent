# Architecture Reviewer Agent

Responsabilidad:
- Interpretar mapas e inventarios generados por `run_architecture_review.py`.
- Convertir hallazgos determinísticos en recomendaciones arquitectónicas.
- No escribir reportes dentro de `agents/`.

Inputs esperados:
```text
state/architecture_app/
  source-inventory.json
  architecture-map.json
  dependency-map.json
  architecture-findings.json
```
