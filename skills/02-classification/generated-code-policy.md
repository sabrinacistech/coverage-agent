# Generated Code Policy

## Regla
Nunca generar tests directos contra código en `target/generated-sources/**` ni clases marcadas con `@Generated` (`javax.annotation.processing.Generated`, `javax.annotation.Generated`).

## Excepciones
- Mappers MapStruct: el test va contra la interface (`XMapper`), nunca contra `XMapperImpl`. Instanciar vía `Mappers.getMapper(XMapper.class)`.
- Builders generados (FreeBuilder/Immutables/AutoValue): se usan como herramientas para construir fixtures de tests de otras clases, no como SUT.

## Procedimiento
1. Marcar fuentes generadas en `classification-index.json` con `type: generated`.
2. Excluirlas de `coverage-targets.json`.
3. Si un test propuesto importa una clase generada, validar que esté en la whitelist (G1) y que su uso sea instrumental, no SUT.
