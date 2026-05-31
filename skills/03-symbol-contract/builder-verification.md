# Builder Verification

## Objetivo
Registrar builders disponibles por tipo y la estrategia de instanciación segura. Soporta gates **G2** y la política de builders del MASTER_PROMPT.

## Procedimiento
1. Detectar AP responsable desde `state/stack-profile.json` (`lombok`, `freebuilder`, `mapstruct`, `immutables`, `auto-value`).
2. Para cada candidato:
   - **Lombok `@Builder`**: confirmar anotación en AST y existencia de `Type.builder()` en `target/classes` vía `javap`.
   - **FreeBuilder**: confirmar `@FreeBuilder` en interface y existencia de clase `Type.Builder` o `Type_Builder` declarada/generada.
   - **Immutables**: confirmar clase `ImmutableType` en `target/generated-sources`.
   - **AutoValue**: confirmar `AutoValue_Type`.
   - **Builder manual**: buscar nested class `Builder` en AST con método `build()` y setters declarados.
3. Enumerar setters/with-methods reales con sus tipos. No asumir convención.

## Salida (fragmento del contrato)

```json
{
  "builders": [
    {
      "evidenceId": "builder:com.acme.Order:a91c2d3e",
      "kind": "lombok",
      "entry": "com.acme.Order.builder()",
      "build": "build()",
      "setters": [
        { "name": "id", "type": "java.lang.String", "required": true },
        { "name": "amount", "type": "java.math.BigDecimal", "required": true },
        { "name": "note", "type": "java.lang.String", "required": false }
      ],
      "source": "bytecode:target/classes/com/acme/Order.class"
    }
  ]
}
```

## Reglas
- Prohibido setter no enumerado.
- Prohibido `new Type.Builder()` si solo existe `Type.builder()` estático (y viceversa).
- Si no hay builder verificado y el tipo es necesario para comportamiento, registrar `instantiation.strategy: "mock"` solo si el objeto es pasivo (no se valida su estado).
- FreeBuilder con interface ⇒ ver `docs/freebuilder-policy.md`.
