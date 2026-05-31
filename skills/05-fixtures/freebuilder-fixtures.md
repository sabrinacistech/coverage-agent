# FreeBuilder Fixtures

Aplica a tipos clasificados como `freebuilder.*`.

## Reglas
- Entry: `new Type.Builder()` si `freebuilder.wrapped`; `new Type_Builder()` solo si `freebuilder.generated-only` y el contrato lo autoriza.
- Setters: solo los listados en `builders[].setters`.
- Propiedades `Optional<X>`: `.setX(x)` recibe `X`, no `Optional<X>`.
- Propiedades requeridas: deben tener valor en cada variante o `build()` lanza.
- `buildPartial()` permitido solo en variantes etiquetadas `partial` y nunca en escenarios que validen invariantes.

## Variantes
- `default`: todas las requeridas con valores típicos.
- `minimal`: solo requeridas, opcionales en su default.
- `with-optionals`: requeridas + opcionales pobladas.
- `nullable-set`: campos `@Nullable` explícitamente en null.

## Prohibido
- Inventar setter no listado ⇒ test descartado por G2.
- `new Type()` directo sobre interface ⇒ descartado por `interface-instantiation-rules.md`.
