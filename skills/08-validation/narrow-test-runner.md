# Narrow Test Runner

## Objetivo
Ejecutar el menor scope necesario para validar el batch actual y producir delta de cobertura.

## Comando base (Maven)
```bash
mvn -q -pl <module> -am \
    -DfailIfNoTests=false \
    -Dtest=<TestFQCN1>,<TestFQCN2> \
    -Djacoco.destFile=target/jacoco-batch-<n>.exec \
    -Djacoco.dataFile=target/jacoco-batch-<n>.exec \
    org.jacoco:jacoco-maven-plugin:prepare-agent \
    test \
    org.jacoco:jacoco-maven-plugin:report \
    -Djacoco.outputDirectory=target/site/jacoco-batch-<n>
```

## Comando base (Gradle)
```bash
./gradlew :<module>:test --tests "<TestFQCN>" -PjacocoDestFile=build/jacoco/batch-<n>.exec jacocoTestReport
```

## Reglas
- Nunca `mvn clean install` ni `mvn verify` durante ciclos; solo `test` con `-pl/-am`.
- Reusar caché del reactor entre ciclos (no borrar `target/`).
- Si el módulo no compila por código productivo no tocado ⇒ abortar el ciclo y reportar (no intentar reparar productivo).
- Capturar stdout/stderr en `state/runs/cycle-<n>.log` para el parser.
- Timeout duro por ciclo (configurable; default 10 min); excederlo activa convergencia G8.

## Salida
- `state/coverage-summary.json` (resumen JaCoCo del batch).
- `state/coverage-delta.json` (diff contra baseline del ciclo anterior).
- `state/compile-error-index.json` (si hubo errores).
