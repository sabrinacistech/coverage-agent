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

## Antipatrones

- "Regenerar el archivo entero por seguridad" ⇒ patch redundante, sube tokens.
- "Adjuntar el archivo de test completo como contexto" cuando solo se agrega un test.
- Emitir `ReplaceAssertion` sin `evidenceId` del símbolo asertado.
- Patches multi-test (rompe atomicidad).
