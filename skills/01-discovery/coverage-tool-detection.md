# Coverage Tool Detection

## Procedimiento
1. Buscar `org.jacoco:jacoco-maven-plugin` en POM (raíz o módulo) o `jacoco` plugin en Gradle.
2. Si configurado: registrar `reportXml` (default `target/site/jacoco/jacoco.xml`) y `execFile`.
3. Si no configurado: marcar `configured: false`. El narrow runner usará `org.jacoco:jacoco-maven-plugin:prepare-agent` por CLI sin modificar el POM.
4. Detectar exclusiones declaradas (`excludes`) y registrarlas: el planning las respeta.

## Salida
Bloque `jacoco` en `state/build-tool-contract.json`. Si hay otra herramienta (Cobertura, JCov), registrar como `coverage.tool` y abortar si no es JaCoCo (este sistema solo soporta JaCoCo).
