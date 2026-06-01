# BGBA Archetype Policy

Reglas específicas para repos basados en los parent POMs `bgba-parent-pom`, `bgba-parent-paas-java-8` y `bgba-parent-paas-java-21`.

## Resumen por archetype

| Archetype | Java | Spring Boot | Namespace | JaCoCo | JUnit por defecto |
|-----------|------|-------------|-----------|--------|-------------------|
| `bgba-parent-paas-java-8` | 8 | 2.x | `javax.*` | **Plugin en POM (requerido)** + CLI para medición | 5 (verificar) |
| `bgba-parent-paas-java-21` | 21 | 3.x | `jakarta.*` | **Heredado** del parent (no tocar POM) | 5 |
| `bgba-parent-pom` | - | - | - | - | - |

## JaCoCo: dos propósitos distintos (no confundir)

1. **Medición del agente (local):** el reporte `jacoco.xml` que el agente usa para
   elegir targets y medir `coverage-delta`. Se obtiene por **bootstrap CLI**
   (`jacoco-maven-plugin:prepare-agent ... test ... report`) **sin modificar el POM**.
2. **Gate de despliegue (OpenShift):** el pipeline de deploy corre JaCoCo y **bloquea
   el despliegue si el branch coverage < 80%**. Para esto JaCoCo debe estar **en el
   build committeado**: heredado del parent (java-21) o **como plugin en el POM
   (java-8 / sin herencia)**. El bootstrap CLI **no** cubre este propósito.

⇒ Por eso, en **java-8** agregar el plugin al POM es **requerido** (no opcional): sin
él, la app no pasa el gate de OpenShift. Es la **única** modificación permitida en la
app (el agente nunca toca `src/main`).

## Reglas duras

### Java 21 / Spring Boot 3
- Prohibido `javax.servlet`, `javax.persistence`, `javax.validation`, `javax.ws.rs`. Usar `jakarta.*`.
- Prohibido agregar `jacoco-maven-plugin` al POM — **el parent ya lo provee** y el
  pipeline de OpenShift mide coverage con esa configuración heredada.
- Si por algún motivo **no se detecta** JaCoCo en el build (caso raro), **igual NO se
  agrega al POM**: obtener la medición por **bootstrap CLI**. Conocer el % de coverage
  en cada ciclo es **mandatorio** (sin medición ⇒ `BLOCKED_NO_COVERAGE`).
- Prohibido `JUnit 4` (`org.junit.Test`, `@RunWith`).
- Permitido y preferido: `org.junit.jupiter.api.*`, `@ExtendWith(MockitoExtension.class)`, AssertJ.

### Java 8 / Spring Boot 2
- Prohibido `jakarta.*`.
- Prohibido APIs Java 9+ (ver `skills/07-generation/java-8-compatibility.md`).
- JaCoCo: si el POM **no** lo tiene, **agregar el plugin al POM (requerido)** con el
  bloque canónico de abajo — incluye el `check` de branch ≥ 0.80 para el gate de
  OpenShift. El bootstrap CLI sirve solo para la medición local del agente.
- JUnit: validar; si hay `spring-boot-starter-test` sin `junit-vintage-engine`, usar JUnit 5.

### Comunes
- Nunca cambiar la versión del parent ni dependencias heredadas.
- Nunca duplicar plugins ya provistos por el parent.
- Si el changelog contradice el POM, ganar el POM y registrar `discrepancies[]`.

## Bloque JaCoCo canónico (Java 8 / sin JaCoCo heredado) — REQUERIDO para el gate de OpenShift

> **Fuente única de verdad** del bloque del POM. Cualquier doc/skill que necesite
> mostrarlo debe **referenciar esta sección** en vez de duplicar uno divergente.
> Versión: **0.8.13**. Incluye el `check` de branch ≥ **0.80**.

```xml
<plugin>
  <groupId>org.jacoco</groupId>
  <artifactId>jacoco-maven-plugin</artifactId>
  <version>0.8.13</version>
  <executions>
    <!-- Instrumenta la JVM de test -->
    <execution>
      <id>prepare-agent</id>
      <goals><goal>prepare-agent</goal></goals>
    </execution>
    <!-- Genera target/site/jacoco/jacoco.xml -->
    <execution>
      <id>report</id>
      <phase>test</phase>
      <goals><goal>report</goal></goals>
    </execution>
    <!-- Gate de despliegue: branch coverage >= 80% (falla el build si no se cumple) -->
    <execution>
      <id>check</id>
      <phase>verify</phase>
      <goals><goal>check</goal></goals>
      <configuration>
        <rules>
          <rule>
            <limits>
              <limit>
                <counter>BRANCH</counter>
                <value>COVEREDRATIO</value>
                <minimum>0.80</minimum>
              </limit>
            </limits>
          </rule>
        </rules>
      </configuration>
    </execution>
  </executions>
</plugin>
```

## Atajos para Generation

`state/archetype-profile.json#implies` actúa como **preset** para los agentes de generación (`test-intent-agent` + `test-body-agent`):
- `namespace: jakarta` ⇒ presets de imports incluyen `jakarta.*` y excluyen `javax.*`.
- `namespace: javax` ⇒ a la inversa.
- `junit: 5` ⇒ runner `MockitoExtension`; prohibido `@RunWith`.

Esto reemplaza largas verificaciones por símbolo: ante un import dudoso, basta consultar el preset.
