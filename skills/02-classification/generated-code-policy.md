# Generated Code Policy

## Regla
Nunca generar tests directos contra código en `target/generated-sources/**` ni clases marcadas con `@Generated` (`javax.annotation.processing.Generated`, `javax.annotation.Generated`).

## Excepciones
- Mappers MapStruct: el test va contra la interface (`XMapper`), nunca contra `XMapperImpl`. Instanciar vía `Mappers.getMapper(XMapper.class)`.
- Builders generados (FreeBuilder/Immutables/AutoValue): se usan como herramientas para construir fixtures de tests de otras clases, no como SUT.

## Procedimiento
1. Marcar fuentes generadas en `classification-index.json` con `type: generated/excluded` (legacy: `generated`). Lo hace `classification_analyzer.py` (Regla 1) a partir de `generated-code-index.json`.
2. Excluirlas como objetivos de cobertura. La aplicación determinista vive en `coverage_planner.py` (`_build_excluded_set`), que descarta del `batch-plan.json` todo `target` cuyo `sut` esté marcado `generated/excluded` ANTES de scorear. Es el primer punto del pipeline donde coexisten `coverage-targets.json` (paso 8) y `classification-index.json` (paso 10) — `jacoco_parser` aún no dispone de la clasificación y por eso no puede filtrar ahí. **Fuente única de esta regla: el planner.**
3. Si un test propuesto importa una clase generada, validar que esté en la whitelist (G1) y que su uso sea instrumental, no SUT.
