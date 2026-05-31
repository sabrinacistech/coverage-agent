# Implementation Guide (Lean Edition)

> Step-by-step path to run the lean architecture against a Java microservice
> from VS Code. Everything heavy is done by `tools/python/`; the LLM works on
> minimal surgical inputs.

## Prerequisites

- JDK detected (`java -version`).
- Maven (or Gradle wrapper) in the repo.
- Repo compiles once without tests (`mvn -q -DskipTests package`) to populate
  `target/classes` and `target/generated-sources`.
- Python 3.10+ and `pip install -r tools/python/requirements.txt`.

## Step 1 — Pre-stage (Python, deterministic)

```bash
mvn -q -DskipTests package
python tools/python/run_pipeline.py \
   --repo . \
   --out docs/agents/java-test-coverage-architecture/state \
   --module <module> \
   --include-fqcn '^com\.acme\.' \
   --jacoco-xml target/site/jacoco/jacoco.xml
```

Produces:

- `state/index/{classes,methods,imports,dependencies,annotations}.json`
- `state/build-tool-contract.json`, `state/archetype-profile.json`, `state/generated-code-index.json`
- `state/import-whitelist.json`, `state/symbol-contracts/<fqcn>.json` (one per SUT in scope)
- `state/coverage-targets.json` (if `--jacoco-xml` was provided)

If anything is missing → `BLOCKED_PRE_STAGE_MISSING`.

## Step 2 — Repository Intelligence

Single agent invocation. Projects the semantic index into:

- `state/classification-index.json` (annotations-driven labels + risk score),
- `state/dependency-graph.json` (filtered view of `index/dependencies.json`),
- `state/stack-profile.json` (framework versions from `build-tool-contract.json`),
- refreshed `state/symbol-contracts/<fqcn>.json` / `import-whitelist.json`.

No LLM, no source re-parsing. Owns gates **G3, G4, G5**.

## Step 3 — Incremental Planner

Refreshes `state/incremental-map.json` from `git diff <since>..HEAD`. Computes
`changedFiles → affectedClasses → affectedTests → coverageDeltaScope`. Emits
`state/batch-plan.json` ordered by mode (`coverage` → `missedLines DESC`,
`branch-coverage` → `missedBranches DESC`, `mutation-hardening` → PIT survivors).

Default scope from VS Code: `single-file`. With `--scope incremental`:
incremental. With `--full`: full module.

## Step 4 — Surgical Generator

For each batch item, emits an AST patch (`schemas/ast-patch.schema.json`). The
LLM only fills `InsertMethod.source` (test body) and `ReplaceAssertion.replacement`.
Allowed ops: `InsertMethod`, `ReplaceAssertion`, `ReplaceStatement`, `AddImport`,
`AddMock`, `InsertAnnotation`.

The patcher (`tools/python/ast_patcher.py`) projects the patch result, runs
**G1 (whitelist) + G6 (AST lint)**, and either writes or rejects.

## Step 5 — Narrow Validator

Compiles only `{ test, SUT, directDeps(SUT) }` with:

```bash
mvn -o -pl <module> -am -Dtest=<TestFqcn> \
    -DfailIfNoTests=false \
    -Dcheckstyle.skip=true -Dspotbugs.skip=true -Denforcer.skip=true \
    -Djacoco.destFile=target/jacoco-<patchId>.exec test
```

Never `mvn clean`. Never `install`. Always offline.

On compile failure, `tools/python/compile_error_parser.py` writes
`state/compile-error-index.json` with parsed causes — the LLM never reads raw
`javac` output.

## Step 6 — Deterministic Repair

For each entry in `compile-error-index.json`:

1. Match against `repair-rules/*.rules`.
2. If matched and not `escalateToLLM` → apply AST patch deterministically.
3. If `escalateToLLM(<reason>)` → call LLM with minimal context (failing lines
   ±2, parsed cause, projected contract subset, rule reason).
4. **G7** consults `failure-memory.json` before applying anything.
5. Re-validate via the Narrow Validator.

Maximum 2 deterministic attempts + 1 LLM fallback per test.

## Step 7 — Coverage Cache

After a green Narrow Validator run, `target/jacoco-<patchId>.exec` is merged
into `coverage-cache/<fqcn>.exec` (atomic). Per-class report goes to
`coverage-cache/<fqcn>.json`. The delta lands in `state/coverage-delta.json`.

Full module reports are regenerated only on `--full` or a baseline refresh.

## Step 8 — Reporting

The Reporting Agent emits the final summary:

- coverage delta per class,
- list of generated tests + `evidence-id`s,
- rejected patches with `reason` (`G1_IMPORT_NOT_WHITELISTED`, `G6_SYMBOL_UNVERIFIED`, …),
- applied fixes + `failure-memory.json` entries,
- token totals from `state/token-metrics.json`.

## Anti-patterns to avoid

- `mvn clean` between cycles (kills the cache, kills warm classes).
- Editing `pom.xml` / `build.gradle` without explicit user request.
- Pasting POMs, JaCoCo XML, or stack traces into prompts.
- Generating whole files when a patch suffices.
- Re-applying a fix already FAILED in `failure-memory.json` (G7 blocks it).
- Trusting LLM-reported coverage without a `.exec` merge.
