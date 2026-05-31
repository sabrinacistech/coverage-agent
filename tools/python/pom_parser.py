"""pom_parser.py — parse pom.xml(s) and emit state/build-tool-contract.json.

Detects:
- Maven multi-module structure.
- Java version (`maven.compiler.release|source|target`).
- JaCoCo plugin configuration.
- packaging.

Does NOT modify any pom. Uses lxml.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lxml import etree

from common import atomic_write_json, fail, find_pom_modules, validate

NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def _txt(node, xp: str) -> str | None:
    r = node.xpath(xp, namespaces=NS)
    if r:
        if isinstance(r[0], etree._Element):
            return (r[0].text or "").strip() or None
        return str(r[0])
    return None


def parse_pom(pom_path: Path) -> dict:
    tree = etree.parse(str(pom_path))
    root = tree.getroot()
    name = _txt(root, "m:artifactId/text()") or pom_path.parent.name
    packaging = _txt(root, "m:packaging/text()") or "jar"
    # Java version
    java = (
        _txt(root, "m:properties/m:maven.compiler.release/text()")
        or _txt(root, "m:properties/m:maven.compiler.target/text()")
        or _txt(root, "m:properties/m:java.version/text()")
        or None
    )
    # JaCoCo plugin
    jacoco_nodes = root.xpath(
        "//m:plugin[m:artifactId='jacoco-maven-plugin']", namespaces=NS
    )
    jacoco_configured = bool(jacoco_nodes)
    return {
        "name": name,
        "path": str(pom_path.parent.resolve()),
        "packaging": packaging,
        "java": java,
        "jacoco_configured": jacoco_configured,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True, help="state directory")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state_dir = Path(args.out).resolve()

    modules = []
    for mod_dir in find_pom_modules(repo):
        pom = mod_dir / "pom.xml"
        if not pom.exists():
            continue
        try:
            modules.append(parse_pom(pom))
        except etree.XMLSyntaxError as e:
            print(f"[WARN] cannot parse {pom}: {e}", file=sys.stderr)

    if not modules:
        fail("No pom.xml found. Is this a Maven project?")

    # Pick java version: most common non-null among modules
    java_versions = [m["java"] for m in modules if m["java"]]
    java = max(set(java_versions), key=java_versions.count) if java_versions else "unknown"

    # JaCoCo report path (assume default for Maven)
    jacoco_any = any(m["jacoco_configured"] for m in modules)
    out = {
        "schemaVersion": 1,
        "tool": "maven",
        "rootPom": str((repo / "pom.xml").resolve()),
        "modules": [
            {"name": m["name"], "path": m["path"], "packaging": m["packaging"]}
            for m in modules
        ],
        "java": java,
        "jacoco": {
            "configured": jacoco_any,
            "reportXml": "target/site/jacoco/jacoco.xml",
            "execFile": "target/jacoco.exec",
        },
    }
    validate("build-tool-contract", out)
    atomic_write_json(state_dir / "build-tool-contract.json", out)
    print(f"[OK] {state_dir/'build-tool-contract.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
