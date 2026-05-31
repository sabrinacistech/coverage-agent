# Repository Method Map

## Objetivo
Por SUT, listar exactamente qué métodos de cada repositorio invoca, para limitar los stubs (anti-overstub) y derivar tests negativos.

## Procedimiento
1. AST del SUT con SymbolSolver: por cada llamada `<repoField>.<method>(...)`, resolver firma y tipo de retorno.
2. Para cada método invocado:
   - registrar firma (con generics resueltos),
   - registrar excepciones declaradas (`throws`),
   - registrar si retorna `Optional<X>` (útil para tests de "no encontrado").

## Salida (extracto, dentro de `dependency-graph.json`)

```json
{
  "sut": "com.acme.FooService",
  "collaboratorUsage": [
    {
      "field": "orderRepo",
      "type": "com.acme.OrderRepository",
      "methods": [
        {
          "evidenceId": "sym:com.acme.OrderRepository#findById:a4d2b1e9",
          "name": "findById",
          "params": ["java.lang.String"],
          "returnType": "java.util.Optional<com.acme.Order>",
          "throws": []
        }
      ]
    }
  ]
}
```

## Reglas
- Stubs en tests solo para métodos listados aquí (Test Quality Gate, regla 5).
- Si retorno `Optional`, generar al menos dos tests: `Optional.empty()` y `Optional.of(fixture)`.
