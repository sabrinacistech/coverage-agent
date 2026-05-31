# Agent JSON Protocol

## Propósito

Define el formato de intercambio entre los agentes LLM y `test_patch_applier.py`.
Los agentes producen JSONs estructurados; el patcher los materializa en Java.
Ningún agente escribe archivos Java directamente.

Tanto el Body Agent (`test-body-agent`) como el Repair Agent (`repair-agent`) producen
el mismo patch descriptor canónico. La única distinción es el prefijo del `patchId`:
`patch:` para generación inicial, `repair:` para reparación (con el campo adicional `repairOf`).

---

## Response Format Hint

Cuando exista un cliente LLM en `tools/python/`, configurar `response_format`
con el JSON Schema canónico de cada agente:

```python
# test-body-agent / repair-agent
response_format = {
    "type": "json_schema",
    "schema": load_json("state/_schemas/protocols/patch-descriptor.schema.json"),
}

# test-intent-agent
response_format = {
    "type": "json_schema",
    "schema": load_json("state/_schemas/protocols/test-intent.schema.json"),
}
```

Para Anthropic, usar `tools=[{"name": "...", "input_schema": <schema>}]` con
`tool_choice={"type":"tool","name":"..."}`. Para OpenAI, usar
`response_format={"type":"json_schema","json_schema":{"name":"...","schema":<schema>,"strict":true}}`.

El JSON Schema canónico del Patch Descriptor vive en
[`state/_schemas/protocols/patch-descriptor.schema.json`](../state/_schemas/protocols/patch-descriptor.schema.json) y debe declararse como
**response format hint** en cualquier integración LLM (Anthropic
`tools` con `input_schema`, OpenAI structured outputs, JSON-mode con
schema, etc.).

El esquema acepta dos variantes mediante `oneOf`:

1. **Patch válido** — objeto con `patchId` (`patch:<hex>` o `repair:<hex>`),
   `sut`, `testClass` y opcionalmente `fields[]`, `methods[]`, etc.
2. **Bloqueo controlado** — `{ "schemaVersion": 1, "status": "BLOCKED", "blockReason": "..." }`.

Cuando el SDK no soporte schemas, el agente debe igualmente devolver
**únicamente** el JSON, sin markdown fences ni texto adicional, y el
patcher rechazará cualquier salida fuera del schema.

## Patch Descriptor — formato canónico

Todos los JSONs producidos por Body Agent y Repair Agent siguen este esquema.
Los archivos se guardan en `state/_patches/<testClass>.patch.json`.

```json
{
  "schemaVersion": 1,
  "patchId": "patch:<12-hex-chars>",
  "cycle": 1,
  "sut": "com.acme.FooService",
  "testClass": "com.acme.FooServiceTest",
  "testPackage": "com.acme",
  "template": "junit5-mockito",
  "targetModule": "my-module",
  "targetDir": "src/test/java",
  "allowedImports": [
    "org.junit.jupiter.api.Test",
    "org.junit.jupiter.api.extension.ExtendWith",
    "org.mockito.Mock",
    "org.mockito.junit.jupiter.MockitoExtension",
    "static org.assertj.core.api.Assertions.assertThat"
  ],
  "fields": [
    {
      "annotation": "@Mock",
      "type": "FooRepository",
      "name": "fooRepository"
    },
    {
      "annotation": "@Mock",
      "type": "EmailService",
      "name": "emailService"
    }
  ],
  "methods": [
    {
      "name": "shouldReturnResult_whenInputValid",
      "annotations": ["@Test"],
      "body": "// arrange\nwhen(fooRepository.findById(1L)).thenReturn(Optional.of(new Foo()));\n// act\nString result = sut.doFoo(1L);\n// assert\nassertThat(result).isEqualTo(\"expected\");\n// evidence: sym:com.acme.FooService#doFoo:e7a1b2c3, ctor:com.acme.Foo:b3c2d4e5",
      "evidenceIds": [
        "sym:com.acme.FooService#doFoo:e7a1b2c3",
        "ctor:com.acme.Foo:b3c2d4e5"
      ]
    }
  ]
}
```

### Campos obligatorios

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `schemaVersion` | `integer` (= 1) | Versión del protocolo |
| `patchId` | `string` | Identificador único, formato `patch:<hex>` |
| `sut` | `string` | FQCN de la clase bajo test |
| `testClass` | `string` | FQCN de la clase de test a crear/modificar |

### Campos opcionales

| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `cycle` | `integer` | 1 | Ciclo de generación actual |
| `testPackage` | `string` | extraído de `sut` | Paquete Java del test |
| `template` | `string` | `"junit5-mockito"` | Nombre del template en `templates/` |
| `targetModule` | `string` | `""` | Submódulo Maven (ej: `"api"`) |
| `targetDir` | `string` | `"src/test/java"` | Directorio de test relativo al módulo |
| `allowedImports` | `string[]` | `[]` | Imports adicionales a inyectar |
| `fields` | `object[]` | `[]` | Declaraciones de @Mock/@Spy/@Captor |
| `methods` | `object[]` | `[]` | Bloques de @Test a inyectar |

---

## Formato de `fields[]`

```json
{
  "annotation": "@Mock",
  "type": "FooRepository",
  "name": "fooRepository"
}
```

| Campo | Obligatorio | Valores típicos |
|-------|-------------|-----------------|
| `type` | sí | nombre simple o FQCN del tipo |
| `name` | sí | nombre del campo (camelCase) |
| `annotation` | no | `@Mock` (default), `@Spy`, `@Captor`, `@MockBean` |

**Regla**: el `type` debe existir en `state/symbol-contracts/<fqcn>.json` con
`instantiation.strategy == "mock"` o ser una interfaz mockeable confirmada.

---

## Formato de `methods[]`

```json
{
  "name": "shouldReturnResult_whenInputValid",
  "annotations": ["@Test"],
  "body": "<contenido del cuerpo del método, sin llaves externas>",
  "evidenceIds": ["sym:com.acme.FooService#doFoo:e7a1b2c3"]
}
```

| Campo | Obligatorio | Descripción |
|-------|-------------|-------------|
| `name` | sí | Nombre del método (sin paréntesis). Debe ser único en el archivo. |
| `body` | sí | Contenido Java del cuerpo (sin `{}`). Incluir `// evidence:` al final. |
| `annotations` | no | Lista de anotaciones, default `["@Test"]` |
| `evidenceIds` | no | IDs de evidencia citados en el método (desde contratos) |

**Convención de naming** (enforced por `TQG_03_NAMING` —
`tools/python/test_linter.py` / `skills/11-quality/03-test-naming.md`): el nombre
debe matchear una de las **dos** formas aceptadas:
`^should[A-Z]\w*_when[A-Z]\w*$` o `^[a-z]\w+_[a-z]\w+_[a-z]\w+$` (snake, 3
segmentos). Ejemplos válidos: `shouldReturnResult_whenInputValid`,
`shouldThrow_whenInputNull`, `doFoo_emptyInput_returnsEmpty`. Nombres genéricos
(`test1`, `testMethod`) y la forma `testX_escenario` son **rechazados**.

**Regla**: cada símbolo en `body` (`new X()`, `x.method()`, `X.static()`) debe
tener un `evidence-id` correspondiente en `evidenceIds[]`.

**Restricciones de contenido de `body`**: el campo `body` contiene **únicamente** el
cuerpo interno del método. Se prohíbe explícitamente incluir:
- Sentencias `import` o cláusulas `package`
- Declaraciones de clase: `public class`, `class`, `interface`, `enum`
El patcher rechaza cualquier patch con estas construcciones dentro de `body`.

---

## Contrato de bloqueo

Cuando un agente no puede generar un patch válido por indeterminación técnica o falta
de datos críticos, devuelve el contrato de bloqueo:

```json
{ "schemaVersion": 1, "status": "BLOCKED", "blockReason": "<razón detallada>" }
```

El orchestrator lee `status == "BLOCKED"` y registra el caso en `state/failure-memory.json`
sin invocar al patcher. El `blockReason` debe identificar el símbolo o dato faltante.

---

## Reparación — Repair Patch

El Repair Agent produce el mismo formato de patch descriptor, con la distinción de que
`patchId` comienza con `repair:` en lugar de `patch:`:

```json
{
  "schemaVersion": 1,
  "patchId": "repair:a1b2c3d4e5f6",
  "cycle": 2,
  "sut": "com.acme.FooService",
  "testClass": "com.acme.FooServiceTest",
  "repairOf": "patch:abc123def456",
  "errorCode": "E_IMPORT_UNRESOLVED",
  "methods": [
    {
      "name": "shouldReturnResult_whenInputValid",
      "annotations": ["@Test"],
      "body": "...",
      "evidenceIds": [...]
    }
  ]
}
```

El campo `repairOf` referencia el `patchId` original que falló. El Repair Agent
**reemplaza** el método existente si el nombre coincide (colisión intencional).

---

## Flujo de vida de un patch

```
Body Agent genera JSON
        │
        ▼
state/_patches/<testClass>.patch.json
        │
        ▼
test_patch_applier.py --patch <file> --repo <repo> --state state --templates templates \
        --context-pack state/context-packs/<fqcn>.json --whitelist state/import-whitelist.json \
        --out state/generated-tests.json
        │
        ├─ [INITIALIZED] si el archivo .java no existe → desde template
        ├─ [PATCHED]      si el archivo .java existe → inyección de métodos/fields
        └─ [SKIPPED]      si el método ya existe (colisión de nombre)
        │
        ▼
state/generated-tests.json (status: PROPOSED)
        │
        ▼
test_linter.py --test-file ... --whitelist ... --contracts ... --stack-profile ...
        │
        ├─ [PASS] → mvn -Dtest=<testClass> test
        │                 │
        │          [PASS] → status: VALIDATED
        │          [FAIL] → compile_error_parser.py → E_* tokens
        │                         │
        │                   Repair Agent → repair patch JSON
        │                         │
        │                   test_patch_applier.py (re-apply)
        └─ [FAIL G1/G2/G5] → status: DISCARDED
```

---

## Templates disponibles

| `template` value | Archivo fuente | Casos de uso |
|-----------------|----------------|--------------|
| `junit5-mockito` | `templates/junit5-mockito.java` | @Service, @Component, @Repository |
| `webmvc-test` | `templates/webmvc-test.java` | @RestController, @Controller |
| `reactive-test` | `templates/reactive-test.java` | Mono, Flux, @ReactiveController |
| `springboot-test` | `templates/springboot-test.java` | @SpringBootTest integración |

El valor de `template` en el patch JSON debe coincidir con la clasificación del SUT
en `state/classification-index.json`. Si no se especifica, el patcher usa `junit5-mockito`.

---

## Invariantes del protocolo

1. El `patchId` es único por patch y nunca se reutiliza entre ciclos.
2. Los `evidenceIds` deben mapear a `evidenceId` reales en `state/symbol-contracts/<fqcn>.json`.
3. El `testClass` nunca apunta a `src/main/java/**` — el patcher lo rechaza con exit code 3.
4. Los `allowedImports` son la lista *adicional* al template base; no deben duplicar los del template.
5. Si `methods[]` está vacío, el patcher inicializa el archivo desde template sin body (esqueleto listo para siguiente ciclo).
