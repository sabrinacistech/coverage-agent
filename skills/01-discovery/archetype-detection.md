# Archetype Detection (BGBA parent POMs)

## Objetivo
Detectar parent/arquetipo, su versión y consumir el changelog correspondiente para reducir alucinaciones (`javax` vs `jakarta`, JUnit 4 vs 5, Surefire/Failsafe, JaCoCo heredado, etc.).

## Fuentes
- `pom.xml` raíz y de cada módulo (sección `<parent>`).
- Changelogs:
  - `docs/archetypes/changelogs/CHANGELOG_bgba-parent-pom.md`
  - `docs/archetypes/changelogs/CHANGELOG_bgba-parent-paas-java-8.md`
  - `docs/archetypes/changelogs/CHANGELOG_bgba-parent-paas-java-21.md`

## Precedencia
1. `pom.xml` / `build.gradle` reales.
2. Parent/arquetipo declarado.
3. Changelog del parent.
4. Tests existentes.
5. Convenciones del agente.

Si el changelog contradice el POM, el POM gana. Registrar diff en `state/archetype-profile.json#discrepancies`.

## Procedimiento
1. Parsear `<parent>` por módulo: `groupId`, `artifactId`, `version`.
2. Mapear a perfil:
   - `bgba-parent-paas-java-8` ⇒ `archetype: java-8`, asume Spring Boot 2.x, `javax.*`, JUnit 5 si declarado, JaCoCo **no heredado** → agregar plugin al POM (requerido para deploy).
   - `bgba-parent-paas-java-21` ⇒ `archetype: java-21`, Spring Boot 3.x, `jakarta.*`, JaCoCo ya provisto por el parent.
   - `bgba-parent-pom` ⇒ `archetype: common`, leer solo cambios base.
3. Cargar el changelog correspondiente, extraer (vía Python `archetype_detector.py`) breaking changes, cambios en plugins, en testing, JaCoCo, security.
4. Persistir resumen condensado en `state/archetype-profile.json`.

## Salida: `state/archetype-profile.json`

```json
{
  "schemaVersion": 1,
  "modules": [
    {
      "path": "service-foo",
      "parent": { "groupId": "ar.com.bgba", "artifactId": "bgba-parent-paas-java-21", "version": "3.5.1" },
      "archetype": "java-21",
      "implies": {
        "java": "21",
        "springBoot": "3.x",
        "namespace": "jakarta",
        "jacoco": "inherited",
        "junit": "5"
      },
      "changelog": "docs/archetypes/changelogs/CHANGELOG_bgba-parent-paas-java-21.md",
      "rulesApplied": [
        "Forbidden imports: javax.servlet.*, javax.persistence.*",
        "JaCoCo inherited; do NOT add plugin manually"
      ],
      "discrepancies": []
    }
  ]
}
```

## Reglas duras
- `archetype: java-21` ⇒ prohibido `javax.servlet.*`, `javax.persistence.*`, `javax.validation.*`. Usar `jakarta.*`.
- `archetype: java-8` ⇒ prohibido `jakarta.*` y APIs Java 9+ (ver `skills/07-generation/java-8-compatibility.md`).
- `archetype: java-21` ⇒ prohibido agregar plugin JaCoCo manualmente al POM (heredado del parent; el pipeline de OpenShift ya mide con esa config). Si no se detecta JaCoCo, **igual no se toca el POM**: medir por **bootstrap CLI** (medición mandatoria; sin ella ⇒ `BLOCKED_NO_COVERAGE`).
- `archetype: java-8` + sin JaCoCo detectado ⇒ bootstrap por CLI para la **medición local** del agente, **y** agregar el plugin al POM (**requerido para el gate de OpenShift ≥ 80%**) usando el bloque canónico de `docs/archetype-policy.md`. Ver `jacoco-bootstrap.md`.
- Si el parent no es BGBA, marcar `archetype: unknown` y caer al flujo genérico (`state/stack-profile.json` producido por `tools/python/stack_profile_detector.py`).

## Token-saving
El LLM consume solo `state/archetype-profile.json` (compacto). No relee POMs ni changelogs en cada fase.
