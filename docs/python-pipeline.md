# Python pipeline — boundary between deterministic tooling and the LLM

## Principio

Todo lo que es **parseable** (POM, classpath, bytecode, JaCoCo XML, log de Maven) lo hace Python. El LLM solo consume JSON compacto y validado por schema. Resultado:

- **Menos tokens**: nada de POMs/XML/javap en el contexto.
- **Más velocidad**: las tareas deterministas son paralelizables y cacheables.
- **Menos alucinación**: el LLM no puede inventar imports/símbolos porque la whitelist ya está cerrada.

## Diagrama

```
┌────────────────────────────────────────────────────────────────┐
│                  Python pre-stage (tools/python)                │
│                                                                │
│  pom_parser ─┐                                                 │
│              ├─► state/build-tool-contract.json                │
│              │   state/archetype-profile.json                  │
│  archetype_  │                                                 │
│  detector  ──┘                                                 │
│                                                                │
│  generated_code_scanner ─► state/generated-code-index.json     │
│  classpath_resolver     ─► state/import-whitelist.json         │
│  bytecode_scanner       ─► state/symbol-contracts/<fqcn>.json  │
│  jacoco_parser          ─► state/coverage-targets.json         │
│  compile_error_parser   ─► state/compile-error-index.json      │
│  state_validator        ─► (verificación de schemas)           │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│                       LLM (agentes)                            │
│                                                                │
│ discovery / classification / planning / generation / repair    │
│ leen *solo* state/*.json. Cero parseo de POM/XML/javap.        │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                  Tests propuestos → test_linter.py
                              │
                  ┌───────────┴──────────┐
                  │ violations ⇒ rechazar│
                  │ pass ⇒ javac/mvn test│
                  └──────────────────────┘
```

## Qué hace Python vs qué hace el LLM

| Tarea | Quién |
|-------|-------|
| Parsear POM, detectar parent/arquetipo | **Python** |
| Cargar changelogs y mapear a reglas | **Python** |
| Listar plugins generadores (CXF, OpenAPI) | **Python** |
| Resolver classpath de tests | **Python** |
| Listar FQCNs/paquetes válidos (whitelist) | **Python** |
| Extraer contratos de bytecode (`javap`) | **Python** |
| Parsear `jacoco.xml` y producir targets/delta | **Python** |
| Parsear errores de compilación de Maven | **Python** |
| Validar JSON contra schemas | **Python** |
| Lint AST-light de tests propuestos (G1+G6) | **Python** |
| Clasificar SUT, decidir patrón de test | **LLM** |
| Diseñar batch y estrategia de mocks | **LLM** |
| Generar el código del test | **LLM** |
| Decidir repair plan ante fallas | **LLM** |
| Análisis cualitativo de cobertura y reporting | **LLM** |

## Reglas para los agentes

1. **Si una pregunta puede responderla un script Python, no la hace el LLM.** Si un agente necesita una respuesta y el archivo `state/*.json` no existe, debe abortar con `BLOCKED_PRE_STAGE_MISSING` y solicitar correr `run_pipeline.py`.
2. Los agentes citan `evidenceId` de `symbol-contracts/<fqcn>.json`. Si un símbolo no está en contratos, **no se usa**.
3. Antes de emitir un test, el agente lo pasa por `test_linter.py`. Si hay violaciones, **no compila** y entra al ciclo de repair sin ejecutar `mvn`.
4. `compile_error_parser.py` produce `compile-error-index.json` después de cada `mvn test`. El `repair-agent` no parsea logs crudos.

## Caching y reuso

- `run_pipeline.py` mantiene una caché centralizada en `<state-dir>/_summaries/cache.json` (`{ "entries": { "<step>": { "inputHash": "<sha256>" } } }`), no un archivo por script.
- Solo los pasos en `_CACHEABLE_STEPS` (definido en `run_pipeline.py`) participan; los demás corren siempre. Si el `inputHash` del paso coincide, no recomputa. Para reset completo: borrar `<state-dir>/_summaries/cache.json` (por default, `../.agent-state/_summaries/cache.json`).

## Cuándo recorrer todo

- `pom_parser` y `archetype_detector`: una vez por commit de POM.
- `generated_code_scanner`: una vez por commit de POM o de spec.
- `classpath_resolver`: una vez por commit de POM/dependencias.
- `bytecode_scanner`: una vez por compilación nueva de `target/classes`.
- `jacoco_parser`: tras cada ciclo de tests.
- `compile_error_parser`: tras cada `mvn test` que falle compilación.


## Enriquecimiento de contratos desde source

El pipeline incluye `source_symbol_enricher.py` después de `bytecode_scanner.py`. Este paso agrega semántica que `javap` no expone de forma suficiente para Copilot: anotaciones `@FreeBuilder`, existencia de `Type.Builder`, setters legales del builder y estrategia segura de instanciación. Si no puede probar un builder, deja el tipo como `mock` para colaboradores pasivos o bloquea la fixture.
