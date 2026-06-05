# Skill: Spring Boot Static Review

Checklist estático:

- `@RestController` debe depender de servicios/casos de uso, no de repositories.
- DTOs separados de entidades JPA.
- Validaciones con `jakarta.validation` o `javax.validation` en entrada.
- Manejo global de errores con `@ControllerAdvice`.
- `application.yml` sin secretos hardcodeados.
- Actuator para health/metrics.
- Seguridad centralizada si hay endpoints protegidos.
