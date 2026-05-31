"""test_aa_suite_runner.py — pytest entry point for the legacy suites.

The other ``test_*.py`` files in this directory predate pytest: they expose
``case_*()`` helpers that record into a module-level ``FAILURES`` list and a
``main() -> int`` that returns non-zero on failure. pytest's default discovery
collects ``test_*`` *functions*, so it skipped every ``case_*`` and historically
collected **zero** tests — a false green (`pytest -q` printed "no tests ran"
while the suites were never asserted).

This wrapper turns each legacy suite into a real pytest test by importing it and
asserting its ``main()`` exits cleanly. It does **not** modify the suites, which
keep working standalone (`python tools/python/tests/test_<name>.py`). Naming the
file ``test_aa_*`` makes it sort first, but order does not matter — each suite is
loaded under a unique module name to avoid clobbering pytest's own import of it.

Run: `python -m pytest tools/python/tests/ -q`
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # so suites can `import common`, `budget_enforcer`, ...

_SELF = Path(__file__).name
_SUITES = sorted(p for p in HERE.glob("test_*.py") if p.name != _SELF)


@pytest.mark.parametrize("suite", _SUITES, ids=[p.stem for p in _SUITES])
def test_legacy_suite(suite: Path) -> None:
    """Invoke a legacy suite's main() and fail if it reports any case failure."""
    spec = importlib.util.spec_from_file_location(f"_legacysuite_{suite.stem}", suite)
    assert spec is not None and spec.loader is not None, f"cannot load {suite}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main()
    assert rc == 0, f"{suite.name} reported case failures (main() returned {rc})"
