# state/_summaries — Cycle History Compression (Context Control)

Compressed summaries of completed cycles. Written by the Orchestrator at the end
of each cycle to keep context size bounded across long runs.

## Purpose

The Orchestrator's context budget grows linearly with cycle count if full state
is retained. Once a cycle completes, its detailed state (generated-tests.json,
compile-error-index.json, coverage-delta.json) is summarised and the raw data
is no longer loaded in subsequent prompts.

## File format: cycle-N.json

```json
{
  "cycle": 3,
  "mode": "coverage",
  "completedAt": "2026-05-25T14:32:00Z",
  "stackProfileHash": "sha256:a1b2c3d4...",
  "coverageDelta": { "lines": 12, "branches": 4, "instructions": 38 },
  "testsGenerated": 5,
  "testsDiscarded": 2,
  "repairAttempts": 1,
  "repairsSucceeded": 1,
  "targets": ["com.acme.FooService", "com.acme.BarService"],
  "discardReasons": { "G1_IMPORT_NOT_WHITELISTED": 1, "G6_LINTER_FAIL": 1 },
  "gates": {
    "G8_triggered": false,
    "consecutiveZeroDelta": 0,
    "compileFailRate": 0.0
  },
  "patchFiles": ["003-com.acme.FooService-p0014.diff"],
  "evidenceIds": ["sym:com.acme.FooService#bar:e7a1b2c3", "ctor:com.acme.BarService:f3b2c1d0"]
}
```

## Context control rule

Agents only receive:
- The **current cycle's** full state.
- The **last 2 cycles'** summaries (cycle-N.json), NOT their raw state files.
- Summaries older than 2 cycles are available on disk but NOT loaded in context.

This keeps the Orchestrator's context budget flat regardless of how many cycles run.

## Written by

`tools/python/cycle_summarizer.py` — invoked by the Orchestrator at phase 12
(after Reporting) with the current cycle number and state dir.
