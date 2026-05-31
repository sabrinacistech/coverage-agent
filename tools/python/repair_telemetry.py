"""repair_telemetry.py — atomically update state/telemetry.json.

Counters increment by one event per invocation:

    --event rules-hit         → repairsByRule         += 1
    --event llm-hit           → repairsByLLM          += 1
    --event template-avoided  → llmCallsAvoidedByTemplate += 1

Token deltas are additive and independent of --event:

    --add-tokens-in  N        → tokensIn  += N
    --add-tokens-out N        → tokensOut += N

rulesHitRate is recomputed every write:

    rulesHitRate = repairsByRule / max(1, repairsByRule + repairsByLLM)

The file is written atomically (tmp + rename) so concurrent readers never
observe a torn payload.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, emit_tool_summary  # noqa: E402

_DEFAULT_PATH = Path("state") / "telemetry.json"

_EVENTS: dict[str, str] = {
    "rules-hit":        "repairsByRule",
    "llm-hit":          "repairsByLLM",
    "template-avoided": "llmCallsAvoidedByTemplate",
}

_BASE: dict = {
    "schemaVersion":             1,
    "repairsByRule":             0,
    "repairsByLLM":              0,
    "llmCallsAvoidedByTemplate": 0,
    "tokensIn":                  0,
    "tokensOut":                 0,
    "rulesHitRate":              0.0,
}


def _load(path: Path) -> dict:
    if not path.exists():
        return dict(_BASE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_BASE)
    merged = dict(_BASE)
    for k in _BASE:
        v = data.get(k, _BASE[k])
        if isinstance(_BASE[k], int) and not isinstance(v, int):
            v = _BASE[k]
        merged[k] = v
    return merged


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _recompute_hit_rate(state: dict) -> None:
    denom = state["repairsByRule"] + state["repairsByLLM"]
    if denom <= 0:
        state["rulesHitRate"] = 0.0
    else:
        state["rulesHitRate"] = round(state["repairsByRule"] / denom, 6)


def update(
    path: Path,
    event: str | None,
    add_in: int,
    add_out: int,
) -> dict:
    state = _load(path)
    if event:
        state[_EVENTS[event]] += 1
    if add_in:
        state["tokensIn"] += add_in
    if add_out:
        state["tokensOut"] += add_out
    _recompute_hit_rate(state)
    state["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_write(path, state)
    return state


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Atomically update state/telemetry.json.",
    )
    ap.add_argument(
        "--event",
        choices=sorted(_EVENTS.keys()),
        default=None,
        help="Increment a single counter (optional).",
    )
    ap.add_argument(
        "--add-tokens-in",
        type=int,
        default=0,
        help="Add N to tokensIn.",
    )
    ap.add_argument(
        "--add-tokens-out",
        type=int,
        default=0,
        help="Add N to tokensOut.",
    )
    ap.add_argument(
        "--path",
        default=str(_DEFAULT_PATH),
        help="Telemetry file path (default: state/telemetry.json).",
    )
    args = ap.parse_args()

    if args.add_tokens_in < 0 or args.add_tokens_out < 0:
        print("[FAIL] token deltas must be >= 0", file=sys.stderr)
        return 2

    if not args.event and not args.add_tokens_in and not args.add_tokens_out:
        print(
            "[FAIL] nothing to do: pass --event and/or --add-tokens-in/--add-tokens-out",
            file=sys.stderr,
        )
        return 2

    target = Path(args.path).resolve()
    state = update(target, args.event, args.add_tokens_in, args.add_tokens_out)
    print(
        f"[OK] telemetry updated: rule={state['repairsByRule']} "
        f"llm={state['repairsByLLM']} "
        f"avoided={state['llmCallsAvoidedByTemplate']} "
        f"tokIn={state['tokensIn']} tokOut={state['tokensOut']} "
        f"hitRate={state['rulesHitRate']}"
    )
    return 0


if __name__ == "__main__":
    with _TimedRun("repair_telemetry") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
