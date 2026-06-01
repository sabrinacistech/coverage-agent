# Guía del Desarrollador — Generación de cobertura con la arquitectura de agentes

Esta guía es el camino completo para que un desarrollador ponga a correr la arquitectura `java-test-coverage-architecture` sobre un microservicio Java y obtenga tests verificables, sin alucinación de paquetes/clases.

> Premisa: la arquitectura separa **trabajo determinista (Python)** y **trabajo creativo (LLM)**. Si saltás el pre-stage Python, el LLM consume muchos más tokens y aparecen los problemas conocidos (imports irresolutos, ciclos largos).

## 0. Requisitos previos

| Herramienta | Versión | Verificación |
|-------------|---------|--------------|
| JDK | 8 o 21 (según archetype) | `java -version` |
| Maven | 3.8+ | `mvn -v` |
| Python | 3.9+ | `python --version` |
| VS Code + GitHub Copilot Chat | actual | extensión activada |

Variables de entorno:
- `JAVA_HOME` apuntando al JDK del proyecto.
- `PATH` con `java`, `javap`, `mvn`, `python`.

Instalar dependencias Python una sola vez:

```powershell
cd C:\Users\l0693685\repoVS\docs-sre\docs\agents\java-test-coverage-architecture\tools\python
python -m pip install -r requirements.txt
```

## 1. Abrir el repo Java en VS Code

Opciones:
- Abrir directamente la carpeta del microservicio (recomendado).
- O agregarla al multi-root workspace junto con `docs-sre`.

Es importante que `cwd` del chat sea la raíz del proyecto Java, no la de `docs-sre`.

## 2. Compilar el proyecto una vez

El pipeline Python necesita `target/classes`, `target/generated-sources` y opcionalmente `target/site/jacoco/jacoco.xml`.

```powershell
cd <ruta-del-microservicio>
mvn -q -DskipTests package
# Opcional: si querés baseline de cobertura desde ya
mvn -q test jacoco:report
```

Reglas:
- **Nunca** correr `mvn clean` entre ciclos.
- Si el archetype es `bgba-parent-paas-java-21`, no agregar `jacoco-maven-plugin` al POM (ya lo hereda).
- Si es `bgba-parent-paas-java-8` y no hay JaCoCo, usar el bootstrap CLI (ver `skills/01-discovery/jacoco-bootstrap.md`).

## 3. Correr el pre-stage Python (Phase 0)

Una sola invocación produce todos los `state/*.json` que el LLM va a consumir:

```powershell
python C:\Users\l0693685\repoVS\docs-sre\docs\agents\java-test-coverage-architecture\tools\python\run_pipeline.py `
  --repo . `
  --out C:\Users\l0693685\repoVS\docs-sre\docs\agents\java-test-coverage-architecture\state `
  --module <nombre-del-modulo> `
  --include-fqcn '^com\.acme\.' `
  --jacoco-xml target\site\jacoco\jacoco.xml `
  --coverage-mode coverage
```

Parámetros:
- `--repo` raíz del microservicio.
- `--out` ruta del directorio `state/` de la arquitectura.
- `--module` nombre del módulo Maven a procesar (si es single-module, el nombre del `<artifactId>` o el de la carpeta).
- `--include-fqcn` regex para limitar contratos de bytecode al paquete del proyecto. Sin esto, escanea todo el classpath.
- `--jacoco-xml` opcional pero recomendado: alimenta `coverage-targets.json`.
- `--coverage-mode` `coverage` | `branch-coverage` | `mutation-hardening`.

Verificá la salida:

```powershell
dir C:\Users\l0693685\repoVS\docs-sre\docs\agents\java-test-coverage-architecture\state\*.json
dir C:\Users\l0693685\repoVS\docs-sre\docs\agents\java-test-coverage-architecture\state\symbol-contracts
```

Debe existir como mínimo:
- `build-tool-contract.json`
- `archetype-profile.json`
- `generated-code-index.json`
- `import-whitelist.json`
- `symbol-contracts/<fqcn>.json` (uno por clase del paquete)
- `coverage-targets.json` (si pasaste `--jacoco-xml`)

Si algo falta o falla la validación de schema, abortar y revisar. No avanzar al chat sin esto.

## 4. Lanzar el agente desde el chat de Copilot

1. Abrir el chat de Copilot en VS Code.
2. Pegar el contenido completo de [BOOT.md](BOOT.md) en el chat.
3. Antes de enviar, completar los parámetros:
   - `repo: <ruta-del-microservicio>`
   - `modules: all` o lista
   - `mode: coverage`
   - `includeFqcn: '^com\.acme\.'`
   - `writeTests: false` (modo propuesta) o `true` (modo aplicar)
   - `coverageGoal:` ajustar si querés metas distintas a 80/60.

Enviar. El agente:
1. Corre `validate_handoff.py` sobre los `state/*.json` (Phase 0 determinista).
2. Con handoff `READY`, arranca **Generation (Phase 8)** consumiendo sólo el
   `handoff-summary.json` + el context-pack compacto (las fases 1-7 no son turnos del LLM).
3. Espera tu confirmación al final de cada turno LLM la primera vez.

## 5. Ciclo de revisión por fase

Para cada fase el agente muestra:
- Precondiciones verificadas (con referencia a schema).
- Comandos exactos ejecutados y salida resumida.
- Estados creados/actualizados con path y SHA-256.
- Gates evaluados (PASS/FAIL).
- Próxima fase.

Responder en el chat con `ok` (o ajustes) para avanzar. A partir del segundo ciclo, el agente avanza automático salvo que un gate falle.

## 6. Modos de ejecución

| Modo | Cuándo usarlo |
|------|---------------|
| `coverage` | Subir cobertura de líneas en un proyecto verde. |
| `branch-coverage` | Cobertura ya alta en líneas, faltan ramas. |
| `mutation-hardening` | Tests existen pero PIT muestra mutantes vivos. Requiere `state/mutation-intelligence.json`. |

## 7. Tamaños de batch (los aplica `coverage_planner.py` vía `coverage-orchestrator`)

| Tipo de SUT | Batch máx. |
|-------------|------------|
| POJO / DTO / value object | 8 |
| Mapper / Validator | 5 |
| Controller / Service | 3 |
| Adapter externo (HTTP, SOAP, MQ) | 1–2 |
| Resilient (retry, circuit-breaker) | 1 |
| Consumer de tipo generado | 1–2 |

## 8. Lectura del reporte final

`cycle_report_builder.py` (Python determinístico) deja:
- `state/_summaries/cycle-<n>.json` por ciclo.
- Reporte final con cobertura antes/después (derivada de los XML JaCoCo reales).
- Lista de tests generados con sus `evidence-id`.
- Tests descartados con `reason` (`G1_IMPORT_NOT_WHITELISTED`, `G6_SYMBOL_UNVERIFIED`, etc.).
- Fixes aplicados / `failure-memory.json` entries.
- Regresiones (si las hay) y siguientes pasos.

Si `writeTests: false`, los tests están en el reporte. Para aplicarlos, repetir con `writeTests: true` o copiarlos a mano a `src/test/java`.

## 9. Si algo falla

| Síntoma | Causa probable | Acción |
|---------|----------------|--------|
| `BLOCKED_PRE_STAGE_MISSING` | Phase 0 no se corrió o falta un JSON. | Volver al paso 3. |
| `G1_IMPORT_NOT_WHITELISTED` | El test propone un import que no está en el classpath. | Revisar `state/import-whitelist.json`; si el import es válido pero falta, regenerar whitelist (cambió el POM). |
| `G3_BYTECODE_FIRST_VIOLATED` | El agente intentó usar regex sobre `.java`. | Regenerar `symbol-contracts` con `bytecode_scanner.py`. |
| `G4_GENERATED_SOURCES_MISSING` | Hay annotation processors pero no `target/generated-sources`. | Correr `mvn -q -DskipTests package` y reejecutar pipeline. |
| `BLOCKED_NO_COVERAGE` | No hay `jacoco.xml` parseable. | Generar el reporte JaCoCo o usar el bootstrap CLI del skill `jacoco-bootstrap`. |
| Imports irresolutos llegan al `javac` | Saltearon `test_linter.py`. | Verificar que `test-body-agent` invoque el linter antes de compilar. |
| El ciclo lleva mucho tiempo | `mvn clean` entre tests, no hay batches, contratos no cacheados. | Ver [docs/performance-tuning.md](docs/performance-tuning.md). |

## 10. Mantenimiento del pre-stage

| Cambio | Re-correr |
|--------|-----------|
| `pom.xml` modificado | `run_pipeline.py` completo |
| Nueva dependencia | `classpath_resolver.py` |
| Recompilación (`target/classes` cambia) | `bytecode_scanner.py` |
| Nuevo spec OpenAPI / WSDL | `generated_code_scanner.py` + recompilar |
| Nuevo `jacoco.xml` | `jacoco_parser.py --mode targets` o `--mode delta` |
| Reset completo | borrar `state/_summaries/cache.json` y `state/*.json` y re-correr |

## 11. Archivos clave de referencia

- [MASTER_PROMPT.md](MASTER_PROMPT.md) — reglas del orquestador y gates G1–G9.
- [BOOT.md](BOOT.md) — punto único de arranque (Phase 0, parámetros, reglas duras, procedimiento).
- [docs/python-pipeline.md](docs/python-pipeline.md) — frontera LLM ↔ Python.
- [docs/performance-tuning.md](docs/performance-tuning.md) — optimizaciones contra ciclos largos.
- [docs/archetype-policy.md](docs/archetype-policy.md) — reglas BGBA `paas-java-8` / `paas-java-21`.
- [skills/01-discovery/archetype-detection.md](skills/01-discovery/archetype-detection.md)
- [skills/01-discovery/generated-code-exclusion.md](skills/01-discovery/generated-code-exclusion.md)
- [skills/01-discovery/jacoco-bootstrap.md](skills/01-discovery/jacoco-bootstrap.md)
- [tools/python/README.md](tools/python/README.md) — manual de scripts.

## 12. Checklist rápido

```
[ ] JDK + Maven + Python en PATH
[ ] pip install -r tools/python/requirements.txt (una vez)
[ ] mvn -q -DskipTests package
[ ] (opcional) mvn -q test jacoco:report
[ ] python run_pipeline.py --repo . --out <state> --module <m> --include-fqcn '^com\.acme\.'
[ ] Verificar state/*.json y state/symbol-contracts/
[ ] Pegar BOOT.md en el chat con parámetros
[ ] Avanzar fase por fase; al fallar un gate, leer la tabla del punto 9
[ ] Revisar reporte final de cycle_report_builder.py (state/_summaries/cycle-<N>-report.json)
[ ] Si writeTests:true, validar diff y correr mvn test
```
