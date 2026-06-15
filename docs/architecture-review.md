# Architecture Review

`tools/python/run_architecture_review.py` is the compatible CLI entry point for
the static architecture review pilot. The implementation now lives under
`tools/python/architecture/` so the flow can evolve without turning the wrapper
back into a mixed CLI/network/analyzer/reporter script.

## Current Scope

- Reads a remote GitHub or GitHub Enterprise repository through the GitHub REST
  API, with retry/backoff for rate limits and transient server errors.
- Reads local directories and ZIP archives through the same `--repo-uri`
  argument.
- Does not clone the target repository.
- Filters source/config/CI/documentation files.
- Downloads bounded file contents using `--max-files` and
  `--max-bytes-per-file`.
- Builds architecture and dependency maps.
- Emits static findings with `id`, `severity`, `category`, `title`,
  `description`, `evidence`, `recommendation`, `source`, and `confidence`.
- Writes the same output filenames as the pilot.

## CLI

```bash
python tools/python/run_architecture_review.py \
  --repo-uri https://github.com/acme/service.git \
  --branch main \
  --out ./state/architecture_app
```

Local directory and ZIP inputs:

```bash
python tools/python/run_architecture_review.py \
  --repo-uri C:/repoVC/service \
  --out ./state/architecture_app

python tools/python/run_architecture_review.py \
  --repo-uri C:/tmp/service.zip \
  --out ./state/architecture_zip
```

Optional LLM/IDE handoff through the existing `orchestrator.llm_gateway`:

```bash
python tools/python/run_architecture_review.py \
  --repo-uri C:/repoVC/service \
  --out ./state/architecture_app \
  --handoff llm
```

With the default IDE provider, this writes the usual request files under
`<out>/_llm` and waits for the response. The response is stored in
`architecture-reviewer-response.md`.

Supported arguments remain:

- `--repo-uri`
- `--branch`
- `--out`
- `--github-token-env`
- `--github-api-base`
- `--max-files`
- `--max-bytes-per-file`
- `--handoff` (`none` by default, `llm` opt-in)

## Outputs

The tool keeps the existing artifact names:

- `source-inventory.json`
- `architecture-map.json`
- `dependency-map.json`
- `architecture-findings.json`
- `architecture-report.md`

`architecture-findings.json` validates against
`state/_schemas/architecture-findings.schema.json`.

## Modules

- `architecture/cli.py`: argument parsing and top-level flow.
- `architecture/repo_sources.py`: GitHub REST, local directory, and ZIP source
  adapters, URI parsing, path classification, output directory safety.
- `architecture/analyzer.py`: Java/package/import/component extraction. Uses
  `javalang` when available and falls back to regex parsing.
- `architecture/rules.py`: deterministic static rules and finding construction.
- `architecture/reporter.py`: Markdown rendering and atomic JSON/text writes.
- `architecture/models.py`: dataclasses shared across the flow.
- `architecture/handoff.py`: architecture-reviewer handoff contract and opt-in
  LLM gateway execution.

## Design Notes

This is intentionally not wired into FastAPI, LangGraph, or the coverage loop
yet. The review remains a separate tool. The LLM handoff is opt-in and reuses
the gateway only after deterministic artifacts are written.

## Next Steps

- Add scoped `state_validator.py` support for `state/_schemas/architecture/`.
- Add deeper Spring analysis for bean dependencies, package boundaries, and
  controller/service/repository cycles.
- Persist handoff metadata and response summaries as structured JSON.
