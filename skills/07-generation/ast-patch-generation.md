# AST Patch Generation (Generation Skill — Phase 4)

## Objetivo
Reducir prompts y evitar regenerar archivos completos. Las modificaciones se expresan
como **parches AST quirúrgicos**.

## Operaciones soportadas

| Operación          | Semántica                                                  |
|--------------------|------------------------------------------------------------|
| `InsertMethod`     | Inserta un método de test en una clase existente o nueva.  |
| `ReplaceAssertion` | Sustituye una expresión `assert*` por otra equivalente.    |
| `AddImport`        | Agrega un import (validado contra `import-whitelist.json`).|
| `AddMock`          | Agrega `@Mock`/`when(...)`/`verify(...)` mínimos.          |
| `AddField`         | Agrega un campo (mock o fixture) en la clase de test.      |
| `AddAnnotation`    | Agrega anotación a clase o método.                         |

## Formato de patch

```json
{
  "patchId": "p-<hash>",
  "targetFile": "src/test/java/com/acme/FooServiceTest.java",
  "sutFqcn": "com.acme.FooService",
  "ops": [
    { "op": "AddImport", "fqn": "org.junit.jupiter.api.Test" },
    { "op": "AddField",  "modifiers": ["@Mock","private"], "type": "BarRepository", "name": "barRepo" },
    { "op": "InsertMethod",
      "anchor": { "kind": "endOfClass" },
      "source": "@Test\nvoid shouldReturnEmpty_whenNotFound() { /* ... */ }"
    }
  ],
  "evidenceIds": ["sym:com.acme.FooService#findById:7c4a1b2e", "sym:com.acme.BarRepository#findById:9e2f3a1d"]
}
```

El aplicador es determinístico (`tools/python/ast_patcher.py`); el LLM **solo** emite
los fragmentos `source` mínimos dentro de operaciones.

## Reglas

- **Cero rewrites**: un patch NO reemplaza el archivo completo.
- **Atomic per-test**: un patch agrupa operaciones de **un** test agregado.
- **Validación previa**: el aplicador valida G1 (whitelist) y G6 (static pre-compile linter) sobre el resultado proyectado, antes de escribir.
- **Idempotencia**: aplicar el mismo patch dos veces es no-op.
- **Reversible**: el aplicador escribe `state/_patches/<patchId>.diff` para auditoría/rollback.

## Inputs mínimos al LLM

Para producir un patch, el prompt incluye **solo**:

- método objetivo (firma + cuerpo, si necesario para razonar) — desde `state/index/methods.json`,
- colaboradores requeridos (firma exclusivamente) — vista filtrada del contrato,
- líneas fallantes (no el archivo completo) — desde `compile-error-index.json`,
- contrato mínimo del SUT — `_views/<batchId>.json`,
- fixtures aplicables — subset de `fixture-catalog.json`.

**Nunca** se envía:

- el archivo de test completo cuando solo se agrega un método,
- el `pom.xml`,
- contratos de colaboradores no usados,
- stack traces completos.

## Backward compatibility

- Si el agente no soporta patches (legacy), puede emitir archivos completos —
  el aplicador detecta el formato y delega al writer clásico.
- El estado `state/generated-tests.json` referencia `patchId` cuando aplica.

## Java String Literal Safety

Todo fragmento `source` / `body` emitido debe ser **Java compilable**. El error
más común y barato de evitar es el *raw control char* dentro de un literal
`String` normal: rompe la compilación con `unclosed string literal` /
`illegal line end in string literal`.

When generating Java tests:

- Never write raw multiline content inside normal Java string literals.
- Any generated Java `String` literal must be valid Java source.
- Escape control characters:
  - newline as `\n`
  - carriage return as `\r`
  - tab as `\t`
  - backslash as `\\`
  - double quote as `\"`
- Do **not** generate Java text blocks (`"""`) unless the project source level is
  known to support them (Java 15+); ver [`java-8-compatibility.md`](java-8-compatibility.md).
- Prefer simple escaped strings for test inputs.

Inválido (newline real dentro del literal):

```java
String value = "a
b	c";
```

Válido (secuencias escapadas):

```java
String value = "a\nb\tc";
```

> **Defensa en profundidad (determinística).** Aunque el agente respete esta
> regla, el aplicador no confía a ciegas: `test_patch_applier.py::sanitize_java_body`
> convierte control-chars reales a escapes durante el render, y un **guard previo
> a la escritura** (`common.has_raw_newline_inside_java_string`) rechaza el patch
> con `INVALID_JAVA_STRING_LITERAL` (exit 2, sin escribir el archivo) si algún
> newline crudo sobrevive dentro de un literal. Para emitir datos de test desde
> Python, usar el helper centralizado `common.java_string_literal(value)`.

## Antipatrones

- "Regenerar el archivo entero por seguridad" ⇒ patch redundante, sube tokens.
- "Adjuntar el archivo de test completo como contexto" cuando solo se agrega un test.
- Emitir `ReplaceAssertion` sin `evidenceId` del símbolo asertado.
- Patches multi-test (rompe atomicidad).
- Newline/tab/CR **reales** dentro de un literal `String` (usar `\n`/`\t`/`\r`); ver *Java String Literal Safety*.
