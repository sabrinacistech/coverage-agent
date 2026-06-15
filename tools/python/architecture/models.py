from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class RepoRef:
    host: str
    owner: str
    repo: str
    api_base: str


@dataclass(frozen=True)
class SourceFile:
    path: str
    kind: str
    size: int


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    category: str
    title: str
    description: str
    evidence: list[str]
    recommendation: str
    source: str
    confidence: float


@dataclass(frozen=True)
class ReviewArtifacts:
    inventory: dict[str, Any]
    architecture_map: dict[str, Any]
    dependency_map: dict[str, Any]
    findings: list[Finding]


def dataclass_dict(obj: object) -> dict[str, Any]:
    return asdict(obj)
