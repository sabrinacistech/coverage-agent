# Project Shape

> **DETERMINISTA — no es un turno del LLM.** Esta fase la ejecuta el pipeline
> Python (`tools/python/pom_parser.py`); este skill documenta el comportamiento, el
> LLM no lo corre. Ver `skills/00-runtime/02-phase-contracts.md`.


## Objetivo
Inventariar la forma física del repo: módulos, raíces de source/test, sources generadas y outputs de build.

## Procedimiento
1. Detectar raíz Maven (`pom.xml`) o Gradle (`settings.gradle*`).
2. Enumerar módulos:
   - Maven: parsear `<modules>` del POM (efectivo si está disponible).
   - Gradle: parsear `settings.gradle(.kts)` y `include(...)`.
3. Para cada módulo, listar:
   - `src/main/java`, `src/main/resources`
   - `src/test/java`, `src/test/resources`
   - `target/classes`, `target/test-classes` (Maven) o `build/classes/java/main`, `build/classes/java/test` (Gradle)
   - `target/generated-sources/**`, `target/generated-test-sources/**`
4. Detectar `packaging` (`jar`, `war`, `pom`); módulos `pom` se ignoran como SUT.
5. Confirmar Java vía `maven.compiler.release|source|target` o `sourceCompatibility`.

## Salida (extracto ilustrativo de `state/build-tool-contract.json`)

> Paso **determinista** (`pom_parser.py` + `archetype_detector.py`), no un turno
> del LLM. El shape canónico —y validado— vive en
> `state/_schemas/build-tool-contract.schema.json`; lo de abajo es solo ilustrativo.

```json
{
  "schemaVersion": 1,
  "root": ".",
  "modules": [
    {
      "name": "service-foo",
      "packaging": "jar",
      "sourceRoots": ["src/main/java"],
      "testRoots": ["src/test/java"],
      "generatedSourceRoots": ["target/generated-sources/annotations"],
      "classesDir": "target/classes",
      "testClassesDir": "target/test-classes",
      "java": "1.8"
    }
  ]
}
```
