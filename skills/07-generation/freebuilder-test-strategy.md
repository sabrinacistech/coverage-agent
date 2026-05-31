# FreeBuilder Test Strategy

Complementa `docs/freebuilder-policy.md` con procedimiento operativo.

## Detección
- AP detectado en `stack-profile.json` (`freebuilder`).
- Interface anotada con `@FreeBuilder` confirmada por AST.
- Builder generado: `Type_Builder` en `target/generated-sources/annotations`.
- Builder wrapper opcional: nested `class Builder extends Type_Builder {}` declarado por el dev.

## Decisión
1. Si existe wrapper `Type.Builder` ⇒ usar `new Type.Builder().<setters>().build()`.
2. Si no existe wrapper pero `Type_Builder` está generado y accesible ⇒ permitido **solo** si el contrato registró `builder.entry: "new com.acme.Type_Builder()"`. Caso contrario, prohibido.
3. Si el objeto es pasivo para el SUT ⇒ `Mockito.mock(Type.class)` con stubs solo de getters realmente consumidos.
4. Nunca `new Type()`.

## Setters
- Solo los listados en el contrato (`builders[].setters`).
- Propiedades requeridas (sin `@Nullable` y sin `Optional<>` en getter) son obligatorias en el fixture.
- Propiedades `Optional<X>` ⇒ `.setX(x)` recibe `X`, no `Optional<X>` (FreeBuilder unwrap).
- `mapX`, `addX`, `addAllX`, `clearX`, `mutateX`, `mergeFrom`, `buildPartial` permitidos solo si registrados.

## Validación
Cualquier `.setY(...)` sobre un builder cuyo `setters[]` no contiene `y` ⇒ test descartado por G2.
