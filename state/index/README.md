# state/index — Semantic Index Layer (Phase 1)

Persistent, deterministic semantic index of the Java repository. Computed **once** per
relevant change (POM, sources, or `target/classes`) by the Python pre-stage and reused
by every agent.

## Files

| File              | Purpose                                                          | Produced by                |
|-------------------|------------------------------------------------------------------|----------------------------|
| `classes.json`    | FQCN → file, modifiers, kind (class/iface/enum/record), parents  | `tools/python` indexer     |
| `methods.json`    | Methods per class with signature, params, return, visibility     | `tools/python` indexer     |
| `imports.json`    | File → imports[] (FQN, static, on-demand)                        | `tools/python` indexer     |
| `dependencies.json` | Inter-class dependency graph (uses/implements/extends/injects) | `tools/python` indexer     |
| `annotations.json`| Class/method/field → annotations[]                               | `tools/python` indexer     |

## Invariants

- **Single source of truth.** Agents query this index; they do **not** reparse `.java`.
- **Deterministic.** Built from bytecode (`javap`) and JavaParser + SymbolSolver.
- **Incremental.** Only re-indexes files whose SHA-256 changed since last run
  (tracked in `execution-state.json.indexFingerprints`).
- **Versioned.** `version` bumps on schema changes; agents validate before consuming.

## Invalidation rules

| Event                         | Action                                       |
|-------------------------------|----------------------------------------------|
| `.java` file SHA-256 changed  | Re-index that file only                      |
| `pom.xml`/`build.gradle` change | Re-index classpath edges in `dependencies` |
| `target/classes/*.class` newer than indexed FQCN | Re-resolve that class    |
| Schema `version` bump         | Full re-index                                |

## Backward compatibility

Legacy contracts (`state/symbol-contracts/`, `state/dependency-graph.json`) remain
authoritative for **test generation**. The semantic index is an **additive cache**
that agents may query before falling back to legacy contracts.

See `skills/00-runtime/semantic-index.md` and `docs/semantic-index-architecture.md`.
