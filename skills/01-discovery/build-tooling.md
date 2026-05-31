# Build Tooling Detection

## Procedimiento
1. Si existe `pom.xml` ⇒ `tool: maven`. Verificar wrapper `mvnw`.
2. Si existe `build.gradle` / `build.gradle.kts` ⇒ `tool: gradle`. Verificar `gradlew`.
3. Si ambos coexisten ⇒ priorizar Maven salvo override de config; registrar `risks[].dualBuild`.
4. Ejecutar `mvn -v` o `./gradlew -v` para capturar versión.
5. Para Maven, generar POM efectivo por módulo: `mvn -q -pl <m> help:effective-pom -Doutput=target/effective-pom.xml`.
6. Para Gradle, capturar `./gradlew :<m>:properties > target/gradle-props.txt`.

## Salida
Actualiza `state/build-tool-contract.json` (ver `build-tool-adapter.md`).

## Reglas
- No invocar `mvn install` ni tasks que muten estado global.
- Si el wrapper existe, usarlo en vez del binario global.
