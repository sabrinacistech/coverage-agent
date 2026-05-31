"""Trabajo de ciclo determinista para el test de paridad (NO es un test).

Misma lógica usada por los DOS drivers comparados:
  - cycle_loop (vía subprocess: `python _fakework.py --state-dir X`)
  - el grafo (vía monkeypatch de nodes.run_cycle_work -> _fakework.run)

Lee un plan `_fakeplan.json` en el state-dir: una lista de pasos
[[rc, linesDelta, branchesDelta], ...]. Cada invocación consume un paso, escribe
coverage-delta.json con esos deltas y devuelve rc. Así ambos drivers evolucionan
el estado idénticamente y deben parar en el mismo lugar.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def run(state_dir) -> int:
    sd = Path(state_dir)
    plan_p = sd / "_fakeplan.json"
    plan = json.loads(plan_p.read_text(encoding="utf-8"))
    i = int(plan.get("i", 0))
    steps = plan["steps"]
    rc, lines, branches = steps[i] if i < len(steps) else [0, 0, 0]
    (sd / "coverage-delta.json").write_text(
        json.dumps({"totals": {"lines": {"delta": lines}, "branches": {"delta": branches}}}),
        encoding="utf-8")
    plan["i"] = i + 1
    plan_p.write_text(json.dumps(plan), encoding="utf-8")
    return int(rc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", required=True)
    return run(ap.parse_args().state_dir)


if __name__ == "__main__":
    sys.exit(main())
