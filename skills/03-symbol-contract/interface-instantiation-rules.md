# Interface & Abstract Instantiation Rules

## Regla principal
Nunca `new Interface()` ni `new AbstractClass()`. La estrategia debe estar declarada en el contrato del tipo.

## Estrategias permitidas (en orden de preferencia)
1. **Builder verificado** (ver `builder-verification.md`).
2. **Static factory** declarada (`X.of(...)`, `X.create(...)`).
3. **Implementación concreta** del mismo paquete o módulo, descubierta por `javap` o AST y registrada en el contrato.
4. **Mockito mock** (`mock(X.class)`) solo si el objeto se usa como colaborador pasivo (no se valida su estado interno).
5. **Anonymous class** solo si el SUT exige una implementación inline trivial y todos los métodos abstractos están enumerados en el contrato.

## Prohibiciones
- `new SomeInterface() { ... }` con métodos inventados.
- Casts a tipos hijo no listados en el contrato (`(ImplX) mock`).
- Usar `spy()` sobre interfaces.

## Salida en contrato
```json
{
  "instantiation": {
    "allowed": true,
    "strategy": "mock|builder|factory|concrete|anonymous",
    "preferred": "<evidenceId>",
    "fallbacks": ["<evidenceId>", "..."]
  }
}
```
