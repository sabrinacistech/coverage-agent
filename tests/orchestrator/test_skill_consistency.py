"""Consistencia de reglas de TODA la solución (markdown-driven).

Norma del proyecto: tras cada cambio, la solución no debe tener reglas duplicadas
que deriven, links rotos, ni skills aislados. Este test la enforza por construcción
(corre en la suite, junto al tripwire test_no_archived_agent_refs.py que cubre las
refs a agentes archivados).

Checks:
  1. test_no_broken_skill_links     — todo link a `skills/.../*.md` existe.
  2. test_jacoco_version_single     — una sola versión de JaCoCo en docs/skills.
  3. test_no_orphan_skills          — cada skill está referenciado por algún driver
                                       o marcado como fase DETERMINISTA (no aislado).
"""
from __future__ import annotations

import re
from pathlib import Path

# tests/orchestrator/<this> → repo root
ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / "skills"

CANONICAL_JACOCO = "0.8.13"


def _live_md() -> list[Path]:
    """Todos los .md vivos (excluye cualquier cosa bajo _archive/)."""
    return [p for p in ROOT.rglob("*.md") if "_archive" not in p.parts]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def test_no_broken_skill_links():
    pat = re.compile(r"skills/[A-Za-z0-9_][A-Za-z0-9_./-]*\.md")
    missing: list[tuple[str, str]] = []
    for md in _live_md():
        for ref in set(pat.findall(_read(md))):
            if not (ROOT / ref).exists():
                missing.append((str(md.relative_to(ROOT)), ref))
    assert not missing, f"Links a skills inexistentes: {missing}"


def test_jacoco_version_single():
    pat = re.compile(r"0\.8\.\d+")  # las versiones de jacoco-maven-plugin son 0.8.x
    found: dict[str, list[str]] = {}
    for md in _live_md():
        for v in pat.findall(_read(md)):
            found.setdefault(v, []).append(str(md.relative_to(ROOT)))
    extra = {v: sorted(set(loc)) for v, loc in found.items() if v != CANONICAL_JACOCO}
    assert not extra, f"Versiones JaCoCo != {CANONICAL_JACOCO}: {extra}"


def test_no_orphan_skills():
    skills = [p for p in SKILLS_DIR.rglob("*.md")
              if "_archive" not in p.parts and p.name != "README.md"]
    live_texts = {p: _read(p) for p in _live_md()}
    orphans: list[str] = []
    for s in skills:
        name = s.stem  # p.ej. 'constructor-verification'
        is_banner = "DETERMINISTA" in live_texts.get(s, "")
        referenced = any(name in txt for p, txt in live_texts.items() if p != s)
        if not (is_banner or referenced):
            orphans.append(str(s.relative_to(ROOT)))
    assert not orphans, (
        "Skills aislados (ni referenciados por un driver ni marcados DETERMINISTA). "
        "Cableá una referencia o agregá el banner: " + ", ".join(orphans))
