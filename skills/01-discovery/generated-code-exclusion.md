# Generated Code Exclusion (CXF / OpenAPI / Annotation Processors)

## Objetivo
Identificar y **excluir** del universo de SUTs todo lo que es código autogenerado, y registrar los paquetes/contratos involucrados para que la generación de tests trate esas clases solo como tipos auxiliares.

## Fuentes
- `pom.xml` / `build.gradle` (plugins).
- `target/generated-sources/**` y `build/generated/**`.
- Contratos: `*.wsdl`, `*.yaml`, `*.yml`.
- Anotaciones `@Generated` en bytecode.

## Detección

### CXF Codegen Plugin (WSDL)
```xml
<groupId>org.apache.cxf</groupId>
<artifactId>cxf-codegen-plugin</artifactId>
```
Por cada `<wsdlOption><wsdl>...</wsdl></wsdlOption>`:
- Resolver path (incluyendo `${project.basedir}`).
- Si el `.wsdl` no existe ⇒ registrar como `BLOCKED_MISSING_CONTRACT`.
- Registrar `wsdl: <path>` y los paquetes generados.

### OpenAPI Generator Maven Plugin
```xml
<groupId>org.openapitools</groupId>
<artifactId>openapi-generator-maven-plugin</artifactId>
```
- Resolver `<inputSpec>`, `<apiPackage>`, `<modelPackage>`, `<sourceFolder>`.
- Si el spec no existe ⇒ `BLOCKED_MISSING_CONTRACT`.

### Otros annotation processors
- Lombok, FreeBuilder, MapStruct, Immutables, AutoValue (ver `state/stack-profile.json`).
- Marcar carpetas `target/generated-sources/annotations`.

## Reglas de exclusión

Excluir como SUT (no se generan tests directos):

- Cualquier clase bajo `target/generated-sources/**`, `build/generated/**`, `src/generated/**`.
- Cualquier clase con anotación `@javax.annotation.Generated` o `@javax.annotation.processing.Generated`.
- Cualquier clase dentro de `apiPackage` o `modelPackage` declarados por OpenAPI Generator.
- Cualquier clase derivada de WSDL CXF (típicamente bajo el `sourceRoot` del plugin).
- Interfaces FreeBuilder (`@FreeBuilder`) salvo que tengan lógica `default` significativa.

Las clases productivas que **consumen** clases generadas sí son SUT; las generadas se usan solo como tipos auxiliares previa validación contra el contrato.

## Salida: `state/generated-code-index.json`

```json
{
  "schemaVersion": 1,
  "module": "service-foo",
  "generators": [
    {
      "kind": "openapi",
      "spec": "src/main/resources/openapi/foo.yaml",
      "specExists": true,
      "apiPackage": "com.acme.api",
      "modelPackage": "com.acme.api.model",
      "sourceFolder": "target/generated-sources/openapi/src/main/java"
    },
    {
      "kind": "cxf",
      "wsdl": "src/main/resources/wsdl/foo.wsdl",
      "wsdlExists": true,
      "packages": ["com.acme.ws.client", "com.acme.ws.types"]
    },
    { "kind": "lombok" }
  ],
  "excludedFqcns": ["com.acme.api.model.OrderDto", "com.acme.ws.types.ObtenerClienteResponse"],
  "excludedPackages": ["com.acme.api.model", "com.acme.ws.types", "target/generated-sources/**"],
  "blocked": []
}
```

## Reglas duras
- Si una clase referenciada en un test no aparece en `excludedFqcns` **ni** en `state/import-whitelist.json` ⇒ G1 falla.
- Si el contrato (`.wsdl`/`.yaml`) no contiene un tipo, está prohibido inventarlo aunque aparezca en imports.
- `OpenApiGenerator` con `sourceFolder` no estándar ⇒ honrar el path real, no asumir `target/generated-sources/openapi/src/main/java`.

## Token-saving
La whitelist + generated-code-index se construyen una sola vez en Python (`generated_code_scanner.py`) y reutilizan entre ciclos. El LLM nunca vuelve a leer POMs ni contratos.
