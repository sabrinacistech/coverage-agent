# Java Test Coverage Agent Architecture

Arquitectura de agentes y skills para generar tests unitarios en microservicios Java con **cero invención de paquetes/clases**, soportando Java 8+, Maven/Gradle, JUnit 4/5, Mockito, AssertJ, JaCoCo y proyectos con FreeBuilder/Lombok/MapStruct/Immutables/AutoValue.

## Principio central

> El agente no inventa símbolos. Solo puede usar clases, imports, constructores, métodos, builders, fixtures y comandos verificados con `evidence-id`. Si no hay evidencia, no se genera el test.

## Flujo

```text
discovery → stack-profile → classification → symbol-contract
        → dependency-graph → fixtures → planning → generation
        → validation → repair → reporting
```

> **Nota (post-audit 2026-05-28):** las fases `discovery → planning` ya **no son
> turnos LLM** — corren como pre-stage determinista en `run_pipeline.py` y se
> validan en un único gate (`validate_handoff.py`). El LLM solo ejecuta
> `generation` y `repair`. `reporting` es Python determinista
> (`cycle_report_builder.py`). Ver [`BOOT.md`](BOOT.md) §Procedimiento.

## Estructura

```text
agents/              Agentes por fase (orchestrator, test-intent, test-body, repair, reporting, ...)
skills/              Procedimientos accionables por dominio
state/_schemas/      JSON Schemas Draft-07 (validación obligatoria) — único contenido versionado bajo state/
docs/                Notas de arquitectura y políticas
tools/python/        Pre-stage determinista (parsea POM/classpath/javap/JaCoCo)
MASTER_PROMPT.md     Prompt principal con gates G1–G9
```

Los **artefactos generados** (JSONs deterministas, context-packs, summaries, patches, caches)
se escriben en `../.agent-state/` — un directorio hermano del repo, fuera del árbol versionado.
Esta separación garantiza que el repo solo contenga código y contratos (schemas), nunca outputs
de ejecución. El path se sobrescribe vía `--out` (Python) o `-StateDir` (`run_agents.ps1`).

## Pre-stage Python (obligatorio)

Antes de cualquier ciclo LLM, correr el pipeline determinista que produce todos los `state/*.json`. Esto **reduce drásticamente los tokens** consumidos y acelera la generación (ver [`docs/performance-tuning.md`](docs/performance-tuning.md) y [`docs/python-pipeline.md`](docs/python-pipeline.md)).

```bash
mvn -q -DskipTests package
python tools/python/run_pipeline.py \
   --repo . \
   --out ../.agent-state \
   --module <module> \
   --include-fqcn '^com\.acme\.' \
   --jacoco-xml target/site/jacoco/jacoco.xml
```

## BGBA archetypes

Detección automática de `bgba-parent-pom`, `bgba-parent-paas-java-8` y `bgba-parent-paas-java-21` con reglas derivadas (`javax` vs `jakarta`, JaCoCo heredado vs manual, JUnit 4 vs 5). Ver [`docs/archetype-policy.md`](docs/archetype-policy.md) y la skill [`archetype-detection`](skills/01-discovery/archetype-detection.md).

## Código autogenerado

CXF (`wsdl2java`), OpenAPI Generator, Lombok, FreeBuilder, MapStruct, Immutables y AutoValue se detectan y excluyen del universo de SUT vía [`generated-code-exclusion`](skills/01-discovery/generated-code-exclusion.md) → `state/generated-code-index.json`.

## Gates anti-alucinación

| Gate | Qué bloquea |
|------|-------------|
| G1 | Imports fuera de `import-whitelist.json` |
| G2 | Símbolo sin `evidence-id` en contrato |
| G3 | Contratos derivados de regex (forzar bytecode/AST) |
| G4 | Generated sources no indexados con APs declarados |
| G5 | Generation sin `stack-profile.json` válido |
| G6 | Static pre-compile linter sobre el test |
| G7 | Re-aplicación de fix ya fallido |
| G8 | Convergencia (delta=0 dos ciclos o compile-fail-rate > 0.5) |
| G9 | Diagnósticos JDT/compilación normalizados (sin inferencia libre) |

> G4 está reportado como `NOT_IMPLEMENTED` por `gate_runner.py` (pendiente). El
> resto de los gates se hacen cumplir de forma determinista — G1/G2/G5 también
> dentro de `test_patch_applier.py`, que es el único punto que escribe Java.

## Modos

- `coverage` — maximiza líneas.
- `branch-coverage` — maximiza ramas y caminos.
- `mutation-hardening` — endurece tests con PIT sobre mutantes sobrevivientes.

## Validación de estados

Todos los `<state-dir>/*.json` validan contra schemas en `state/_schemas/` (dentro del repo,
no movibles). Escritura atómica (`*.tmp` + rename) y hashes SHA-256 en `<state-dir>/execution-state.json`.

## VS Code + GitHub Copilot

Para ejecutar desde Visual Studio Code, usar la guía [`docs/vscode-copilot-execution-guide.md`](docs/vscode-copilot-execution-guide.md) y las instrucciones de proyecto en `.github/copilot-instructions.md`. La arquitectura ahora incluye controles específicos para diagnósticos JDT/Copilot: imports no resueltos, `new Interface()`, uso directo de `Type_Builder` y setters/métodos inventados.

## Cómo arrancar

1. Leer [`BOOT.md`](BOOT.md) — punto único de entrada (parámetros, Phase 0, reglas duras, procedimiento).
2. Leer `MASTER_PROMPT.md` — contrato técnico (gates G1–G9, schemas, división del trabajo).
3. Correr el pre-stage Python con auto-detección: `python tools/python/bootstrap.py --repo <ruta>`. Para overrides manuales, invocar `tools/python/run_pipeline.py` directamente.
4. Ejecutar Orchestrator con `mode` y `budget` pegando `BOOT.md` en el chat (o cargándolo desde el agente).
5. Validar cada test generado con `tools/python/test_linter.py` antes de compilar.
6. Inspeccionar `state/execution-state.json` y los `state/_summaries/cycle-*.json` para progreso.
7. Reporte final emitido determinísticamente por `tools/python/cycle_report_builder.py` (migrado desde el ex-`reporting-agent`; no requiere turno LLM).

Para detalles operativos del día a día, ver [`docs/developer-guide.md`](docs/developer-guide.md).
