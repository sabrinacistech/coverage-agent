# DTO Fixtures

## Reglas
- DTOs Lombok `@Data`/`@Builder`: fixture vía builder verificado.
- DTOs Java records (si Java >= 14, fuera de modo Java 8): fixture vía constructor canónico.
- DTOs POJO clásicos (getters/setters): usar constructor + setters listados en contrato.
- Sin builder ni constructor utilizable ⇒ `Mockito.mock` solo si el SUT consume getters; stubear únicamente los getters realmente invocados (cruzar con `dependency-graph.json`).

## Variantes mínimas
- `default`: campos requeridos con valores típicos.
- `null-optional`: campos opcionales en null.
- `empty-collections`: listas/sets vacíos para tipos colección.
- `boundary` (solo `branch-coverage`): mínimo, máximo, vacío.
