# Build Tool Adapter

## Objetivo
Aislar Maven vs Gradle detrás de un contrato común `state/build-tool-contract.json` para que el resto del sistema no asuma sintaxis.

> ⚠️ **Estado de soporte actual**
> El **pipeline Python (`tools/python/`)** implementa soporte completo **solo para Maven**.
> Los scripts `pom_parser.py`, `classpath_resolver.py`, `bytecode_scanner.py` y
> `archetype_detector.py` invocan comandos `mvn` directamente.
> **Gradle no está soportado aún en el pipeline determinista.**
> Si el repositorio objetivo es Gradle-only, abortar con
> `BLOCKED_GRADLE_NOT_SUPPORTED_IN_PIPELINE` y notificar al usuario.
> El soporte Gradle (Kotlin DSL + `gradlew`) está marcado como **pendiente**.

## Detección
- Maven: existe `pom.xml` en raíz o módulo. **→ Soporte completo (pipeline Python).**
- Gradle: existe `build.gradle` o `build.gradle.kts`, o wrapper `gradlew`. **→ Soporte pendiente; abortar pipeline con `BLOCKED_GRADLE_NOT_SUPPORTED_IN_PIPELINE`.**
- Multi: si ambos existen, tratar como Maven (prioridad `maven`) y loguear advertencia.

## Comandos abstractos → concretos

| Acción | Maven | Gradle |
|--------|-------|--------|
| Resolver classpath test | `mvn -pl <m> dependency:build-classpath -DincludeScope=test -Dmdep.outputFile=target/cp.txt` | `gradle :<m>:printTestClasspath` (tarea custom) o `gradle :<m>:dependencies --configuration testRuntimeClasspath` |
| Compilar tests | `mvn -pl <m> -am test-compile` | `gradle :<m>:compileTestJava` |
| Ejecutar test único | `mvn -pl <m> -Dtest=<FQCN> -DfailIfNoTests=false test` | `gradle :<m>:test --tests "<FQCN>"` |
| Reporte JaCoCo | `mvn -pl <m> jacoco:report` | `gradle :<m>:jacocoTestReport` |
| POM/efectivo | `mvn -pl <m> help:effective-pom -Doutput=target/effective-pom.xml` | `gradle :<m>:properties` |

## `state/build-tool-contract.json` (ejemplo)

```json
{
  "schemaVersion": 1,
  "tool": "maven",
  "rootPom": "pom.xml",
  "modules": [
    { "name": "service-foo", "path": "service-foo", "packaging": "jar" }
  ],
  "java": "1.8",
  "jacoco": {
    "configured": true,
    "reportXml": "target/site/jacoco/jacoco.xml",
    "execFile": "target/jacoco.exec"
  }
}
```

## Reglas
- Si JaCoCo no está configurado ⇒ generar reporte vía CLI (`org.jacoco:jacoco-maven-plugin:report`) sin modificar el POM.
- Prohibido editar `pom.xml`/`build.gradle` salvo instrucción explícita del usuario.
