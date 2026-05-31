# Testability Classifier

## Objetivo
Asignar score y categoría a cada clase productiva. Alimenta `state/classification-index.json`.

## Señales
- **Tipo**: `service`, `controller`, `repository`, `mapper`, `util`, `config`, `dto`, `generated`, `entity`, `exception`.
- **Tamaño**: LOC, número de métodos públicos.
- **Complejidad**: ciclomática aproximada por método (contar `if/for/while/case/catch/&&/||/?:`).
- **Dependencias**: cantidad de inyecciones, llamadas a estáticos externos.
- **Tests existentes**: cobertura actual de JaCoCo si disponible.
- **Riesgo de compilación**: uso de generics complejos, AP, reflection.

## Score (0–100)
`score = w1·gapDeCobertura + w2·complejidad − w3·riesgo`. Pesos default `w1=0.5, w2=0.3, w3=0.4`.

## Salida (extracto)

```json
{
  "schemaVersion": 1,
  "classes": [
    {
      "fqcn": "com.acme.FooService",
      "type": "service",
      "loc": 240,
      "publicMethods": 9,
      "cyclomatic": 22,
      "coverage": { "lines": 0.31, "branches": 0.18 },
      "risk": 0.2,
      "score": 78
    }
  ]
}
```

## Exclusiones
- Clases con `@Generated` o ubicadas en `target/generated-sources` ⇒ tipo `generated`, no candidatas.
- Configs Spring puras (`@Configuration` sin lógica) ⇒ tipo `config`, score=0 salvo flag explícito.
- DTOs Lombok puros ⇒ score=0 (cobertura por uso, no por test directo).
