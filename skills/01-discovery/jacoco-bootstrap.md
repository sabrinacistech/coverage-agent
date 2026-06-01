# JaCoCo Bootstrap

## Objetivo
Garantizar dos cosas distintas (ver `docs/archetype-policy.md` §"dos propósitos"):
1. **Medición del agente:** un `jacoco.xml` utilizable en cada ciclo, idealmente
   **sin modificar** el `pom.xml` (bootstrap CLI).
2. **Gate de despliegue (OpenShift):** que JaCoCo quede **en el build committeado**
   (heredado o plugin en POM) para que el pipeline gatee **branch ≥ 80%**. El
   bootstrap CLI NO cubre esto.

## Decisión

Dado `state/archetype-profile.json` y `state/build-tool-contract.json`:

| Caso | Medición del agente | Gate de despliegue (POM) |
|------|---------------------|--------------------------|
| JaCoCo ya configurado en POM/Gradle | Usar la config existente. NO duplicar. | Ya cubierto. |
| `archetype: java-21` (BGBA) | Heredado (`mvn jacoco:report` / `mvn test`); si **no** se detecta, **bootstrap CLI** (medir es mandatorio). | **Heredado del parent — NO agregar plugin** (ni aunque no se detecte). |
| `archetype: java-8` (BGBA) sin JaCoCo | Bootstrap CLI (sin tocar POM). | **Agregar el plugin al POM (REQUERIDO)** — bloque canónico de `docs/archetype-policy.md`. |
| Parent no BGBA, sin JaCoCo | Bootstrap CLI. | Agregar el plugin al POM (bloque canónico) para poder desplegar. |
| Reporte XML inaccesible tras build | Marcar `BLOCKED_NO_COVERAGE` y abortar el ciclo. | — |

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
- El agente **nunca** toca `src/main`. La **única** modificación permitida en la app
  es agregar el `jacoco-maven-plugin` al POM cuando el arquetipo lo requiere (abajo).
- `archetype: java-21` ⇒ **prohibido** agregar `jacoco-maven-plugin` (heredado del parent).
- `archetype: java-8` (o parent no-BGBA) **sin** JaCoCo ⇒ **agregar el plugin al POM
  (requerido para el gate de OpenShift)** usando el **bloque canónico** de
  `docs/archetype-policy.md` (versión 0.8.13 + `check` branch ≥ 0.80). El bootstrap
  CLI cubre solo la medición local del agente, no el despliegue.
- Capturar la ruta del XML en `state/build-tool-contract.json#jacoco.reportXml` para que `coverage-delta-analysis` la consuma.

## Validación
Tras el primer ciclo, el orquestador valida:
- `jacoco.xml` existe y es parseable.
- Contiene `<counter type="LINE">` y `<counter type="BRANCH">` en al menos una clase.
Si falla ⇒ `BLOCKED_NO_COVERAGE`.

## Token-saving
La política se decide una sola vez por módulo y queda en `state/build-tool-contract.json`. El LLM no necesita razonar sobre JaCoCo en cada ciclo.
