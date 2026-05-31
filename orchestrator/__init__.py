"""Capa de orquestación v2 del coverage-agent.

Construye, encima del núcleo determinista (``tools/python``), el driver LLM
autónomo que antes faltaba: gateway de modelos (LiteLLM), prompts (LangChain) y,
en M2, la máquina de estados del ciclo (LangGraph).

Regla de oro: esta capa ORQUESTA; NO adjudica gates. Los gates G1-G8 y el
presupuesto siguen siendo Python determinista en ``tools/python`` (gate_runner,
budget_enforcer, test_patch_applier). El LLM solo produce un patch-descriptor;
nunca decide si un gate pasó.
"""

__version__ = "2.0.0a0"
