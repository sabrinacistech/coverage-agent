# Runtime Mode

## Modos soportados
- `coverage`: maximizar líneas cubiertas.
- `branch-coverage`: maximizar ramas.
- `mutation-hardening`: matar mutantes PIT sobrevivientes.

## Efectos por fase

| Fase | `coverage` | `branch-coverage` | `mutation-hardening` |
|------|------------|--------------------|----------------------|
| Planning | ordena por `missedLines DESC` | ordena por `missedBranches DESC` | ordena por survivors PIT |
| Fixtures | valores típicos | valores límite, nulls, vacíos | valores que activen el mutador |
| Generation | un test por método principal | un test por rama | un test por mutante |
| Validation | JaCoCo `LINE` | JaCoCo `BRANCH` | PIT + JaCoCo |
| Reporting | delta de líneas | delta de ramas | mutantes matados |

## Reglas
- El modo se fija al inicio del ciclo y no cambia hasta el siguiente.
- `mutation-hardening` requiere `state/mutation-intelligence.json` no vacío y plugin PIT presente; si no, bloquear con `BLOCKED_NO_PIT`.
- Si el usuario no especifica modo, default = `coverage`.

## Scope de ejecución (Phase 3)

Ortogonal al modo, el agente elige **scope**:

| Scope          | Cuándo                                                  |
|----------------|---------------------------------------------------------|
| `single-file`  | Default desde VS Code, archivo activo.                  |
| `incremental`  | Desde `state/incremental-map.json` (git delta).         |
| `full`         | Solo con flag explícito (`--full`) o invocación CI.     |

Reglas:
- VS Code nunca dispara `full` salvo flag explícito.
- Compile, validation y JaCoCo se narrowean al scope (`-pl`, `-Dtest=`, reporte filtrado).
- Ver `skills/00-runtime/incremental-execution.md`.
