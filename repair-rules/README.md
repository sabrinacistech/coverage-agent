# repair-rules/ — Deterministic Repair Engine (Phase 6)

Reglas determinísticas aplicadas **antes** de invocar al LLM. Cada archivo agrupa
reglas por dominio.

| Archivo            | Dominio                                                  |
|--------------------|----------------------------------------------------------|
| `imports.rules`    | Imports faltantes / ambiguos.                            |
| `mockito.rules`    | Errores típicos de Mockito (`PotentialStubbingProblem`, etc.). |
| `spring.rules`     | Contexto Spring, bean wiring, slices.                    |
| `junit.rules`      | Runner/Extension, `@Test` mal anotado, lifecycle.        |
| `builders.rules`   | FreeBuilder / Lombok / generated builders.               |
| `quality.rules`    | Violaciones G6-quality del linter (`TQG_*`) → reparación o escalado al LLM con la cita del skill `11-quality/NN`. |

## Formato

Cada línea no-comentario es una regla:

```
<errorPattern> => <action>(<args>)
```

Donde `errorPattern` matchea contra `state/compile-error-index.json[*].code|message`.

### Actions

Sólo las del primer bloque son determinísticas hoy
(`tools/python/repair_dispatch.py:_AST_PATCHER_ACTIONS`). El resto se escala al
`repair-agent` LLM con el `_escalateReason` = nombre de la acción. Las dejamos
declaradas en los `.rules` para que el día que se implementen en `ast_patcher.py`
el cambio sea drop-in (un agregado al set, cero cambios en los `.rules`).

**Implementadas (fast-path determinístico):**

- `addImport(<fqn>)`
- `removeImport(<fqn>)`
- `insertAaaComments(<scope>)`
- `removeUnusedStub(<symbol>)`
- `convertMockSutToInjectMocks(<symbol>)`

**Declaradas pero escaladas al LLM (TODO: portar a `ast_patcher.py`):**

- `wrapWith(lenient)` — Mockito strict stubbing.
- `useMockMaker(<maker>)`
- `normalizeMatchers()`
- `replaceCall(<from>, <to>)`
- `addMockBean(<type>)`
- `useBuilder(<fqn>)`
- `addAnnotation(<target>, <fqn>)`
- `setBuilderRequiredFields(...)`
- `triggerAnnotationProcessing()`
- `applyInstantiationStrategy(<type>)`
- `replaceWithContractMethod(<type>, <method>)`
- `replaceWithDeclaredBuilderOrBlock(<type>)`

**Fallback explícito:**

- `escalateToLLM(<reason>)` — usar este cuando el caso requiere juicio.

## Pipeline de repair

1. Resolver causa raíz desde `state/compile-error-index.json` (ya parseado).
2. Buscar regla en `repair-rules/*.rules` por `errorCode` / patrón.
3. Aplicar acción vía `tools/python/ast_patcher.py` (mismo motor que generación).
4. Recompilar **scope incremental** (`-Dtest=<one>`).
5. Si persiste → segunda iteración determinística (max 2).
6. Si aún persiste y no está en `failure-memory.json#FAILED` → `escalateToLLM`.

## Invariantes

- El LLM **nunca** parsea errores ni stack traces.
- G7 (failure-memory) bloquea reaplicar fixes ya fallidos.
- Cada fix aplicado registra `{ errorCode, symbolFQN, fixId, result }` en
  `state/failure-memory.json`.

Ver `agents/repair-agent.md` y `skills/00-runtime/deterministic-analysis-policy.md`.
