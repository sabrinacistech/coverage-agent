"""inherited_evidence.py — synthetic evidence for JDK-inherited Throwable methods.

An exception SUT rarely re-declares ``getMessage``/``getCause``/``toString`` — it
inherits them from ``java.lang.Throwable`` — so they never appear in the
bytecode-derived symbol-contracts. Yet asserting ``new FooException("x")
.getMessage()`` is a legitimate, non-hallucinated test. This module is the SINGLE
source of truth for the synthetic evidenceIds that authorize those inherited
calls, so the request side (batch_runner advertises them to the model) and the
gate side (gate_runner.G2 must accept them) can never drift apart — the drift
that made G2 reject evidence the orchestrator itself had advertised.
"""
from __future__ import annotations

import hashlib

# Inherited methods we authorize on any Throwable subclass: (name, returnType).
THROWABLE_METHODS: tuple[tuple[str, str], ...] = (
    ("getMessage", "java.lang.String"),
    ("getCause", "java.lang.Throwable"),
    ("toString", "java.lang.String"),
)


def is_throwable_sut(sut: str, classification_type: str | None = None) -> bool:
    """True when the SUT is an exception/throwable — by classification when
    known, else by the conventional Exception/Error name suffix."""
    if classification_type and str(classification_type).lower() == "exception":
        return True
    return bool(sut) and sut.endswith(("Exception", "Error"))


def throwable_evidence_id(sut: str, name: str) -> str:
    """Deterministic synthetic evidenceId for an inherited Throwable method."""
    raw = f"sym:{sut}#{name}:java.lang.Throwable"
    return f"sym:{sut}#{name}:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]}"


def throwable_evidence_ids(sut: str) -> set[str]:
    """The set of synthetic evidenceIds authorized for ``sut``'s inherited methods."""
    return {throwable_evidence_id(sut, name) for name, _ in THROWABLE_METHODS}


def throwable_evidence_refs(sut: str) -> list[dict]:
    """evidenceRefs rows (kind='method') for the inherited Throwable methods."""
    return [
        {
            "evidenceId": throwable_evidence_id(sut, name),
            "kind": "method",
            "name": name,
            "returnType": return_type,
            "params": [],
            "inheritedFrom": "java.lang.Throwable",
        }
        for name, return_type in THROWABLE_METHODS
    ]
