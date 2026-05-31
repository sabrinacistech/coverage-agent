# JaCoCo Bootstrap

## Objetivo
Garantizar que existe un reporte JaCoCo XML utilizable sin modificar el `pom.xml`/`build.gradle` salvo instrucción explícita.

## Decisión

Dado `state/archetype-profile.json` y `state/build-tool-contract.json`:

| Caso | Acción |
|------|--------|
| JaCoCo ya configurado en POM/Gradle | Usar configuración existente. NO duplicar. |
| `archetype: java-21` (BGBA) | NO agregar plugin; el parent lo provee. Usar `mvn jacoco:report` o `mvn test` según binding. |
| `archetype: java-8` (BGBA) sin JaCoCo | Bootstrap CLI: ejecutar agente y reporte sin tocar POM. |
| Parent no BGBA, sin JaCoCo | Bootstrap CLI. |
| Reporte XML inaccesible tras build | Marcar `BLOCKED_NO_COVERAGE` y abortar el ciclo. |

## Bootstrap CLI (sin modificar POM)

Genera `target/jacoco-batch-<n>.exec` y `target/site/jacoco-batch-<n>/jacoco.xml`:

```bash
mvn -q -pl <module> -am \
    -DfailIfNoTests=false \
    -Dtest=<TestFQCN1>,<TestFQCN2> \
    -Djacoco.destFile=target/jacoco-batch-<n>.exec \
    org.jacoco:jacoco-maven-plugin:0.8.13:prepare-agent \
    test \
    org.jacoco:jacoco-maven-plugin:0.8.13:report \
    -Djacoco.dataFile=target/jacoco-batch-<n>.exec \
    -Djacoco.outputDirectory=target/site/jacoco-batch-<n>
```

Gradle equivalente:
```bash
./gradlew :<module>:test --tests "<TestFQCN>" \
    -PjacocoDestFile=build/jacoco/batch-<n>.exec \
    jacocoTestReport
```

## Reglas
- Nunca modificar el POM/Gradle salvo instrucción explícita del usuario.
- Si el archetype es `java-21`, prohibido agregar `jacoco-maven-plugin` (ya viene heredado).
- Si el archetype es `java-8` y el usuario aprueba modificar POM, agregar bloque mínimo (ver `archetype-policy.md`).
- Capturar la ruta del XML en `state/build-tool-contract.json#jacoco.reportXml` para que `coverage-delta-analysis` la consuma.

## Validación
Tras el primer ciclo, el orquestador valida:
- `jacoco.xml` existe y es parseable.
- Contiene `<counter type="LINE">` y `<counter type="BRANCH">` en al menos una clase.
Si falla ⇒ `BLOCKED_NO_COVERAGE`.

## Token-saving
La política se decide una sola vez por módulo y queda en `state/build-tool-contract.json`. El LLM no necesita razonar sobre JaCoCo en cada ciclo.
