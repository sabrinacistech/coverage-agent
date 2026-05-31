# Method Verification

> **DETERMINISTA — no aplicable al LLM.**
> Esta lógica corre en [`tools/python/bytecode_scanner.py`](../../tools/python/bytecode_scanner.py)
> (+ `source_symbol_enricher.py` para genéricos/Lombok) y emite
> `state/symbol-contracts/<fqcn>.json` (schema: `state/_schemas/symbol-contract.schema.json`).
> El LLM **no invoca `javap`** — consume el contrato como dato de entrada para G2.

## Contrato (lo que el LLM lee)

```json
{
  "methods": [
    {
      "evidenceId": "sym:com.acme.FooService#calc:e7a1b2c3",
      "name": "calc",
      "returnType": "java.math.BigDecimal",
      "params": [{ "type": "java.math.BigDecimal", "name": "amount" }],
      "throws": ["com.acme.DomainException"],
      "usable": true,
      "source": "bytecode"
    }
  ]
}
```

## Reglas de uso (consumidor LLM)

- Prohibido invocar método ausente de `methods[]` o con `usable: false`.
- Para Mockito stubs: matchear firma exacta (returnType + params). No convertir primitivos↔wrappers sin overload evidenciado.
- `void` ⇒ `doNothing()` / `doThrow()`, nunca `when(...).thenReturn(...)`.
- `final` / `static` solo mockeables si `stack-profile.json` declara `mockito-inline` / `MockedStatic`.
- Cada invocación generada DEBE citar el `evidenceId` en el evidence-comment del `@Test`.
