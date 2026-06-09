"""test_generation_mode.py — generation-mode + batch knobs config resolution.

Locks the config helpers that route the post-pre-stage loop:
  * generation_mode() defaults to handoff-single (compat) and honours the env var,
    falling back to the default on an unknown value (the CLI validates choices).
  * batch_size() defaults to 10 and is clamped to a sane range.
  * max_repair_rounds() defaults to 2 and is clamped.

The interactive-vs-automatic behaviour itself is covered where it lives:
  * handoff-batch waits but pauses the budget   → test_batch_runner.py
  * the budget freeze during a manual wait       → test_budget_pause.py
  * auto refuses the manual 'ide' provider       → run_all_deterministic CLI guard

Legacy-suite convention: expose ``main() -> int`` (0 = ok). Auto-discovered by
test_aa_suite_runner.py. Run standalone:
    python tools/python/tests/test_generation_mode.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # repo root → orchestrator.*

from orchestrator import config  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(label + (f" — {detail}" if detail else ""))


def _with_env(**env):
    """Set env vars, returning a restore callable."""
    saved = {k: os.environ.get(k) for k in env}

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return restore


def case_mode_default_is_handoff_single() -> None:
    restore = _with_env(COVAGENT_GENERATION_MODE=None)
    try:
        _assert("default mode handoff-single", config.generation_mode() == "handoff-single")
    finally:
        restore()


def case_mode_env_override() -> None:
    for val in ("handoff-batch", "auto", "handoff-single"):
        restore = _with_env(COVAGENT_GENERATION_MODE=val)
        try:
            _assert(f"mode env {val}", config.generation_mode() == val)
        finally:
            restore()


def case_mode_unknown_falls_back() -> None:
    restore = _with_env(COVAGENT_GENERATION_MODE="nonsense")
    try:
        _assert("unknown mode → default", config.generation_mode() == "handoff-single")
    finally:
        restore()


def case_batch_size_default_and_override() -> None:
    restore = _with_env(COVAGENT_BATCH_SIZE=None)
    try:
        _assert("batch_size default 10", config.batch_size() == 10)
    finally:
        restore()
    for given, expect in (("3", 3), ("5", 5), ("10", 10), ("0", 1), ("999", 50), ("x", 10)):
        restore = _with_env(COVAGENT_BATCH_SIZE=given)
        try:
            _assert(f"batch_size {given}→{expect}", config.batch_size() == expect,
                    str(config.batch_size()))
        finally:
            restore()


def case_max_repair_rounds_default_and_clamp() -> None:
    restore = _with_env(COVAGENT_MAX_REPAIR_ROUNDS=None)
    try:
        _assert("max_repair_rounds default 2", config.max_repair_rounds() == 2)
    finally:
        restore()
    for given, expect in (("0", 0), ("1", 1), ("99", 10), ("x", 2)):
        restore = _with_env(COVAGENT_MAX_REPAIR_ROUNDS=given)
        try:
            _assert(f"max_repair_rounds {given}→{expect}", config.max_repair_rounds() == expect)
        finally:
            restore()


def main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("case_")]
    for c in cases:
        try:
            c()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{c.__name__} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print("FAIL test_generation_mode:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK   test_generation_mode: {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
