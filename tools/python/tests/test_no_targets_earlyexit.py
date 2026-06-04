"""test_no_targets_earlyexit.py — Opción A: early-exit cuando JaCoCo no halla targets.

Cuando `coverage-targets.json` tiene 0 targets (proyecto ya 100% cubierto),
run_pipeline saltea los pasos caros (classpath/index/clasificación/planner/context)
y escribe un batch-plan vacío; validate_handoff lo reporta como NO_TARGETS (exit 0)
en vez de BLOCKED_PRE_STAGE_MISSING.

Run: `python tools/python/tests/test_no_targets_earlyexit.py`  (también lo colecta pytest)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import common  # noqa: E402
import run_pipeline  # noqa: E402
import validate_handoff  # noqa: E402


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# ── _write_empty_batch_plan: válido contra el schema ───────────────────────────

def case_empty_batch_plan_is_schema_valid(tmp: Path) -> None:
    run_pipeline._write_empty_batch_plan(tmp, "coverage")
    bp = json.loads((tmp / "batch-plan.json").read_text(encoding="utf-8"))
    common.validate("batch-plan", bp)  # lanza si es inválido
    assert bp["items"] == [] and bp["sizeChosen"] == 0, bp


# ── validate_handoff: NO_TARGETS cuando coverage-targets está vacío ────────────

def case_validate_handoff_no_targets(tmp: Path, monkeypatch) -> None:
    _write(tmp / "coverage-targets.json", {"schemaVersion": 1, "mode": "coverage", "targets": []})
    monkeypatch.setattr(sys, "argv", ["validate_handoff.py", "--state", str(tmp)])
    rc = validate_handoff.main()
    assert rc == 0, rc
    summ = json.loads((tmp / "_summaries" / "handoff-summary.json").read_text(encoding="utf-8"))
    assert summ["status"] == "NO_TARGETS", summ
    # NO_TARGETS no exige whitelist/packs/contracts (no estaban y aun así exit 0).
    assert not (tmp / "import-whitelist.json").exists()


def case_validate_handoff_no_targets_when_batch_empty(tmp: Path, monkeypatch) -> None:
    # Había targets crudos (generados) pero el batch quedó vacío tras la exclusión
    # → NO_TARGETS limpio, no BLOCKED.
    _write(tmp / "coverage-targets.json", {
        "schemaVersion": 1, "mode": "coverage",
        "targets": [{"id": "t1", "sut": "com.acme.gen.FooDTO", "method": "equals"}],
    })
    _write(tmp / "batch-plan.json", {
        "schemaVersion": 1, "cycle": 1, "mode": "coverage", "sizeChosen": 0, "items": [],
    })
    monkeypatch.setattr(sys, "argv", ["validate_handoff.py", "--state", str(tmp)])
    rc = validate_handoff.main()
    assert rc == 0, rc
    summ = json.loads((tmp / "_summaries" / "handoff-summary.json").read_text(encoding="utf-8"))
    assert summ["status"] == "NO_TARGETS", summ


def case_validate_handoff_blocks_when_targets_present_but_artifacts_missing(tmp: Path, monkeypatch) -> None:
    # Hay targets reales pero faltan los artefactos → sigue bloqueando (la rama
    # NO_TARGETS NO debe tragarse un handoff genuinamente incompleto).
    _write(tmp / "coverage-targets.json", {
        "schemaVersion": 1, "mode": "coverage",
        "targets": [{"id": "t1", "sut": "com.acme.Foo", "method": "bar"}],
    })
    monkeypatch.setattr(sys, "argv", ["validate_handoff.py", "--state", str(tmp)])
    rc = validate_handoff.main()
    assert rc == 2, rc
    summ = json.loads((tmp / "_summaries" / "handoff-summary.json").read_text(encoding="utf-8"))
    assert summ["status"] == "BLOCKED_PRE_STAGE_MISSING", summ


# ── pytest entry points ─────────────────────────────────────────────────────────

def test_empty_batch_plan_is_schema_valid(tmp_path):
    case_empty_batch_plan_is_schema_valid(tmp_path)


def test_validate_handoff_no_targets(tmp_path, monkeypatch):
    case_validate_handoff_no_targets(tmp_path, monkeypatch)


def test_validate_handoff_no_targets_when_batch_empty(tmp_path, monkeypatch):
    case_validate_handoff_no_targets_when_batch_empty(tmp_path, monkeypatch)


def test_validate_handoff_blocks_when_targets_present_but_artifacts_missing(tmp_path, monkeypatch):
    case_validate_handoff_blocks_when_targets_present_but_artifacts_missing(tmp_path, monkeypatch)


# ── standalone runner ─────────────────────────────────────────────────────────

def main() -> int:
    import tempfile
    from unittest import mock

    class _MP:
        """monkeypatch mínimo para el runner standalone."""
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            old = getattr(obj, name); self._undo.append((obj, name, old)); setattr(obj, name, val)
        def undo(self):
            for obj, name, old in reversed(self._undo): setattr(obj, name, old)

    cases = [
        ("empty-batch-plan-schema-valid", lambda td: case_empty_batch_plan_is_schema_valid(Path(td))),
    ]
    mp_cases = [
        ("validate-handoff-no-targets", case_validate_handoff_no_targets),
        ("validate-handoff-blocks-when-incomplete", case_validate_handoff_blocks_when_targets_present_but_artifacts_missing),
    ]
    failed = 0
    for name, fn in cases:
        try:
            with tempfile.TemporaryDirectory() as td:
                fn(td)
            print(f"OK   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1; print(f"FAIL {name}: {exc}")
    for name, fn in mp_cases:
        try:
            with tempfile.TemporaryDirectory() as td:
                mp = _MP()
                try:
                    fn(Path(td), mp)
                finally:
                    mp.undo()
            print(f"OK   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1; print(f"FAIL {name}: {exc}")
    if failed:
        print(f"\n{failed} test(s) failed"); return 1
    print("\nAll no-targets early-exit cases passed"); return 0


if __name__ == "__main__":
    sys.exit(main())
