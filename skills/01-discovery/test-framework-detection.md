# Test Framework Detection

> **DETERMINISTA — no es un turno del LLM.** Esta fase la ejecuta el pipeline
> Python (`tools/python/stack_profile_detector.py`); este skill documenta el comportamiento, el
> LLM no lo corre. Ver `skills/00-runtime/02-phase-contracts.md`.


Alimenta `stack-profile.json`. Bloqueante para G5.

## Procedimiento
Por módulo, escanear dependencias (alcance `test`):
- `junit:junit` ⇒ JUnit 4 (registrar versión).
- `org.junit.jupiter:junit-jupiter*`, `org.junit.platform:*` ⇒ JUnit 5.
- `org.mockito:mockito-core` / `mockito-inline` / `mockito-junit-jupiter` ⇒ Mockito (versión y features).
- `org.assertj:assertj-core` ⇒ AssertJ.
- `org.hamcrest:hamcrest*` ⇒ Hamcrest.
- `org.springframework:spring-test`, `org.springframework.boot:spring-boot-starter-test` ⇒ Spring Test slices.
- `org.testcontainers:*` ⇒ Testcontainers.
- `org.pitest:pitest-maven` ⇒ PIT habilitado.

## Coexistencia JUnit 4 + 5
Si ambas presentes, registrar `dualJUnit: true` y elegir preferencia según mayoría de tests existentes (parsear `src/test/java` para imports). Generation respeta la preferida.

## Salida
Bloque `modules[].test`, `modules[].mock`, `modules[].assert` en `state/stack-profile.json`.
