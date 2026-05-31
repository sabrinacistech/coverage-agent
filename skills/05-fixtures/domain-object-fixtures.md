# Domain Object Fixtures

## Objetivo
Fixtures para entidades/agregados con invariantes (no DTOs).

## Reglas
- Respetar invariantes declaradas (constructor que valida, factory `of(...)` que lanza, etc.). Nunca bypassarlas con reflection.
- Si la entidad expone `equals/hashCode` sobre identidad (`id`), las variantes deben usar ids distintos para evitar colisiones accidentales en colecciones.
- Si la entidad tiene asociaciones (`@OneToMany`, listas internas), inicializar con listas vacías por defecto y agregar variantes con elementos verificados.
- Para entidades JPA con `@Version`/`@Id` autogenerado, dejar `id` no nulo en fixtures (string o long determinista).

## Prohibido
- Usar `Unsafe`, `ReflectionTestUtils.setField` para construir un dominio "imposible". Si el constructor no permite el estado deseado, el escenario probablemente no es válido.
