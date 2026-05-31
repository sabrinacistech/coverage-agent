# Deterministic Analysis Policy (Runtime Skill — Phase 2)

## Objetivo
Reducir agresivamente el consumo de tokens delimitando con precisi\u00f3n qu\u00e9 puede y qu\u00e9 **no** puede hacer el LLM.

## Regla 1 — Lo determin\u00edstico nunca al LLM

Las siguientes operaciones DEBEN ejecutarse como c\u00f3digo (pre-stage Python, herramientas en `tools/`, o consultas al \u00edndice sem\u00e1ntico). Enviarlas al LLM es un **bug**.

| Operaci\u00f3n                              | Fuente determin\u00edstica                                  |
|----------------------------------------|--------------------------------------------------------|
| Resoluci\u00f3n de imports                  | `state/index/imports.json` + `import-whitelist.json`   |
| Detecci\u00f3n de framework                 | `state/index/annotations.json` (`@RestController`, etc.) |
| Extracci\u00f3n de dependencias             | `state/index/dependencies.json`                        |
| Parseo de errores de compilaci\u00f3n       | `state/compile-error-index.json`                       |
| Parseo de stack traces                  | parser determin\u00edstico en `tools/python/stacktrace.py` |
| Validaci\u00f3n de s\u00edmbolos                | `state/index/classes.json` + `methods.json`            |
| Clasificaci\u00f3n de SUT                   | `state/classification-index.json`                      |
| C\u00e1lculo de classpath                   | `mvn dependency:build-classpath` (cacheado)            |
| Selecci\u00f3n de mutantes PIT              | `state/mutation-intelligence.json`                     |

## Regla 2 \u2014 Solo lo que requiere razonamiento al LLM

El LLM **s\u00ed** maneja:

- **Asserts** sem\u00e1nticamente correctos contra el contrato del SUT.
- **Edge cases** (l\u00edmites, null, vac\u00edo, valores at\u00edpicos) cuando no son derivables de tabla.
- **Naming** de tests legible y consistente.
- **Razonamiento de reparaci\u00f3n compleja** cuando las reglas determin\u00edsticas no aplican (fallback de Phase 6).
- **Lo m\u00ednimo de l\u00f3gica de test** (arrange/act/assert local).

## Regla 3 \u2014 Prompt size caps

| Tipo de prompt          | M\u00e1ximo orientativo (tokens) |
|-------------------------|------------------------------|
| Generation por m\u00e9todo  | 1.2k                         |
| Reparaci\u00f3n por error    | 0.6k                         |
| Aserciones por edge case| 0.4k                         |

Si un agente excede su presupuesto, **dividir** la unidad de trabajo. Nunca aumentar el contexto.

## Regla 4 \u2014 Lo que NUNCA se env\u00eda al LLM

- Repositorios completos.
- POMs/Gradle completos.
- `target/site/jacoco/jacoco.xml` completo (solo el delta).
- Stack traces completos (solo el frame relevante + causa parseada).
- Archivos de test completos cuando solo cambia un m\u00e9todo (ver Phase 4).
- Tablas de \u00edndices completas (solo entradas referenciadas).

## Regla 5 \u2014 Trazabilidad

Cada output del LLM DEBE poder mapearse a:

- un `evidence-id` (s\u00edmbolo del \u00edndice / contrato), **o**
- una regla determin\u00edstica aplicada (`repair-rules/*.rules`), **o**
- una plantilla determin\u00edstica completada (`templates/*.java`).

Outputs sin esa traza ⇒ **descartar**.

## Antipatrones

- "Dale al LLM el POM para que detecte Spring." \u2192 usar `annotations.json`.
- "Que el LLM lea el javap output." \u2192 ya est\u00e1 en `methods.json`.
- "Que el LLM mire el stack trace entero." \u2192 parsear, pasar causa.
- "Reenviar el contrato completo en cada subprompt." \u2192 referenciar por `evidence-id`.
