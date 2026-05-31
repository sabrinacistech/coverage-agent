# Incremental Execution (Runtime Skill — Phase 3)

## Objetivo
Evitar ejecutar pipelines completas desde VS Code. Solo se procesa el delta.

## Mapa incremental

`state/incremental-map.json` mantiene la propagaci\u00f3n:

```
changedFiles  \u2192  affectedClasses  \u2192  affectedTests  \u2192  coverageDeltaScope
```

C\u00e1lculo (determin\u00edstico):

1. `changedFiles` = `git diff --name-only <since>..HEAD` \u2229 `*.java|pom.xml|*.gradle`.
2. `affectedClasses` = FQCNs de `changedFiles` + FQCNs que dependen de ellos seg\u00fan
   `state/index/dependencies.json` (cierre transitivo limitado por reverse-edges).
3. `affectedTests` = clases test bajo `src/test/**` cuya whitelist incluye alg\u00fan
   FQCN en `affectedClasses`, m\u00e1s tests cuyos archivos est\u00e1n en `changedFiles`.
4. `coverageDeltaScope` = subset de `coverage-targets.json` cuya `fqcn \u2208 affectedClasses`.

## Modos

| Modo runtime    | Scope por defecto              | Comando t\u00edpico                                  |
|-----------------|--------------------------------|-------------------------------------------------|
| `single-file`   | `affectedTests` del archivo abierto | `mvn -o -pl <m> -Dtest=<TestX> test`        |
| `incremental`   | `affectedTests` del incremental-map | `mvn -o -pl <m> -Dtest=<list> test`         |
| `full`          | todos los m\u00f3dulos                | `mvn -pl <m> test` (requiere flag expl\u00edcito) |

`full` solo se invoca con `--full` o desde CI; nunca por defecto desde VS Code.

## Reglas

- **Compile narrowing**: pasar `-pl <module> -am -Dtest=<list>` y NO ejecutar `clean`.
- **JaCoCo parcial**: usar `jacoco:report` sobre el m\u00f3dulo afectado; el reporte se
  cruza con `coverageDeltaScope` antes de presentarse.
- **Validation parcial**: G6 (static pre-compile linter) y G1 (whitelist) solo sobre `affectedTests`.
- **Repair parcial**: el ciclo de repair itera \u00fanicamente sobre tests fallidos del
  scope (ver Phase 6).

## Invalidaci\u00f3n

`incremental-map.json` se invalida si:

- `git HEAD` cambi\u00f3,
- `state/index/*` fue reindexado,
- el usuario fuerza recalculaci\u00f3n con `--refresh`.

Escritura at\u00f3mica (`*.tmp` + rename), igual que el resto de estados.

## Antipatrones

- Lanzar `mvn clean install` desde el orquestador en cada ciclo.
- Cargar JaCoCo XML global cuando solo interesan 3 clases.
- Recalcular el grafo de dependencias en cada ejecuci\u00f3n incremental
  (usar `state/index/dependencies.json`).
- Ejecutar fixtures/generation sobre SUTs fuera de `affectedClasses`.
