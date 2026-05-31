# State and Recovery

## Atomicidad
Cada escritura de estado:
1. Escribir `state/<file>.json.tmp`.
2. Validar contra `state/_schemas/<file>.schema.json`.
3. `rename` atómico a `state/<file>.json`.
4. Actualizar `state/execution-state.json.checkpoints` con `{file, sha256, cycle, phase}`.

## Recuperación
Al iniciar:
1. Leer `state/execution-state.json`.
2. Verificar hashes; si alguno no coincide ⇒ degradar a `lastGoodCheckpoint` y registrar en `risks[]`.
3. Reanudar desde la fase indicada.

## `state/execution-state.json`

```json
{
  "schemaVersion": 1,
  "mode": "coverage",
  "cycle": 5,
  "phase": "generation",
  "budget": { "maxCycles": 20, "maxMinutesPerCycle": 10 },
  "consecutiveZeroDeltaCycles": 0,
  "compileFailRateWindow": [0.1, 0.2, 0.0],
  "checkpoints": [
    { "file": "stack-profile.json",  "sha256": "...", "cycle": 1, "phase": "stack-profile" },
    { "file": "failure-memory.json", "sha256": "...", "cycle": 5, "phase": "repair" }
  ],
  "lastGoodCheckpoint": { "cycle": 4, "phase": "reporting" }
}
```

## Reglas
- Nunca borrar estados previos sin checkpoint.
- Nunca editar estado a mano durante un ciclo en curso.
- Si dos agentes corren en paralelo, particionar por SUT (un agente, un FQCN); nunca dos agentes sobre el mismo archivo de estado.
- **`failure-memory.json` DEBE persistirse en `checkpoints[]`** tras cada repair attempt. Su pérdida ante crash rompe G7 (anti-loop) en el ciclo siguiente.

## Archivos obligatorios en checkpoints
Todos los siguientes deben tener una entrada en `checkpoints[]` después de ser escritos por primera vez en un ciclo:

| Archivo | Productor | Fase de escritura |
|---------|-----------|-------------------|
| `stack-profile.json` | `stack_profile_detector.py` | discovery |
| `symbol-contracts/<fqcn>.json` | `bytecode_scanner.py` | symbol-contract |
| `import-whitelist.json` | `classpath_resolver.py` | discovery |
| `batch-plan.json` | `coverage_planner.py` | planning |
| `failure-memory.json` | `gate_runner.py` / `repair_dispatch.py` | repair |
| `execution-state.json` | orchestrator | runtime (self-referencial) |

`state_validator.py` debe alertar si un archivo escrito **no** tiene checkpoint registrado en el mismo ciclo.
