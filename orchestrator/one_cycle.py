"""one_cycle.py — el comando "un ciclo" que cycle_loop.py orquesta.

Este es exactamente el comando que `cycle_loop.py` esperaba y que en v1 no
existía (lo hacía un humano). cycle_loop sigue siendo el dueño del ciclo: tickea
el presupuesto, checkea el token-budget ANTES de dispatch, escribe los campos G8
y evalúa gate_g8. one_cycle hace el trabajo de UN ciclo y reescribe
coverage-delta.json para que cycle_loop mida progreso:

  fase 8  generación      → gateway + prompts → patch-descriptor (validado)
  fase 9  validación      → test_patch_applier (gates G1/G2/G5/G6/G7 + budget,
                            POR CONSTRUCCIÓN) → compilar + narrow test + jacoco
  fase 10a repair det.    → repair_dispatch (determinista)
  fase 10b repair-LLM     → gateway (solo para lo escalado)

Regla de oro: one_cycle NO adjudica gates. Delega en test_patch_applier.py, el
único escritor sancionado, que ya integra la suite de gates y el backstop de
presupuesto. one_cycle solo interpreta sus exit codes:
  0 → aplicado · 2 → presupuesto excedido · 3 → bloqueado por gate/perímetro

Uso (vía cycle_loop) — invocar como MÓDULO para que resuelvan los imports del
paquete orchestrator (desde la raíz del repo):
  python tools/python/cycle_loop.py --state <exec-state> --state-dir <state> \\
      -- python -m orchestrator.one_cycle --state-dir <state> --repo <java-repo>
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config, generation

# Exit codes de one_cycle (los lee cycle_loop).
RC_OK = 0
RC_BUDGET = 2
RC_NO_TARGETS = 7  # coincide con el --done-exit-code por defecto de cycle_loop

_PROCESSED = "_summaries/processed-targets.json"


# ── selección de target ───────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _processed_ids(state_dir: Path) -> set[str]:
    p = state_dir / _PROCESSED
    if not p.exists():
        return set()
    try:
        return set(_load_json(p).get("targetIds", []))
    except Exception:
        return set()


def mark_processed(state_dir: Path, target_id: str) -> None:
    p = state_dir / _PROCESSED
    p.parent.mkdir(parents=True, exist_ok=True)
    ids = _processed_ids(state_dir)
    ids.add(target_id)
    p.write_text(json.dumps({"targetIds": sorted(ids)}, ensure_ascii=False), encoding="utf-8")


def select_next_target(state_dir: Path) -> dict | None:
    """Primer item de batch-plan.json aún no procesado, o None si no quedan."""
    plan_path = state_dir / "batch-plan.json"
    if not plan_path.exists():
        return None
    done = _processed_ids(state_dir)
    for item in _load_json(plan_path).get("items", []):
        if item.get("targetId") not in done:
            return item
    return None


def is_generated_sut(state_dir: Path, sut: str) -> bool:
    """REGLA DURA: las clases autogeneradas NO se testean (DTOs OpenAPI/CXF,
    artefactos de annotation processors, etc.). Fuente: generated-code-index.json
    (`excludedFqcns` + `excludedPackages`), producido por generated_code_scanner.py.

    El planner ya las descarta del batch (coverage_planner._build_excluded_set), pero
    esto es la garantía POR CONSTRUCCIÓN a nivel de ciclo: aunque un target generado
    llegue por un batch-plan editado a mano o una regresión del planner, NUNCA se
    invoca al modelo para una clase generada. Ver docs/canonical-prohibitions.md #13."""
    idx = state_dir / "generated-code-index.json"
    if not idx.exists():
        return False
    try:
        d = _load_json(idx)
    except Exception:
        return False
    if sut in set(d.get("excludedFqcns") or []):
        return True
    for pkg in (d.get("excludedPackages") or []):
        # Solo entradas que son NOMBRES DE PAQUETE (no globs de path como build/generated/**).
        if pkg and "/" not in pkg and "*" not in pkg and sut.startswith(pkg + "."):
            return True
    return False


def load_context_pack_compact(state_dir: Path, sut: str) -> dict | None:
    """Pack COMPACTO (context-packs-compact/<sut>.json) — proyección minificada
    que se manda al modelo (minimización de tokens).

    F4: si NO existe, devuelve None en vez de degradar al pack completo. Mandar el
    pack completo (cientos de KB) al modelo sería un blowup de tokens silencioso;
    es preferible BLOQUEAR el target y forzar re-correr la fase 0 con `--compact`
    (run_pipeline.py). El llamador trata el None como BLOCKED."""
    compact = state_dir / "context-packs-compact" / f"{sut}.json"
    if compact.exists():
        return _load_json(compact)
    return None


def testcase_from_target(item: dict) -> dict:
    """Sintetiza el testCase mínimo que test-body-agent espera, a partir del
    item del plan. El escenario guía el nombre del método (skill 11-quality/03)."""
    method = item.get("method", "")
    return {
        "targetId": item.get("targetId"),
        "sut": item.get("sut"),
        "method": method,
        "scenario": f"cubrir el comportamiento de {method}",
        "fixtureIds": item.get("fixtureIds", []),
    }


# ── aplicación del patch (escritor sancionado) ────────────────────────────────

def apply_patch(patch: dict, *, state_dir: Path, repo: Path, context_pack_path: Path,
                repair_attempts: list[dict] | None = None) -> int:
    """Materializa el patch vía test_patch_applier.py (gates + budget por
    construcción). Devuelve su exit code (0 ok · 2 budget · 3 gate/perímetro).

    ``repair_attempts`` son los triplets anti-loop de G7 (errorCode, symbolFQN,
    fixId) que el orquestador deriva para una reparación; se pasan como
    ``--repair-attempt`` para que un repair patch no quede bloqueado con
    G7_REPAIR_WITHOUT_TRIPLET. None/[] en generación normal."""
    with tempfile.NamedTemporaryFile("w", suffix=".patch.json", delete=False, encoding="utf-8") as fh:
        json.dump(patch, fh, ensure_ascii=False)
        patch_path = Path(fh.name)
    try:
        cmd = [
            sys.executable,
            str(config.TOOLS_PYTHON / "test_patch_applier.py"),
            "--patch", str(patch_path),
            "--repo", str(repo),
            "--state", str(state_dir),
            "--context-pack", str(context_pack_path),
        ]
        for tri in (repair_attempts or []):
            cmd += ["--repair-attempt",
                    f"{tri['errorCode']}|{tri['symbolFQN']}|{tri['fixId']}"]
        # Capture the patcher's output so a gate/perimeter rejection (rc=3) keeps
        # its reason instead of vanishing — the repair payload surfaces it so the
        # model is not blind on a non-compiler rejection (the [BLOCKED] gate line
        # + [BLOCKED-DETAIL] JSON). stdout/stderr are still echoed through.
        proc = subprocess.run(cmd, check=False, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            _record_patcher_rejection(state_dir, patch, proc.returncode,
                                      (proc.stdout or "") + (proc.stderr or ""))
        return proc.returncode
    finally:
        patch_path.unlink(missing_ok=True)


def _patch_sut_fqcn(patch: dict) -> str:
    sut = patch.get("sut", "")
    return sut.get("fqcn", "") if isinstance(sut, dict) else str(sut)


def _record_patcher_rejection(state_dir: Path, patch: dict, rc: int, output: str) -> None:
    """Persist the patcher's rejection (keyed by canonical testClass) so the
    batch repair payload can carry WHY the patch was rejected — e.g. the gate
    code and the orphan evidenceId — rather than a bare ``patcher rc=3``."""
    test_class = str(patch.get("testClass") or _patch_sut_fqcn(patch) or "unknown")
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", test_class)
    out_dir = state_dir / "_summaries" / "patcher-decisions"
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schemaVersion": 1,
        "rc": rc,
        "testClass": test_class,
        "sut": _patch_sut_fqcn(patch),
        "output": output.strip()[-8000:],
    }
    (out_dir / f"{safe}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


# ── validación (fase 9) — requiere Maven; best-effort ─────────────────────────

def _run_tool(script: str, args: list[str]) -> int:
    cmd = [sys.executable, str(config.TOOLS_PYTHON / script), *args]
    return subprocess.run(cmd, check=False).returncode


def validate_and_score(test_class: str, *, state_dir: Path, repo: Path, cycle: int) -> None:
    """Corre el test recién aplicado y recalcula coverage-delta.json.

    Best-effort: si Maven/JaCoCo no están disponibles no aborta el ciclo (el
    delta simplemente queda en cero y cycle_loop lo cuenta como sin-progreso).
    """
    _run_tool("narrow_test_runner.py", [
        "--repo", str(repo), "--state", str(state_dir), "--test-class", test_class,
    ])
    before = repo / "target" / "jacoco-baseline.xml"
    after = repo / "target" / "site" / "jacoco" / "jacoco.xml"
    if before.exists() and after.exists():
        _run_tool("jacoco_parser.py", [
            "--mode", "delta", "--before", str(before), "--after", str(after),
            "--cycle", str(cycle), "--out", str(state_dir / "coverage-delta.json"),
        ])


# ── un ciclo ──────────────────────────────────────────────────────────────────

def run_one_cycle(state_dir: Path, repo: Path) -> int:
    state_dir = state_dir.resolve()
    repo = repo.resolve()

    target = select_next_target(state_dir)
    if target is None:
        print("[one_cycle] no quedan targets sin procesar.")
        return RC_NO_TARGETS

    sut = target["sut"]
    target_id = target.get("targetId", sut)

    # REGLA DURA (canonical-prohibitions #13): las clases autogeneradas NO se testean.
    # Garantía por construcción a nivel de ciclo — nunca se invoca al modelo para una
    # clase generada, aun si el planner la dejara pasar.
    if is_generated_sut(state_dir, sut):
        print(f"[one_cycle] target {target_id}: BLOCKED — {sut} es código autogenerado "
              "(generated-code-index); las clases generadas NO se testean.")
        mark_processed(state_dir, target_id)
        return RC_OK

    pack_path = state_dir / "context-packs" / f"{sut}.json"
    pack_compact = load_context_pack_compact(state_dir, sut)  # → al modelo (pocos tokens)

    # F4: sin compact-pack NO se degrada al pack completo (evita blowup de tokens).
    # Se BLOQUEA el target; re-correr la fase 0 con --compact regenera el pack.
    if pack_compact is None:
        print(f"[one_cycle] target {target_id}: BLOCKED — sin compact-pack para {sut}. "
              "Re-corré la fase 0 (run_pipeline.py --compact) para regenerarlo; "
              "NO se degrada al pack completo.")
        mark_processed(state_dir, target_id)
        return RC_OK

    # Fase 8 — generación (el gateway aplica el token-budget antes de llamar).
    # Al modelo va el pack COMPACTO; el COMPLETO queda para el perímetro del patcher.
    patch = generation.generate_patch(
        state_dir=state_dir, context_pack=pack_compact, test_case=testcase_from_target(target),
    )

    if str(patch.get("status", "")).upper() == "BLOCKED":
        print(f"[one_cycle] target {target_id}: BLOCKED — {patch.get('blockReason')}")
        mark_processed(state_dir, target_id)
        return RC_OK

    # Fase 9 — aplicación: gates + budget POR CONSTRUCCIÓN dentro del patcher.
    rc = apply_patch(patch, state_dir=state_dir, repo=repo, context_pack_path=pack_path)
    if rc == RC_BUDGET:
        print("[one_cycle] patcher: presupuesto excedido (backstop).")
        return RC_BUDGET
    if rc == 3:
        print(f"[one_cycle] target {target_id}: patch bloqueado por gate/perímetro.")
        mark_processed(state_dir, target_id)
        return RC_OK
    if rc != 0:
        print(f"[one_cycle] patcher devolvió rc={rc}.")
        mark_processed(state_dir, target_id)
        return RC_OK

    # Fase 9 (cont.) — compilar/correr/medir. Reescribe coverage-delta.json.
    exec_state = _load_json(state_dir / "execution-state.json") if (state_dir / "execution-state.json").exists() else {}
    validate_and_score(
        patch.get("testClass", ""), state_dir=state_dir, repo=repo,
        cycle=int(exec_state.get("cycle", 1)),
    )

    mark_processed(state_dir, target_id)
    return RC_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Un ciclo de generación+patch+validación (driver LLM v2).")
    ap.add_argument("--state-dir", required=True, type=Path, help="Directorio de estado (.agent-state).")
    ap.add_argument("--repo", required=True, type=Path, help="Raíz del proyecto Java bajo prueba.")
    args = ap.parse_args(argv)
    return run_one_cycle(args.state_dir, args.repo)


if __name__ == "__main__":
    sys.exit(main())
