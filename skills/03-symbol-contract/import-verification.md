# Import Verification

## Objetivo
Producir `state/import-whitelist.json` y validar que cada import futuro de un test esté en él. Soporta los gates **G1** y **G6**.

## Entradas
- `state/build-tool-contract.json` (classpath y módulos).
- Output de `mvn -pl <m> dependency:build-classpath -Dmdep.outputFile=target/cp.txt -DincludeScope=test`.
- `target/classes`, `target/generated-sources`, `src/main/java`, `src/test/java`.
- `JAVA_HOME/lib/modules` (o `rt.jar` en Java 8) para paquetes del JDK.

## Procedimiento
1. Leer `target/cp.txt` y enumerar jars.
2. Para cada jar: `jar tf <jar>` → extraer FQCNs (`*.class`) y paquetes.
3. Para Java 8: enumerar paquetes JDK con `jar tf $JAVA_HOME/jre/lib/rt.jar`. Para Java 9+: `java --list-modules` + `jmod list <m>`.
4. Para sources locales: walk `src/main/java`, `src/test/java`, `target/generated-sources`; parsear `package` con JavaParser (no regex).
5. Unir todo en `packages[]` y `classes[]` con `origin` (`jdk|dep|source|generated`).

## Salida: `state/import-whitelist.json`

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-05-22T10:00:00Z",
  "module": "service-foo",
  "packages": [
    { "name": "java.util", "origin": "jdk" },
    { "name": "com.acme.foo", "origin": "source" },
    { "name": "com.acme.foo.generated", "origin": "generated" }
  ],
  "classes": [
    { "fqcn": "com.acme.foo.FooService", "origin": "source" },
    { "fqcn": "org.mockito.Mockito", "origin": "dep", "jar": "mockito-core-5.11.0.jar" }
  ]
}
```

## Gate G1 (runtime)
Para cada `import x.y.Z;` del test propuesto:
- `x.y.Z` debe estar en `classes[]`, **o**
- `x.y.*` debe ser un paquete en `packages[]` (wildcard) y `Z` debe existir en ese paquete.
- Falla ⇒ descartar test, registrar `{ "reason": "G1_IMPORT_NOT_WHITELISTED", "import": "x.y.Z" }`.

## Prohibiciones
- No inventar paquetes (`com.fake.*`).
- No usar imports `sun.*` o `com.sun.*` salvo que el código productivo ya los use.
- No usar `import static` de clases sin entrada en `classes[]`.
