from __future__ import annotations

import json
import sys
from pathlib import Path

from common import atomic_write_text


def expected_agent_inputs(out_dir: Path) -> dict:
    """Return the architecture-reviewer handoff contract without invoking an LLM."""
    return {
        "schemaVersion": 1,
        "agent": "architecture-reviewer",
        "status": "READY",
        "inputs": {
            "sourceInventory": str(out_dir / "source-inventory.json"),
            "architectureMap": str(out_dir / "architecture-map.json"),
            "dependencyMap": str(out_dir / "dependency-map.json"),
            "findings": str(out_dir / "architecture-findings.json"),
        },
        "note": "Handoff contract only; no FastAPI, LangGraph, IDE, or LLM integration yet.",
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_agent_prompt() -> str:
    return (_repo_root() / "agents" / "architecture-reviewer" / "ARCHITECTURE_REVIEWER.md").read_text(
        encoding="utf-8"
    )


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def build_handoff_messages(out_dir: Path) -> list[dict]:
    payload = {
        "sourceInventory": _load_json(out_dir / "source-inventory.json"),
        "architectureMap": _load_json(out_dir / "architecture-map.json"),
        "dependencyMap": _load_json(out_dir / "dependency-map.json"),
        "findings": _load_json(out_dir / "architecture-findings.json"),
        "instruction": (
            "Interpretar la evidencia deterministica y producir recomendaciones "
            "arquitectonicas accionables. No pedir cambios al flujo de coverage."
        ),
    }
    return [
        {"role": "system", "content": _load_agent_prompt()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def run_llm_handoff(out_dir: Path) -> Path:
    """Run the architecture-reviewer through the existing LLM gateway.

    This is opt-in from the CLI and intentionally does not touch FastAPI or
    LangGraph. With the default IDE provider, llm_gateway writes the usual
    request/response handoff files under <out>/_llm and waits for the response.
    """
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from orchestrator import llm_gateway  # noqa: WPS433

    raw = llm_gateway.complete(
        build_handoff_messages(out_dir),
        role="architecture",
        state_dir=out_dir,
    )
    response_path = out_dir / "architecture-reviewer-response.md"
    atomic_write_text(response_path, raw)
    return response_path
