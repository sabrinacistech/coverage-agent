# FreeBuilder Classifier

## Objetivo
Marcar tipos FreeBuilder y su estrategia de instanciación admisible para el contrato.

## Procedimiento
1. Por cada interface con `@FreeBuilder` (AST), registrar:
   - propiedades (getters abstractos) y si son `Optional<>` o `@Nullable`.
   - existencia de wrapper `class Builder extends Type_Builder {}`.
   - existencia de `Type_Builder` en `target/generated-sources/annotations`.
2. Asignar etiqueta:
   - `freebuilder.wrapped` si hay wrapper.
   - `freebuilder.generated-only` si solo `Type_Builder`.
   - `freebuilder.unresolved` si no se generó (forzar build previo o excluir).

## Salida
`classification-index.json` añade `tags: ["freebuilder.wrapped"]` o equivalente, y propaga a `symbol-contracts` la estrategia permitida según `freebuilder-test-strategy.md`.
