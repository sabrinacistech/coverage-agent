# Fixture Catalog

## Objetivo
`state/fixture-catalog.json` es el único origen permitido de datos de prueba.

## Procedimiento
1. Para cada tipo requerido por el batch (parámetros del SUT y de colaboradores), buscar estrategia en este orden:
   1. Builder verificado (`symbol-contracts/<fqcn>.json#builders`).
   2. Constructor verificado.
   3. Static factory verificada.
   4. Mockito mock (solo si pasivo).
2. Definir fixture con `id`, `type`, `strategy`, `args/setters` y variantes (`default`, `boundary`, `null`, `empty`).
3. Resolver dependencias transitivas: si un setter requiere `OtherType`, crear fixture para `OtherType` antes.
4. Detectar ciclos (`A→B→A`): marcar `cycleSafe: false` y exigir mock para uno de los lados.

## Salida (extracto)

```json
{
  "schemaVersion": 1,
  "fixtures": [
    {
      "id": "fx:Order:default",
      "type": "com.acme.Order",
      "strategy": "builder",
      "builderEvidence": "builder:com.acme.Order:a91c2d3e",
      "values": {
        "id": "ord-001",
        "amount": "10.00",
        "note": null
      },
      "variants": ["default","boundary-zero-amount","null-note"],
      "cycleSafe": true
    }
  ]
}
```

## Reglas
- Nada de `new Random()`, `UUID.randomUUID()` sin seed.
- Valores deterministas y representativos del dominio.
- Tipos primitivos: variantes `0`, `1`, `MIN`, `MAX`, `-1` cuando aplique al modo `branch-coverage`.
