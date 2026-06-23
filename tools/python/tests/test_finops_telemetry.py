"""test_finops_telemetry.py — volumetría de workspace + telemetría FinOps.

Cubre orchestrator/workspace_volumetry.py y orchestrator/cost_telemetry.py:
  * tamaño de directorio recursivo, excluyendo .git/target y tolerante a fallos
  * tabla de eficiencia (formato + factor de reducción)
  * pricing por modelo (substring + override por entorno + fallback conservador)
  * cálculo de costo USD y estimación de tokens por tamaño
  * extracción de `usage` en ambos vocabularios (Anthropic/OpenAI)
  * persistencia atómica + acumulación de costs-telemetry.json
  * atribución por target del handoff (medido vs estimado, suma exacta)

Convención legacy: expone ``main() -> int`` (0 = ok). Auto-descubierto por
test_aa_suite_runner.py. Standalone:
    python tools/python/tests/test_finops_telemetry.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # repo root → import orchestrator.*

from orchestrator import cost_telemetry as ct  # noqa: E402
from orchestrator import workspace_volumetry as vol  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(label + (f" — {detail}" if detail else ""))


# ── volumetría ────────────────────────────────────────────────────────────────

def case_directory_size_excludes_and_sums() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src").mkdir()
        (root / "src" / "A.java").write_text("x" * 100, encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "objects").write_text("y" * 1000, encoding="utf-8")
        (root / "target").mkdir()
        (root / "target" / "A.class").write_text("z" * 5000, encoding="utf-8")
        size = vol.directory_size_bytes(root)
        _assert("size counts only non-excluded files", size == 100, f"size={size}")


def case_directory_size_tolerant_to_missing() -> None:
    size = vol.directory_size_bytes(Path(tempfile.gettempdir()) / "no-existe-finops-xyz")
    _assert("missing path → 0", size == 0, f"size={size}")


def case_sum_file_sizes_tolerant() -> None:
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "r.json"
        f.write_text("a" * 40, encoding="utf-8")
        total = vol.sum_file_sizes([f, Path(d) / "ausente.json"])
        _assert("sum_file_sizes ignores missing", total == 40, f"total={total}")


def case_reduction_factor_and_table() -> None:
    _assert("factor zero-safe", vol.reduction_factor(1000, 0) == 0.0)
    _assert("factor math", abs(vol.reduction_factor(1_048_576, 1024) - 1024.0) < 1e-6)
    table = vol.format_efficiency_table(repo_bytes=125 * 1_048_576, context_bytes=10 * 1024)
    _assert("table has header", "METRICA DE EFICIENCIA DE CONTEXTO" in table, table)
    _assert("table has repo MB", "125.00 MB" in table, table)
    _assert("table has context KB", "10.00 KB" in table, table)
    _assert("table has factor x", "x" in table.splitlines()[-2], table)
    # caja bien formada: todas las líneas de igual ancho.
    lines = table.splitlines()
    _assert("table box aligned", len({len(l) for l in lines}) == 1, repr([len(l) for l in lines]))


# ── pricing ─────────────────────────────────────────────────────────────────────

def case_pricing_by_substring() -> None:
    _assert("opus price", ct.price_for_model("anthropic/claude-opus-4-8") == (15.0, 75.0))
    _assert("sonnet price", ct.price_for_model("anthropic/claude-sonnet-4-6") == (3.0, 15.0))
    _assert("haiku price", ct.price_for_model("claude-haiku-4-5") == (0.8, 4.0))


def case_pricing_specificity_mini_vs_4o() -> None:
    _assert("gpt-4o-mini wins over gpt-4o",
            ct.price_for_model("openai/gpt-4o-mini") == (0.15, 0.60),
            str(ct.price_for_model("openai/gpt-4o-mini")))
    _assert("gpt-4o", ct.price_for_model("openai/gpt-4o") == (2.50, 10.00))


def case_pricing_fallback_unknown_is_conservative() -> None:
    _assert("unknown → opus fallback", ct.price_for_model("some/unknown-model") == (15.0, 75.0))
    _assert("none → fallback", ct.price_for_model(None) == (15.0, 75.0))


def case_pricing_env_override() -> None:
    os.environ["COVAGENT_PRICE_OPUS_IN"] = "9.0"
    os.environ["COVAGENT_PRICE_OPUS_OUT"] = "40.0"
    try:
        _assert("env override applied",
                ct.price_for_model("anthropic/claude-opus-4-8") == (9.0, 40.0),
                str(ct.price_for_model("anthropic/claude-opus-4-8")))
    finally:
        del os.environ["COVAGENT_PRICE_OPUS_IN"]
        del os.environ["COVAGENT_PRICE_OPUS_OUT"]


def case_compute_cost_usd() -> None:
    # 1M in @15 + 1M out @75 = 90.0
    _assert("cost 1M/1M opus", ct.compute_cost_usd("opus", 1_000_000, 1_000_000) == 90.0,
            str(ct.compute_cost_usd("opus", 1_000_000, 1_000_000)))
    # 1245 in + 850 out @ opus
    expect = round(1245 / 1e6 * 15 + 850 / 1e6 * 75, 6)
    _assert("cost small opus", ct.compute_cost_usd("opus", 1245, 850) == expect)


# ── usage / estimation ───────────────────────────────────────────────────────────

def case_extract_usage_variants() -> None:
    _assert("anthropic top-level",
            ct.extract_usage({"input_tokens": 10, "output_tokens": 5}) == (10, 5))
    _assert("openai nested usage",
            ct.extract_usage({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}) == (7, 3))
    _assert("none when absent", ct.extract_usage({"foo": 1}) is None)
    _assert("none on None", ct.extract_usage(None) is None)

    class _U:  # objeto estilo SDK
        prompt_tokens = 4
        completion_tokens = 2
    _assert("object usage via getattr", ct.extract_usage(_U()) == (4, 2))


def case_estimate_tokens() -> None:
    _assert("estimate ~chars/4", ct.estimate_tokens("a" * 40) == 10, str(ct.estimate_tokens("a" * 40)))
    _assert("estimate dict nonzero", ct.estimate_tokens({"x": "y"}) >= 1)
    _assert("estimate none → 0", ct.estimate_tokens(None) == 0)


# ── persistencia + acumulación ───────────────────────────────────────────────────

def case_record_interaction_accumulates() -> None:
    with tempfile.TemporaryDirectory() as d:
        run = Path(d)
        ct.record_interaction(run, run_id="run-X", target_id="tgt:1", role="generation",
                              rnd=0, tokens_in=1000, tokens_out=500, duration_seconds=1.2345,
                              model="anthropic/claude-opus-4-8")
        ct.record_interaction(run, run_id="run-X", target_id="tgt:2", role="repair",
                              rnd=1, tokens_in=200, tokens_out=100, duration_seconds=0.5,
                              model="anthropic/claude-sonnet-4-6")
        data = json.loads((run / ct.TELEMETRY_FILENAME).read_text(encoding="utf-8"))
        _assert("two interactions", len(data["interactions"]) == 2, str(data))
        _assert("totals tokens in", data["total_prompt_tokens"] == 1200, str(data))
        _assert("totals tokens out", data["total_completion_tokens"] == 600, str(data))
        c0 = round(1000 / 1e6 * 15 + 500 / 1e6 * 75, 6)
        c1 = round(200 / 1e6 * 3 + 100 / 1e6 * 15, 6)
        _assert("total usd accumulates", data["total_accumulated_usd"] == round(c0 + c1, 6),
                str(data["total_accumulated_usd"]))
        _assert("duration millis", data["interactions"][0]["duration_seconds"] == 1.234,
                str(data["interactions"][0]["duration_seconds"]))
        _assert("round persisted as int", data["interactions"][1]["round"] == 1)


def case_record_handoff_estimate_split() -> None:
    with tempfile.TemporaryDirectory() as d:
        run = Path(d)
        request = {"targets": [{"targetId": "a", "sutSourceCode": "x" * 400},
                               {"targetId": "b", "sutSourceCode": "y" * 40}]}
        response = {"targets": [{"targetId": "a", "methods": [1, 2, 3]},
                                {"targetId": "b", "methods": []}]}
        rec = ct.record_handoff(run, run_id="run-Y", role="generation", rnd=0,
                                request=request, response=response,
                                target_ids=["a", "b"], duration_seconds=2.0,
                                model="anthropic/claude-opus-4-8")
        _assert("one interaction per target", len(rec) == 2, str(rec))
        _assert("flagged estimated", all(r["estimated"] for r in rec), str(rec))
        _assert("source size_estimate", all(r["source"] == "size_estimate" for r in rec))
        data = json.loads((run / ct.TELEMETRY_FILENAME).read_text(encoding="utf-8"))
        # totales exactos = estimación del payload completo (sin pérdida por redondeo).
        _assert("split sums to whole-request estimate",
                data["total_prompt_tokens"] == ct.estimate_tokens(request),
                f"{data['total_prompt_tokens']} vs {ct.estimate_tokens(request)}")
        _assert("split sums to whole-response estimate",
                data["total_completion_tokens"] == ct.estimate_tokens(response))
        # 'a' tiene más contexto que 'b' → más tokens_in.
        ta = next(r for r in rec if r["targetId"] == "a")
        tb = next(r for r in rec if r["targetId"] == "b")
        _assert("bigger slice → more input tokens", ta["tokens_in"] > tb["tokens_in"],
                f"a={ta['tokens_in']} b={tb['tokens_in']}")
        _assert("duration split even", ta["duration_seconds"] == 1.0, str(ta["duration_seconds"]))


def case_record_handoff_uses_measured_usage() -> None:
    with tempfile.TemporaryDirectory() as d:
        run = Path(d)
        request = {"targets": [{"targetId": "a"}, {"targetId": "b"}]}
        response = {"usage": {"input_tokens": 1000, "output_tokens": 300},
                    "targets": [{"targetId": "a"}, {"targetId": "b"}]}
        rec = ct.record_handoff(run, run_id="run-Z", role="generation", rnd=0,
                                request=request, response=response,
                                target_ids=["a", "b"], duration_seconds=1.0,
                                model="opus")
        _assert("measured not estimated", all(not r["estimated"] for r in rec), str(rec))
        _assert("source api_usage", all(r["source"] == "api_usage" for r in rec))
        data = json.loads((run / ct.TELEMETRY_FILENAME).read_text(encoding="utf-8"))
        _assert("measured totals in", data["total_prompt_tokens"] == 1000, str(data))
        _assert("measured totals out", data["total_completion_tokens"] == 300, str(data))


def case_token_summary_table_totals_and_roles() -> None:
    tele = {
        "total_prompt_tokens": 66116, "total_completion_tokens": 5407,
        "total_accumulated_usd": 1.3973,
        "interactions": [
            {"role": "generation", "tokens_in": 60000, "tokens_out": 5000,
             "cost_usd": 1.30, "estimated": True},
            {"role": "repair", "tokens_in": 6116, "tokens_out": 407,
             "cost_usd": 0.0973, "estimated": True},
        ],
    }
    box = ct.format_token_summary_table(tele)
    _assert("box has title", "RESUMEN FINOPS" in box, box)
    _assert("box shows in tokens (thousands)", "66,116 tok" in box, box)
    _assert("box shows out tokens", "5,407 tok" in box, box)
    _assert("box shows total", "71,523 tok" in box, box)
    _assert("box shows cost", "$1.3973" in box, box)
    _assert("box flags estimated source", "estimado" in box, box)
    _assert("box has per-role generation", "generation:" in box, box)
    _assert("box has per-role repair", "repair:" in box, box)

    agg = ct.aggregate_by_role(tele["interactions"])
    _assert("role agg in", agg["generation"]["in"] == 60000, str(agg))
    _assert("role agg counts", agg["repair"]["n"] == 1, str(agg))

    # Tolerant: empty telemetry yields a zeroed box, never raises.
    empty = ct.format_token_summary_table({})
    _assert("empty box renders", "0 tok" in empty and "sin datos" in empty, empty)


def main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("case_")]
    for c in cases:
        try:
            c()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{c.__name__} raised {type(exc).__name__}: {exc}")
    if FAILURES:
        print("FAIL test_finops_telemetry:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK   test_finops_telemetry: {len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
