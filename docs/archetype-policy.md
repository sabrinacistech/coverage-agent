# BGBA Archetype Policy

Reglas específicas para repos basados en los parent POMs `bgba-parent-pom`, `bgba-parent-paas-java-8` y `bgba-parent-paas-java-21`.

## Resumen por archetype

| Archetype | Java | Spring Boot | Namespace | JaCoCo | JUnit por defecto |
|-----------|------|-------------|-----------|--------|-------------------|
| `bgba-parent-paas-java-8` | 8 | 2.x | `javax.*` | Manual (CLI bootstrap) | 5 (verificar) |
| `bgba-parent-paas-java-21` | 21 | 3.x | `jakarta.*` | **Heredado** (no tocar POM) | 5 |
| `bgba-parent-pom` | - | - | - | - | - |

## Reglas duras

### Java 21 / Spring Boot 3
- Prohibido `javax.servlet`, `javax.persistence`, `javax.validation`, `javax.ws.rs`. Usar `jakarta.*`.
- Prohibido agregar `jacoco-maven-plugin` al POM.
- Prohibido `JUnit 4` (`org.junit.Test`, `@RunWith`).
- Permitido y preferido: `org.junit.jupiter.api.*`, `@ExtendWith(MockitoExtension.class)`, AssertJ.

### Java 8 / Spring Boot 2
- Prohibido `jakarta.*`.
- Prohibido APIs Java 9+ (ver `skills/07-generation/java-8-compatibility.md`).
- JaCoCo: bootstrap por CLI o, con autorización explícita, agregar al POM (ver bloque inferior).
- JUnit: validar; si hay `spring-boot-starter-test` sin `junit-vintage-engine`, usar JUnit 5.

### Comunes
- Nunca cambiar la versión del parent ni dependencias heredadas.
- Nunca duplicar plugins ya provistos por el parent.
- Si el changelog contradice el POM, ganar el POM y registrar `discrepancies[]`.

## Bloque JaCoCo opcional (solo Java 8 + autorización explícita)

```xml
<plugin>
  <groupId>org.jacoco</groupId>
  <artifactId>jacoco-maven-plugin</artifactId>
  <version>0.8.13</version>
  <executions>
    <execution>
      <id>prepare-agent</id>
      <goals><goal>prepare-agent</goal></goals>
    </execution>
    <execution>
      <id>report</id>
      <phase>test</phase>
      <goals><goal>report</goal></goals>
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
