"""archetype_detector.py — detect BGBA parent archetype per module.

Emits state/archetype-profile.json.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lxml import etree

from common import atomic_write_json, fail, find_pom_modules, validate

NS = {"m": "http://maven.apache.org/POM/4.0.0"}

ARCHETYPE_MAP = {
    "bgba-parent-paas-java-8": "java-8",
    "bgba-parent-paas-java-21": "java-21",
    "bgba-parent-pom": "common",
}

IMPLIES = {
    "java-8": {
        "java": "8",
        "springBoot": "2.x",
        "namespace": "javax",
        "jacoco": "manual",
        "junit": "5",
    },
    "java-21": {
        "java": "21",
        "springBoot": "3.x",
        "namespace": "jakarta",
        "jacoco": "inherited",
        "junit": "5",
    },
    "common": {"namespace": "none", "jacoco": "absent", "junit": "5"},
    "unknown": {"namespace": "none", "jacoco": "absent", "junit": "5"},
}

RULES = {
    "java-21": [
        "Forbidden imports: javax.servlet.*, javax.persistence.*, javax.validation.*, javax.ws.rs.*",
        "JaCoCo inherited from parent; do NOT add plugin manually",
        "Use JUnit 5 only; @ExtendWith(MockitoExtension.class)",
    ],
    "java-8": [
        "Forbidden imports: jakarta.*",
        "Forbidden Java 9+ APIs (List.of, Map.of, var, records, text blocks)",
        "If JaCoCo not configured, bootstrap via CLI (no POM edits)",
    ],
    "common": [],
    "unknown": [],
}


def detect_parent(pom_path: Path) -> dict | None:
    tree = etree.parse(str(pom_path))
    root = tree.getroot()
    parent = root.find("m:parent", namespaces=NS)
    if parent is None:
        return None
    return {
        "groupId": parent.findtext("m:groupId", namespaces=NS),
        "artifactId": parent.findtext("m:artifactId", namespaces=NS),
        "version": parent.findtext("m:version", namespaces=NS),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--changelogs",
        default="docs/archetypes/changelogs",
        help="relative path inside repo (or absolute)",
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state_dir = Path(args.out).resolve()
    changelogs_dir = Path(args.changelogs)
    if not changelogs_dir.is_absolute():
        changelogs_dir = repo / changelogs_dir

    modules_out = []
    for mod_dir in find_pom_modules(repo):
        pom = mod_dir / "pom.xml"
        parent = None
        try:
            parent = detect_parent(pom)
        except etree.XMLSyntaxError:
            pass
        artifact_id = parent.get("artifactId") if parent else None
        archetype = ARCHETYPE_MAP.get(artifact_id or "", "unknown")
        changelog_path = None
        if archetype == "java-8":
            cl = changelogs_dir / "CHANGELOG_bgba-parent-paas-java-8.md"
            if cl.exists():
                changelog_path = str(cl)
        elif archetype == "java-21":
            cl = changelogs_dir / "CHANGELOG_bgba-parent-paas-java-21.md"
            if cl.exists():
                changelog_path = str(cl)
        elif archetype == "common":
            cl = changelogs_dir / "CHANGELOG_bgba-parent-pom.md"
            if cl.exists():
                changelog_path = str(cl)
        modules_out.append(
            {
                "path": str(mod_dir.resolve()),
                "parent": parent or {},
                "archetype": archetype,
                "implies": IMPLIES[archetype],
                "changelog": changelog_path,
                "rulesApplied": RULES[archetype],
                "discrepancies": [],
            }
        )

    if not modules_out:
        fail("No modules detected.")

    out = {"schemaVersion": 1, "modules": modules_out}
    validate("archetype-profile", out)
    atomic_write_json(state_dir / "archetype-profile.json", out)
    print(f"[OK] {state_dir/'archetype-profile.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
