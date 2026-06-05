# Skill: Layered Architecture Rules

Reglas piloto:

- Controller -> Service permitido.
- Service -> Repository permitido.
- Controller -> Repository es riesgo alto.
- Domain -> Spring Web es riesgo medio/alto.
- Entity JPA expuesta en API es riesgo medio.
- Configuración con secretos es riesgo crítico.
