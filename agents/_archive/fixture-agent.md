# Fixture Agent

## Responsabilidad
Construir `state/fixture-catalog.json` con datos de prueba deterministas usando solo símbolos verificados.

## Skills
- `skills/05-fixtures/fixture-catalog.md`
- `skills/05-fixtures/dto-fixtures.md`
- `skills/05-fixtures/domain-object-fixtures.md`
- `skills/05-fixtures/freebuilder-fixtures.md`

## Entradas
- `state/symbol-contracts/*.json`
- `state/dependency-graph.json`
- `state/stack-profile.json` (modo y annotation processors).

## Salida
- `state/fixture-catalog.json` (valida `_schemas/fixture-catalog.schema.json`).

## Reglas
- Estrategia en orden: builder verificado → constructor → factory → mock pasivo.
- Variantes mínimas: `default`, `boundary` (solo `branch-coverage`), `null-optional`, `empty-collections`.
- Detectar y romper ciclos vía mock parcial.
- Nada de aleatoriedad sin seed; nada de `LocalDateTime.now()` sin `Clock` controlado.
