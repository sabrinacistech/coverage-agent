"""test_find_pom_modules_consolidation.py — discovery de módulos hecho UNA vez.

find_pom_modules(repo, contract=...) reutiliza la lista de módulos ya descubierta
por pom_parser (build-tool-contract.json) en vez de re-caminar el árbol con rglob
en cada paso de discovery. Si el contrato falta o está vacío, cae al rglob previo.

Run: `python tools/python/tests/test_find_pom_modules_consolidation.py` (también pytest)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from common import find_pom_modules  # noqa: E402


def _make_repo(root: Path) -> None:
    (root / "pom.xml").write_text("<project/>", encoding="utf-8")
    (root / "modA").mkdir(parents=True, exist_ok=True)
    (root / "modA" / "pom.xml").write_text("<project/>", encoding="utf-8")


def case_uses_contract_when_present(tmp: Path) -> None:
    _make_repo(tmp)
    # El contrato lista SOLO modA. Si find_pom_modules usara rglob incluiría también
    # la raíz → que devuelva solo modA prueba que reusó el contrato.
    contract = tmp / "build-tool-contract.json"
    contract.write_text(json.dumps({"modules": [{"path": str(tmp / "modA")}]}), encoding="utf-8")
    mods = find_pom_modules(tmp, contract=contract)
    assert [str(m) for m in mods] == [str(tmp / "modA")], mods


def case_falls_back_to_rglob_when_contract_missing(tmp: Path) -> None:
    _make_repo(tmp)
    mods = find_pom_modules(tmp, contract=tmp / "does-not-exist.json")
    assert {str(m) for m in mods} == {str(tmp), str(tmp / "modA")}, mods


def case_falls_back_when_contract_has_no_modules(tmp: Path) -> None:
    _make_repo(tmp)
    contract = tmp / "build-tool-contract.json"
    contract.write_text(json.dumps({"modules": []}), encoding="utf-8")
    mods = find_pom_modules(tmp, contract=contract)
    assert str(tmp) in {str(m) for m in mods}, mods  # vacío → rglob


def case_no_contract_arg_is_unchanged(tmp: Path) -> None:
    _make_repo(tmp)
    mods = find_pom_modules(tmp)  # comportamiento previo
    assert {str(m) for m in mods} == {str(tmp), str(tmp / "modA")}, mods


# ── pytest ──────────────────────────────────────────────────────────────────

def test_uses_contract_when_present(tmp_path):
    case_uses_contract_when_present(tmp_path)


def test_falls_back_to_rglob_when_contract_missing(tmp_path):
    case_falls_back_to_rglob_when_contract_missing(tmp_path)


def test_falls_back_when_contract_has_no_modules(tmp_path):
    case_falls_back_when_contract_has_no_modules(tmp_path)


def test_no_contract_arg_is_unchanged(tmp_path):
    case_no_contract_arg_is_unchanged(tmp_path)


def main() -> int:
    import tempfile
    cases = [
        ("uses-contract-when-present", case_uses_contract_when_present),
        ("falls-back-when-missing", case_falls_back_to_rglob_when_contract_missing),
        ("falls-back-when-empty", case_falls_back_when_contract_has_no_modules),
        ("no-contract-unchanged", case_no_contract_arg_is_unchanged),
    ]
    failed = 0
    for name, fn in cases:
        try:
            with tempfile.TemporaryDirectory() as td:
                fn(Path(td))
            print(f"OK   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    if failed:
        print(f"\n{failed} test(s) failed")
        return 1
    print("\nAll find_pom_modules consolidation cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
