"""state_validator.py — validate state/*.json against state/_schemas/*.schema.json.

Correcciones implementadas:

  1. Acepta tanto --state como --state-dir (alias). Si se pasan ambos,
     --state-dir tiene prioridad y se emite un warning.

  2. symbol-contract.schema.json valida state/symbol-contracts/*.json
     (uno por FQCN), NO state/symbol-contract.json (que no existe y no
     debe existir). El manifest state/symbol-contracts.json se trata como
     estado auxiliar.

  3. context-pack.schema.json valida state/context-packs/*.json
     (uno por SUT), NO state/context-pack.json (que no existe y no
     debe existir).

  4. Archivos state/*.json sin schema asociado se reportan como
     [INFO] ... has no schema; treated as auxiliary state
     en lugar de quedar como estados ambiguos o silenciados.

  5. Archivos ausentes se tratan según su origen:
       - Escritos por el pipeline Python (steps 1-5, 9-10, siempre) → [ERR] si faltan.
       - Escritos condicionalmente por el pipeline Python            → [SKIP] con motivo.
       - Escritos por agentes LLM (fase posterior al pipeline)       → [SKIP] con motivo.
     Solo los archivos verdaderamente runtime/opcionales reciben [SKIP].

  6. Formato de salida estandarizado:
       [OK]   state/<file>.json                 — válido
       [SKIP] <name>.json — <motivo>            — ausente pero legítimamente opcional
       [INFO] state/<file>.json ...             — auxiliar sin schema, o directorio vacío
       [ERR]  state/<file>.json                 — inválido o faltante cuando era requerido
       [WARN] ...                               — advertencia no bloqueante
       [FAIL] ...                               — error fatal (dependencia faltante, etc.)

Usage:
    python tools/python/state_validator.py --state state
    python tools/python/state_validator.py --state-dir state
    python tools/python/state_validator.py --state state --state-dir state  # warn + use state-dir
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from common import _TimedRun, SCHEMAS_DIR, emit_tool_summary  # noqa: F401

# ---------------------------------------------------------------------------
# Artefactos requeridos para que el pipeline determinista se considere "listo".
# Usado por --watch. El nombre coincide con state/<n>.json o un directorio.
# ---------------------------------------------------------------------------
_WATCH_REQUIRED_FILES: tuple[str, ...] = (
    "build-tool-contract.json",
    "archetype-profile.json",
    "generated-code-index.json",
    "stack-profile.json",
    "import-whitelist.json",
)
_WATCH_REQUIRED_DIRS: tuple[str, ...] = (
    "symbol-contracts",
)

# ---------------------------------------------------------------------------
# Schemas que NO se mapean a state/<name>.json sino que tienen lógica propia.
# ---------------------------------------------------------------------------
_SPECIAL_SCHEMAS: frozenset[str] = frozenset({
    "symbol-contract",  # → valida state/symbol-contracts/*.json
    "context-pack",     # → valida state/context-packs/*.json
    "semantic-index",   # → valida state/index/{classes,methods,imports,dependencies,annotations}.json
})

# ---------------------------------------------------------------------------
# state/_schemas/protocols/ contiene contratos de mensajes/JSON entre tools y
# agentes (gate-failure, cycle-summary, pipeline-run, llm-budget, artifact-map,
# context-pack-compact, patch-descriptor, telemetry). Esos schemas NO se
# corresponden con state/<name>.json runtime: sus instancias viven en
# state/_summaries/, state/context-packs-compact/, state/_patches/, o se emiten
# inline. Por eso protocols/ se excluye del walk top-level.
# ---------------------------------------------------------------------------
_EXCLUDED_SCHEMA_SUBDIRS: frozenset[str] = frozenset({
    "protocols",
})

# ---------------------------------------------------------------------------
# Estados runtime/opcionales: ausentes no es un error.
#
# Cada entrada mapea el nombre del schema (sin extensión) a la razón por la
# que su archivo de estado puede estar ausente legítimamente.
#
# Archivos NO listados aquí que tengan schema asociado son REQUERIDOS: el
# pipeline Python los escribe incondicionalmente y su ausencia es un [ERR].
# Actualmente eso corresponde a:
#   build-tool-contract  ← pom_parser.py              (Step  1)
#   archetype-profile    ← archetype_detector.py       (Step  2)
#   generated-code-index ← generated_code_scanner.py   (Step  3)
#   import-whitelist     ← classpath_resolver.py        (Step  4)
#   stack-profile        ← stack_profile_detector.py    (Step  5)
#   classification-index ← classification_analyzer.py   (Step 10)
# ---------------------------------------------------------------------------
_RUNTIME_OPTIONAL: dict[str, str] = {
    # ── Escritos por agentes LLM (fase posterior al pipeline Python) ──────────
    "compile-error-index":   "written by compile_error_parser when compilation fails",
    "coverage-summary":      "written by jacoco_parser after a JaCoCo run",
    "coverage-delta":        "written by jacoco_parser --mode delta (separate invocation)",
    "discovery-summary":     "written by LLM Discovery agent",
    "execution-state":       "written by LLM orchestrator",
    "failure-memory":        "written by LLM Repair agent across cycles",
    "generated-tests":       "written by LLM Generation agent",
    "mutation-intelligence": "written by LLM Mutation agent",
    # ── Escritos condicionalmente por el pipeline Python (flags opcionales) ───
    "coverage-targets":      "requires --jacoco-xml flag (Step 8)",
    "dependency-graph":      "requires pipeline Step 11; use --skip deps to omit",
    "fixture-catalog":       "requires pipeline Step 12; use --skip fixtures to omit",
    "batch-plan":            "requires pipeline Step 13; use --skip planning to omit",
    "incremental-map":       "requires --since flag (Step 14)",
}


# ---------------------------------------------------------------------------
# Helpers de validación de un único archivo
# ---------------------------------------------------------------------------

# Colector central de fallas de validación. Cada función llama a _record_failure
# en lugar de imprimir el [ERR] suelto, para que main() pueda (a) imprimir un
# RESUMEN al final —lo último en pantalla, sin que el detalle se pierda por scroll—
# y (b) persistirlo en <state>/_summaries/validation-errors.json.
_FAILURES: list[dict] = []


def _record_failure(file_label: str, schema_label: str, reason: str) -> None:
    """Registra UNA falla de validación e imprime su bloque [ERR] inmediato."""
    _FAILURES.append({"file": file_label, "schema": schema_label, "reason": reason})
    print(
        f"[ERR]  {file_label}\n"
        f"       schema: {schema_label}\n"
        f"       reason: {reason}",
        file=sys.stderr,
    )


def _format_validation_error(exc) -> str:
    """Mensaje conciso y legible para un jsonschema.ValidationError.

    El `.message` por defecto vuelca la instancia completa: para un array de miles
    de elementos da '[...]' is too long' — ilegible e inútil. Acá damos la RUTA
    JSON, la regla violada y el límite, con una muestra acotada de la instancia.
    """
    path = getattr(exc, "json_path", None) or "$"
    validator = getattr(exc, "validator", None)
    limit = getattr(exc, "validator_value", None)
    inst = getattr(exc, "instance", None)
    try:
        if validator in ("maxItems", "minItems") and isinstance(inst, list):
            sample = inst[:3]
            more = " …" if len(inst) > 3 else ""
            return (
                f"{path}: el array tiene {len(inst)} ítems y viola "
                f"{validator}={limit}. Muestra: {sample}{more}"
            )
        if validator in ("maxLength", "minLength") and isinstance(inst, str):
            return f"{path}: string de {len(inst)} caracteres, viola {validator}={limit}"
        if validator == "enum":
            return f"{path}: valor {inst!r} no está en el enum permitido {limit}"
        if validator in ("required", "additionalProperties", "type", "pattern"):
            return f"{path}: {exc.message}"
    except Exception:
        pass
    msg = exc.message
    if len(msg) > 300:
        msg = msg[:300] + "… (truncado)"
    return f"{path}: {msg}"


def _finalize_failures(state_dir: Path) -> None:
    """Imprime el resumen final de fallas y lo persiste a JSON (si hubo)."""
    if not _FAILURES:
        return
    print(
        f"\n================ VALIDATION FAILURES ({len(_FAILURES)}) ================",
        file=sys.stderr,
    )
    for f in _FAILURES:
        print(f"  - {f['file']}", file=sys.stderr)
        print(f"      schema: {f['schema']}", file=sys.stderr)
        print(f"      reason: {f['reason']}", file=sys.stderr)
    print(
        "=" * 57 + "\n"
        f"[FAIL] {len(_FAILURES)} archivo(s) no cumplen su schema (ver arriba).",
        file=sys.stderr,
    )
    try:
        out = state_dir / "_summaries" / "validation-errors.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schemaVersion": 1, "count": len(_FAILURES), "failures": _FAILURES}
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[INFO] detalle completo en {out}", file=sys.stderr)
    except OSError as exc:
        print(f"[WARN] no se pudo escribir validation-errors.json: {exc}", file=sys.stderr)


def _validate_file(
    target: Path,
    schema: dict,
    jsonschema,
) -> tuple[str, str | None]:
    """Valida `target` contra `schema`.

    Retorna ("OK", None) o ("ERR", mensaje).
    """
    try:
        with target.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        return "ERR", f"JSON inválido — {exc}"
    except OSError as exc:
        return "ERR", f"no se puede leer — {exc}"

    try:
        jsonschema.validate(data, schema)
        return "OK", None
    except jsonschema.ValidationError as exc:
        return "ERR", _format_validation_error(exc)
    except jsonschema.SchemaError as exc:
        return "ERR", f"schema error — {exc.message}"


# ---------------------------------------------------------------------------
# Validación estándar: un schema → state/<name>.json
# ---------------------------------------------------------------------------

def validate_standard_schemas(
    schemas_dir: Path,
    state_dir: Path,
    jsonschema,
) -> int:
    """Valida state/<name>.json para cada *.schema.json (excepto los especiales).

    Retorna 0 si todo OK, 1 si al menos un archivo falla.
    """
    rc = 0
    for schema_file in sorted(schemas_dir.glob("*.schema.json")):
        name = schema_file.stem.replace(".schema", "")
        if name in _SPECIAL_SCHEMAS:
            continue   # manejado por validate_symbol_contracts()

        target = state_dir / f"{name}.json"
        if not target.exists():
            if name in _RUNTIME_OPTIONAL:
                print(f"[SKIP] {name}.json — {_RUNTIME_OPTIONAL[name]}")
            else:
                print(
                    f"[ERR]  state/{name}.json — missing; must be produced by the Python pipeline",
                    file=sys.stderr,
                )
                rc = 1
            continue

        try:
            with schema_file.open("r", encoding="utf-8") as fh:
                schema = json.load(fh)
        except Exception as exc:
            print(
                f"[ERR]  cannot load schema {schema_file.name}: {exc}",
                file=sys.stderr,
            )
            rc = 1
            continue

        status, error = _validate_file(target, schema, jsonschema)
        if status == "OK":
            print(f"[OK]   state/{target.name}")
        else:
            _record_failure(
                f"state/{target.name}",
                f"state/_schemas/{schema_file.name}",
                error,
            )
            rc = 1

    return rc


# ---------------------------------------------------------------------------
# Validación especial: symbol-contract.schema.json → state/symbol-contracts/
# ---------------------------------------------------------------------------

def validate_symbol_contracts(
    schemas_dir: Path,
    state_dir: Path,
    jsonschema,
) -> int:
    """Valida cada state/symbol-contracts/<fqcn>.json contra symbol-contract.schema.json.

    - Si el directorio no existe o está vacío → [INFO], sin error.
    - Si existe algún contrato inválido → [ERR] con detalle, exit 1.
    - No toca state/symbol-contracts.json (manifest auxiliar, otro archivo).
    """
    schema_file = schemas_dir / "symbol-contract.schema.json"
    contracts_dir = state_dir / "symbol-contracts"

    if not schema_file.exists():
        print("[INFO] symbol-contract.schema.json not found; skipping contract validation")
        return 0

    if not contracts_dir.exists() or not contracts_dir.is_dir():
        print("[INFO] state/symbol-contracts/ directory not found; skipping contract validation")
        return 0

    contract_files = sorted(contracts_dir.glob("*.json"))
    if not contract_files:
        print("[INFO] state/symbol-contracts/ has no contract files yet")
        return 0

    try:
        with schema_file.open("r", encoding="utf-8") as fh:
            schema = json.load(fh)
    except Exception as exc:
        print(
            f"[ERR]  cannot load symbol-contract.schema.json: {exc}",
            file=sys.stderr,
        )
        return 1

    rc = 0
    ok_count = 0
    for cf in contract_files:
        status, error = _validate_file(cf, schema, jsonschema)
        if status == "OK":
            print(f"[OK]   state/symbol-contracts/{cf.name}")
            ok_count += 1
        else:
            _record_failure(
                f"state/symbol-contracts/{cf.name}",
                "state/_schemas/symbol-contract.schema.json",
                error,
            )
            rc = 1

    if ok_count > 0 and rc == 0:
        print(
            f"[OK]   state/symbol-contracts/ — {ok_count} contract(s) valid"
        )

    return rc


# ---------------------------------------------------------------------------
# Validación especial: context-pack.schema.json → state/context-packs/
# ---------------------------------------------------------------------------

def validate_context_packs(
    state_dir: Path,
    schemas_dir: Path,
    jsonschema,
) -> int:
    """Valida cada state/context-packs/<sut>.json contra context-pack.schema.json.

    - Si el directorio no existe o está vacío → [INFO], sin error (exit 0).
    - Si existe algún pack inválido → [ERR] con detalle, exit 1.
    - No lee state/context-pack.json; ese archivo singular no existe y no
      debe existir.
    """
    schema_file = schemas_dir / "context-pack.schema.json"
    context_packs_dir = state_dir / "context-packs"

    if not schema_file.exists():
        print("[INFO] context-pack.schema.json not found; skipping context-pack validation")
        return 0

    if not context_packs_dir.exists() or not context_packs_dir.is_dir():
        print("[INFO] state/context-packs/ has no context pack files yet")
        return 0

    pack_files = sorted(context_packs_dir.glob("*.json"))
    if not pack_files:
        print("[INFO] state/context-packs/ has no context pack files yet")
        return 0

    try:
        with schema_file.open("r", encoding="utf-8") as fh:
            schema = json.load(fh)
    except Exception as exc:
        print(
            f"[ERR]  cannot load context-pack.schema.json: {exc}",
            file=sys.stderr,
        )
        return 1

    rc = 0
    ok_count = 0
    for pf in pack_files:
        status, error = _validate_file(pf, schema, jsonschema)
        if status == "OK":
            print(f"[OK]   state/context-packs/{pf.name}")
            ok_count += 1
        else:
            _record_failure(
                f"state/context-packs/{pf.name}",
                "state/_schemas/context-pack.schema.json",
                error,
            )
            rc = 1

    if ok_count > 0 and rc == 0:
        print(f"[OK]   state/context-packs/ — {ok_count} context pack(s) valid")

    return rc


# ---------------------------------------------------------------------------
# Validación especial: semantic-index.schema.json → state/index/
# ---------------------------------------------------------------------------

# Mapping: filename in state/index/ → definition name inside semantic-index.schema.json
_SEMANTIC_INDEX_FILES: dict[str, str] = {
    "classes.json":      "classesFile",
    "methods.json":      "methodsFile",
    "imports.json":      "importsFile",
    "dependencies.json": "dependenciesFile",
    "annotations.json":  "annotationsFile",
}


def validate_semantic_index(
    schemas_dir: Path,
    state_dir: Path,
    jsonschema,
) -> int:
    """Valida state/index/{classes,methods,imports,dependencies,annotations}.json.

    Usa las definitions de semantic-index.schema.json porque ese schema no
    corresponde a un único state/<name>.json sino a un directorio con 5 archivos
    escritos por semantic_index_writer.py.  Los 5 archivos son requeridos.
    """
    schema_file = schemas_dir / "semantic-index.schema.json"
    index_dir = state_dir / "index"

    if not schema_file.exists():
        print("[INFO] semantic-index.schema.json not found; skipping index validation")
        return 0

    if not index_dir.exists() or not index_dir.is_dir():
        print(
            "[ERR]  state/index/ — missing; must be produced by semantic_index_writer.py",
            file=sys.stderr,
        )
        return 1

    try:
        with schema_file.open("r", encoding="utf-8") as fh:
            full_schema = json.load(fh)
    except Exception as exc:
        print(f"[ERR]  cannot load semantic-index.schema.json: {exc}", file=sys.stderr)
        return 1

    rc = 0
    ok_count = 0
    for filename, definition in _SEMANTIC_INDEX_FILES.items():
        target = index_dir / filename
        if not target.exists():
            print(
                f"[ERR]  state/index/{filename} — missing; must be produced by semantic_index_writer.py",
                file=sys.stderr,
            )
            rc = 1
            continue

        try:
            with target.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            print(f"[ERR]  state/index/{filename} — JSON inválido: {exc}", file=sys.stderr)
            rc = 1
            continue

        # Validate against the specific definition, resolving internal $refs.
        # RefResolver is deprecated in jsonschema ≥4.18 but still functional;
        # suppress the DeprecationWarning to keep output clean.
        sub_schema = {"$ref": f"#/definitions/{definition}"}
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*RefResolver.*")
                resolver = jsonschema.RefResolver.from_schema(full_schema)
                jsonschema.validate(data, sub_schema, resolver=resolver)
            print(f"[OK]   state/index/{filename}")
            ok_count += 1
        except jsonschema.ValidationError as exc:
            _record_failure(
                f"state/index/{filename}",
                f"state/_schemas/semantic-index.schema.json#/definitions/{definition}",
                _format_validation_error(exc),
            )
            rc = 1
        except jsonschema.SchemaError as exc:
            print(
                f"[ERR]  state/index/{filename} — schema error: {exc.message}",
                file=sys.stderr,
            )
            rc = 1

    if ok_count > 0 and rc == 0:
        print(f"[OK]   state/index/ — {ok_count} index file(s) valid")

    return rc


# ---------------------------------------------------------------------------
# Reporte de archivos auxiliares (sin schema)
# ---------------------------------------------------------------------------

def report_auxiliary_files(schemas_dir: Path, state_dir: Path) -> None:
    """Imprime [INFO] para state/*.json sin schema asociado.

    No emite error; solo informa que son estados auxiliares no validados.
    Ejemplos: symbol-contracts.json (manifest), module-progress.json, telemetry.json.
    """
    schema_stems = {
        sf.stem.replace(".schema", "")
        for sf in schemas_dir.glob("*.schema.json")
    }
    for jf in sorted(state_dir.glob("*.json")):
        stem = jf.stem
        if stem in schema_stems:
            # Ya validado (o en skip justificado) por validate_standard_schemas()
            continue
        print(
            f"[INFO] state/{jf.name} has no schema; treated as auxiliary state"
        )


# ---------------------------------------------------------------------------
# --watch mode
# ---------------------------------------------------------------------------

def _watch_required_present(state_dir: Path) -> tuple[bool, list[str]]:
    """Return (all_present, list_of_missing_descriptions)."""
    missing: list[str] = []
    for fname in _WATCH_REQUIRED_FILES:
        if not (state_dir / fname).exists():
            missing.append(fname)
    for dname in _WATCH_REQUIRED_DIRS:
        d = state_dir / dname
        if not d.exists() or not d.is_dir() or not any(d.glob("*.json")):
            missing.append(f"{dname}/*.json")
    return (not missing), missing


def _watch_validate(state_dir: Path, jsonschema) -> bool:
    """Run targeted validation over watched artefacts. True if all valid."""
    ok = True
    for fname in _WATCH_REQUIRED_FILES:
        target = state_dir / fname
        schema_name = fname.replace(".json", "")
        schema_file = SCHEMAS_DIR / f"{schema_name}.schema.json"
        if not schema_file.exists() or not target.exists():
            ok = False
            continue
        try:
            with schema_file.open("r", encoding="utf-8") as fh:
                schema = json.load(fh)
        except Exception:
            ok = False
            continue
        status, _ = _validate_file(target, schema, jsonschema)
        if status != "OK":
            ok = False
    # symbol-contracts directory
    contracts_dir = state_dir / "symbol-contracts"
    schema_file = SCHEMAS_DIR / "symbol-contract.schema.json"
    if schema_file.exists() and contracts_dir.exists():
        try:
            with schema_file.open("r", encoding="utf-8") as fh:
                schema = json.load(fh)
            for cf in contracts_dir.glob("*.json"):
                status, _ = _validate_file(cf, schema, jsonschema)
                if status != "OK":
                    ok = False
                    break
        except Exception:
            ok = False
    return ok


def run_watch(state_dir: Path, timeout_seconds: int) -> int:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        print(
            "[FAIL] jsonschema not installed — run: pip install jsonschema",
            file=sys.stderr,
        )
        return 3

    deadline = time.monotonic() + max(1, timeout_seconds)
    last_missing: list[str] = []
    while time.monotonic() < deadline:
        present, missing = _watch_required_present(state_dir)
        if present and _watch_validate(state_dir, jsonschema):
            print(
                f"[OK] watch satisfied — required artefacts present and valid in {state_dir}"
            )
            return 0
        if missing != last_missing:
            print(
                f"[WAIT] missing: {', '.join(missing) if missing else '(awaiting valid schema)'}"
            )
            last_missing = missing
        time.sleep(0.2)
    print(
        f"[FAIL] --watch timeout after {timeout_seconds}s; missing: "
        f"{', '.join(last_missing) if last_missing else '(validation never succeeded)'}",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Validate state/*.json against state/_schemas/*.schema.json.\n"
            "Validates state/symbol-contracts/*.json against symbol-contract.schema.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--state",
        default=None,
        help="Path to the state directory (e.g. state/)",
    )
    ap.add_argument(
        "--state-dir",
        dest="state_dir",
        default=None,
        help="Alias for --state; takes priority over --state when both are given",
    )
    ap.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Poll the state directory every 200 ms until the required pipeline "
            "artefacts exist and validate, or --timeout-seconds elapses."
        ),
    )
    ap.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        help="Maximum seconds to wait when --watch is set (default: 60).",
    )
    ap.add_argument(
        "--scope",
        choices=("all", "contracts", "index"),
        default="all",
        help=(
            "Restrict validation to a single artefact group (post-audit 2026-05-28):\n"
            "  all       — full validation pass (default; runs after step 15)\n"
            "  contracts — validate only state/symbol-contracts/ (run after step 6)\n"
            "  index     — validate only state/index/             (run after step 9)\n"
            "Scoped runs let the pipeline fail fast rather than burning 14 steps "
            "before catching a schema violation."
        ),
    )
    args = ap.parse_args()

    # ── Resolver directorio de estado ────────────────────────────────────────
    if args.state_dir and args.state:
        print(
            "[WARN] Both --state and --state-dir supplied; using --state-dir",
            file=sys.stderr,
        )
        raw_dir = args.state_dir
    elif args.state_dir:
        raw_dir = args.state_dir
    elif args.state:
        raw_dir = args.state
    else:
        ap.error("one of --state or --state-dir is required")
        return 2  # inalcanzable, pero calma a los type-checkers

    state_dir = Path(raw_dir).resolve()
    if not state_dir.exists():
        print(
            f"[FAIL] state directory not found: {state_dir}",
            file=sys.stderr,
        )
        return 2

    # ── --watch mode: corto-circuita la validación estándar ──────────────────
    if args.watch:
        return run_watch(state_dir, args.timeout_seconds)

    # ── Dependencia jsonschema ────────────────────────────────────────────────
    try:
        import jsonschema  # type: ignore
    except ImportError:
        print(
            "[FAIL] jsonschema not installed — run: pip install jsonschema",
            file=sys.stderr,
        )
        return 3

    rc = 0

    # Scoped runs: invoked from the middle of the pipeline (after step 6 or 9)
    # to validate one artefact group as soon as it is produced.
    if args.scope == "contracts":
        rc = validate_symbol_contracts(SCHEMAS_DIR, state_dir, jsonschema)
    elif args.scope == "index":
        rc = validate_semantic_index(SCHEMAS_DIR, state_dir, jsonschema)
    else:
        # Full validation (scope == "all"): runs as step 15.
        # ── 1. Validación estándar: schema → state/<name>.json ───────────────
        rc |= validate_standard_schemas(SCHEMAS_DIR, state_dir, jsonschema)

        # ── 2. Validación especial: symbol-contracts/ ────────────────────────
        if validate_symbol_contracts(SCHEMAS_DIR, state_dir, jsonschema) != 0:
            rc = 1

        # ── 3. Validación especial: context-packs/ ───────────────────────────
        if validate_context_packs(state_dir, SCHEMAS_DIR, jsonschema) != 0:
            rc = 1

        # ── 4. Validación especial: state/index/ (semantic-index) ────────────
        if validate_semantic_index(SCHEMAS_DIR, state_dir, jsonschema) != 0:
            rc = 1

        # ── 5. Archivos auxiliares sin schema ────────────────────────────────
        report_auxiliary_files(SCHEMAS_DIR, state_dir)

    # Resumen final de fallas (lo último en pantalla) + persistencia a JSON.
    # Aplica a TODOS los scopes, incluido el scoped 'contracts' que usa el pipeline.
    _finalize_failures(state_dir)

    return rc


if __name__ == "__main__":
    with _TimedRun("state_validator") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
