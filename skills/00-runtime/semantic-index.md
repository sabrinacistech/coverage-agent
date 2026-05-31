# Semantic Index (Runtime Skill — Phase 1)

## Objetivo
Eliminar el reparseo repetido de fuentes Java a lo largo de los agentes. Toda metadata
estructural (clases, métodos, imports, dependencias, anotaciones) se calcula **una vez**
de forma determinística y se consulta desde `state/index/`.

## Contrato de uso

Antes de cualquier análisis estructural, un agente DEBE:

1. Verificar `state/execution-state.json.indexFingerprints` ≠ vacío.
2. Validar `state/index/*.json` contra `state/_schemas/index/*.schema.json`.
3. Si la huella del módulo cambió → solicitar reindexado incremental al pre-stage.
4. Consultar el índice. **Nunca** abrir archivos `.java` para extraer estructura.

Si el índice falta o está desactualizado y el agente no puede solicitar reindexado,
abortar con `BLOCKED_INDEX_MISSING`.

## API conceptual (queries sobre el índice)

```text
classes.byFqcn(fqcn)                 -> ClassEntry
classes.implementing(iface)          -> ClassEntry[]
methods.of(fqcn)                     -> MethodEntry[]
methods.bySignature(fqcn, sig)       -> MethodEntry
imports.in(file)                     -> ImportEntry[]
dependencies.edgesOf(fqcn)           -> EdgeEntry[]
annotations.on(target)               -> AnnotationEntry[]
```

Estas queries son operaciones de lookup sobre JSON ya cargado — no parsing.

## Operaciones permitidas al LLM

El LLM **solo** puede:
- formular qué FQCN/método necesita,
- consumir el resultado de la query,
- razonar sobre asserts/edge cases basándose en él.

El LLM **no** puede:
- inferir imports a partir de nombres simples,
- detectar frameworks por patrones de texto,
- listar dependencias a partir de POM,
- resolver símbolos por heurística textual.

Todas esas operaciones son **determinísticas** (ver `deterministic-analysis-policy.md`).

## Invalidación incremental

El reindexado opera por archivo:

```text
for f in changed_files:
    sha = sha256(f)
    if sha == indexFingerprints[f]: continue
    reindex(f)
    indexFingerprints[f] = sha
```

Solo se reescriben las entradas afectadas en los JSON de índice (merge atómico
`*.tmp` + rename, como el resto de estados).

## Relación con contratos existentes

| Índice                  | Reemplaza parcialmente                       | Sigue siendo autoritativo                |
|-------------------------|----------------------------------------------|------------------------------------------|
| `classes.json`          | Lectura repetida de `.java` para detectar SUTs | `symbol-contracts/<fqcn>.json` para tests |
| `methods.json`          | Re-extracción de firmas por agente           | `symbol-contracts/<fqcn>.json`           |
| `imports.json`          | Inferencia LLM de imports                    | `import-whitelist.json`                  |
| `dependencies.json`     | Reconstrucción del grafo por agente          | `dependency-graph.json` (vista filtrada) |
| `annotations.json`      | Re-detección de `@Service`, `@RestController`| `classification-index.json`              |

El índice es **fuente** para los contratos derivados; nunca al revés.

## Gates relacionados

- G1 (import whitelist) → se construye desde `imports.json` + `dependencies.json`.
- G3 (bytecode-first) → el índice ya respeta precedencia: bytecode > AST.
- G6 (static pre-compile linter) → reutiliza `methods.json` para validar firmas referenciadas.

## Antipatrones

- Llamar al LLM con "lista todas las clases en este módulo".
- Re-parsear un `.java` para verificar un símbolo ya indexado.
- Mantener cachés paralelos por agente en lugar de consultar `state/index/`.
- Regenerar el índice completo cuando solo cambió un archivo.
