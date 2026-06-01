"""G5 (stack profile) valida IDENTIDAD de frameworks, no versiones.

Regresión: una versión `unknown` (gestionada por el BOM) no debe bloquear; un
framework `unknown` sí.
"""
from __future__ import annotations

import sys

from orchestrator import config

sys.path.insert(0, str(config.TOOLS_PYTHON))
from gate_runner import gate_g5  # noqa: E402


def test_g5_passes_when_versions_unknown_but_frameworks_known():
    pack = {"stack": {
        "javaVersion": "21", "testFramework": "junit5", "mockFramework": "mockito",
        "testVersion": "unknown", "mockVersion": "unknown", "assertFramework": "assertj",
        "springEnabled": True, "springBootVersion": "3.3.5", "namespaceStyle": "none",
    }}
    assert gate_g5(pack)["status"] == "PASS"


def test_g5_fails_when_a_framework_is_unknown():
    pack = {"stack": {
        "javaVersion": "21", "testFramework": "unknown", "mockFramework": "mockito",
        "assertFramework": "assertj", "springEnabled": True, "namespaceStyle": "none",
    }}
    assert gate_g5(pack)["status"] == "FAIL"


def test_g5_compact_tuple_ignores_version_positions():
    # stk posicional con testVersion/mockVersion unknown (idx 6,7) → PASS.
    pack = {"stk": ["21", "junit5", "mockito", "assertj", True, "none", "unknown", "unknown", "3.3.5"]}
    assert gate_g5(pack)["status"] == "PASS"
