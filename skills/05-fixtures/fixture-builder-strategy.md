# Fixture Builder Strategy

> **DETERMINISTA — no aplicable al LLM.**
> El árbol de decisión por tipo corre en
> [`tools/python/fixture_catalog_builder.py`](../../tools/python/fixture_catalog_builder.py)
> y emite `state/fixture-catalog.json` (schema: `state/_schemas/fixture-catalog.schema.json`).
> El LLM **no decide estrategia** — consume `fixtures[]` del context-pack.

## Contrato (lo que el LLM lee del context-pack)

```json
{
  "fixtures": [
    {
      "id": "fx-foo-default",
      "type": "com.acme.Foo",
      "strategy": "constructor",
      "constructorEvidence": "ctor:com.acme.Foo:a1b2c3d4",
      "cycleSafe": true,
      "values": { "name": "test", "id": 42 }
    }
  ]
}
```

## Reglas de uso (consumidor LLM)

- Usar **solo** fixtures listadas en `contextPack.fixtures[]`.
- Si la fixture no existe para un tipo requerido → registrar `BLOCKED` para ese caso, no inventar.
- Respetar `strategy`: si dice `mock`, no construir; si dice `constructor`, no buildear.
- `cycleSafe: false` ⇒ no usar en `@BeforeEach` compartido — solo dentro del `@Test`.

## Variantes por modo

Las variantes (null, vacío, límites) ya vienen pre-calculadas en
`fixtures[].variants[]` según `mode` (`coverage` / `branch-coverage` /
`mutation-hardening`). El LLM elige la variante adecuada al escenario, **no**
la inventa.

## Anti-patrones (rechazados por G2)

| Anti-patrón | Por qué falla |
|-------------|---------------|
| `new SomeInterface()` | Interface no instanciable; usar `strategy: mock` o `builder` |
| `mock(X.class)` como SUT | El SUT debe ser real para tener cobertura |
| `SomeClass.builder()` sin Lombok en stack | Builder puede no existir en runtime |
| Setters no listados en `builders[].setters[]` | G2 rechaza |
