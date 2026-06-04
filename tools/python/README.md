# Python pipeline (tools/python)

Procesos deterministas que el LLM **no** debe ejecutar a mano: parseos de POM, classpath, bytecode, JaCoCo XML y errores de compilación. Generan los `state/*.json` que consume el LLM.

## Por qué

- **Tokens**: el LLM lee solo JSON compacto, no POMs/XML/log/javap.
- **Velocidad**: paralelizable y cacheable por mtime/SHA.
- **Determinismo**: cero invención en la capa de evidencia.

## Scripts

| Script | Salida (`state/`) |
|--------|-------------------|
| `pom_parser.py` | `build-tool-contract.json` |
| `archetype_detector.py` | `archetype-profile.json` |
| `jacoco_pom_guard.py` | gate determinista del **único** edit permitido en el POM (agrega `jacoco-maven-plugin` solo si falta y el arquetipo lo requiere; rechaza si es heredado). Ver `docs/archetype-policy.md`. |
| `generated_code_scanner.py` | `generated-code-index.json` |
| `classpath_resolver.py` | `import-whitelist.json` |
| `bytecode_scanner.py` | `symbol-contracts/<fqcn>.json` |
| `source_symbol_enricher.py` | enriquece contracts con FreeBuilder/Lombok desde source |
| `jacoco_parser.py` | `coverage-targets.json` / `coverage-delta.json` |
| `compile_error_parser.py` | `compile-error-index.json` |
| `test_linter.py` | reporte G1+G6 (stdout / exit code) |
| `stacktrace.py` | JSON mínimo de stack trace para Repair Agent |
| `ast_patcher.py` | parche conservador de imports en test Java |
| `cycle_summarizer.py` | `state/_summaries/cycle-N.json` |
| `incremental_map_writer.py` | `state/incremental-map.json` |
| `semantic_index_writer.py` | `state/index/{classes,methods,imports,dependencies,annotations}.json` |
| `state_validator.py` | valida cualquier `state/*.json` contra `state/_schemas/` |
| `run_pipeline.py` | orquesta todos los pasos anteriores |

## Requisitos

```bash
pip install -r tools/python/requirements.txt
```

Java/Maven en `PATH`. Para multi-módulo Maven, el repo debe haber pasado al menos un `mvn -DskipTests package` para tener `target/classes` y `target/generated-sources`.

## Uso (típico)

```bash
# Pre-build una sola vez por commit (ejecutar desde el repo Java)
mvn -q -DskipTests package

# Bootstrap de evidencia (modo standalone: VS Code abre java-test-coverage-architecture/)
python tools/python/run_pipeline.py \
  --repo <ruta-al-repo-java> \
  --out state \
  --module <modulo> \
  --include-fqcn '^com\.acme\.'

# Modo embebido (arquitectura en docs/agents/java-test-coverage-architecture/ del proyecto):
# python docs/agents/java-test-coverage-architecture/tools/python/run_pipeline.py \
#   --repo . --out docs/agents/java-test-coverage-architecture/state

# El LLM ahora consume solo state/*.json
```

## Validar estado

```bash
# Valida todos los state/*.json contra state/_schemas/*.schema.json
python tools/python/state_validator.py --state state
# Exit 0 = todos válidos o ausentes. Exit 1 = al menos un JSON inválido.
```

## Caché

`run_pipeline.py` mantiene una caché centralizada de input-hash en
`<state-dir>/_summaries/cache.json` para el subconjunto de pasos cacheables
(`_CACHEABLE_STEPS` en `run_pipeline.py`): si el hash de las entradas coincide
con el registrado, ese paso **no recomputa**. Los pasos no listados siempre
corren. Para reset de caché: borrar `<state-dir>/_summaries/cache.json`.

## Convención de errores

- Exit code 0: éxito y JSON válido contra schema.
- Exit code 2: bloqueo recuperable (falta `target/classes`, contrato OpenAPI inexistente, etc.).
- Exit code 3: schema inválido (bug del script).
- stderr humano-legible; stdout JSON cuando aplica.
