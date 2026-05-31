# State Schemas

Cada archivo `state/*.json` valida contra un schema en esta carpeta. El Orchestrator rechaza estados inválidos.

| Estado | Schema |
|--------|--------|
| `build-tool-contract.json` | `build-tool-contract.schema.json` |
| `stack-profile.json` | `stack-profile.schema.json` |
| `classification-index.json` | `classification-index.schema.json` |
| `import-whitelist.json` | `import-whitelist.schema.json` |
| `symbol-contracts/<fqcn>.json` | `symbol-contract.schema.json` |
| `dependency-graph.json` | `dependency-graph.schema.json` |
| `fixture-catalog.json` | `fixture-catalog.schema.json` |
| `coverage-targets.json` | `coverage-targets.schema.json` |
| `batch-plan.json` | `batch-plan.schema.json` |
| `compile-error-index.json` | `compile-error-index.schema.json` |
| `coverage-delta.json` | `coverage-delta.schema.json` |
| `failure-memory.json` | `failure-memory.schema.json` |
| `execution-state.json` | `execution-state.schema.json` |
| `mutation-intelligence.json` | `mutation-intelligence.schema.json` |

Validar con cualquier validador JSON Schema Draft-07 (Ajv, python-jsonschema, etc.). En caso de incompatibilidad, incrementar `schemaVersion` y publicar migración explícita.
