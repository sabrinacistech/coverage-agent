"""generated_code_scanner.py — detect CXF, OpenAPI Generator and APs.

Emits state/generated-code-index.json per module (merged in a single file).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from lxml import etree

from common import atomic_write_json, find_pom_modules, long_path, validate

NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def _resolve(s: str | None, module_dir: Path) -> str | None:
    if not s:
        return None
    return s.replace("${project.basedir}", str(module_dir)).replace(
        "${basedir}", str(module_dir)
    )


def scan_module(pom_path: Path) -> dict:
    mod_dir = pom_path.parent
    tree = etree.parse(str(pom_path))
    root = tree.getroot()

    generators: list[dict] = []
    excluded_packages: set[str] = set()
    excluded_fqcns: set[str] = set()
    blocked: list[dict] = []

    # CXF Codegen
    for plugin in root.xpath(
        "//m:plugin[m:artifactId='cxf-codegen-plugin']", namespaces=NS
    ):
        for wsdl_node in plugin.xpath(".//m:wsdl", namespaces=NS):
            raw = wsdl_node.text or ""
            wsdl = _resolve(raw.strip(), mod_dir)
            exists = bool(wsdl) and Path(wsdl).exists()
            entry = {"kind": "cxf", "wsdl": wsdl or "", "wsdlExists": exists, "packages": []}
            generators.append(entry)
            if not exists:
                blocked.append({"kind": "cxf", "reason": "BLOCKED_MISSING_CONTRACT", "wsdl": wsdl or ""})

    # OpenAPI Generator
    for plugin in root.xpath(
        "//m:plugin[m:artifactId='openapi-generator-maven-plugin']", namespaces=NS
    ):
        for execution in plugin.xpath(".//m:execution", namespaces=NS) or [plugin]:
            cfg = execution.find("m:configuration", namespaces=NS) or plugin.find(
                "m:configuration", namespaces=NS
            )
            if cfg is None:
                continue
            spec = _resolve(cfg.findtext("m:inputSpec", namespaces=NS), mod_dir)
            api_pkg = cfg.findtext("m:apiPackage", namespaces=NS)
            model_pkg = cfg.findtext("m:modelPackage", namespaces=NS)
            source_folder = cfg.findtext("m:sourceFolder", namespaces=NS)
            entry = {
                "kind": "openapi",
                "spec": spec or "",
                "specExists": bool(spec) and Path(spec).exists(),
                "apiPackage": api_pkg or "",
                "modelPackage": model_pkg or "",
                "sourceFolder": source_folder or "target/generated-sources/openapi/src/main/java",
            }
            generators.append(entry)
            if api_pkg:
                excluded_packages.add(api_pkg)
            if model_pkg:
                excluded_packages.add(model_pkg)
            if not entry["specExists"]:
                blocked.append(
                    {"kind": "openapi", "reason": "BLOCKED_MISSING_CONTRACT", "spec": entry["spec"]}
                )

    # Annotation processors (Lombok / FreeBuilder / MapStruct / Immutables / AutoValue)
    deps_xpaths = {
        "lombok": "//m:dependency[m:groupId='org.projectlombok' and m:artifactId='lombok']",
        "freebuilder": "//m:dependency[m:groupId='org.inferred' and m:artifactId='freebuilder']",
        "mapstruct": "//m:dependency[m:artifactId='mapstruct-processor' or m:artifactId='mapstruct']",
        "immutables": "//m:dependency[m:groupId='org.immutables' and m:artifactId='value']",
        "autovalue": "//m:dependency[m:groupId='com.google.auto.value']",
    }
    for kind, xp in deps_xpaths.items():
        if root.xpath(xp, namespaces=NS):
            generators.append({"kind": kind})

    # Default generated source roots
    for d in ("target/generated-sources", "build/generated", "src/generated"):
        excluded_packages.add(d + "/**")

    # Walk generated source roots to collect FQCNs (best effort)
    for gen_root in (
        mod_dir / "target" / "generated-sources",
        mod_dir / "build" / "generated",
    ):
        if not gen_root.exists():
            continue
        # long_path() prefixes \\?\ on Windows so deeply nested OpenAPI/CXF
        # generated trees (frequently > MAX_PATH = 260) are still walkable.
        for path, dirs, files in os.walk(long_path(gen_root)):
            dirs.sort()
            for fn in sorted(files):
                if not fn.endswith(".java"):
                    continue
                fp = Path(path) / fn
                # crude: parse package + class name from first lines
                try:
                    pkg = ""
                    cls = fp.stem
                    with open(long_path(fp), "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("package "):
                                pkg = line[len("package "):].rstrip(";").strip()
                                break
                    excluded_fqcns.add(f"{pkg}.{cls}" if pkg else cls)
                except Exception:
                    pass

    return {
        "module": str(mod_dir.resolve()),
        "generators": generators,
        "excludedFqcns": sorted(excluded_fqcns),
        "excludedPackages": sorted(excluded_packages),
        "blocked": blocked,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    state_dir = Path(args.out).resolve()

    aggregated_modules = []
    for mod_dir in find_pom_modules(repo, contract=state_dir / "build-tool-contract.json"):
        pom = mod_dir / "pom.xml"
        if pom.exists():
            try:
                aggregated_modules.append(scan_module(pom))
            except etree.XMLSyntaxError as e:
                print(f"[WARN] cannot parse {pom}: {e}", file=sys.stderr)

    # Single file per repo with first/primary module; for multi-module, write one per module
    out_dir = state_dir / "generated-code-index"
    out_dir.mkdir(parents=True, exist_ok=True)
    for m in aggregated_modules:
        out = {
            "schemaVersion": 1,
            "module": m["module"],
            "generators": m["generators"],
            "excludedFqcns": m["excludedFqcns"],
            "excludedPackages": m["excludedPackages"],
            "blocked": m["blocked"],
        }
        validate("generated-code-index", out)
        # safe filename
        safe = m["module"].replace(":", "_").replace("/", "_").replace("\\", "_")
        atomic_write_json(out_dir / f"{safe}.json", out)

    # Also write a flat single-file (first module) for convenience
    if aggregated_modules:
        first = aggregated_modules[0]
        out = {
            "schemaVersion": 1,
            "module": first["module"],
            "generators": first["generators"],
            "excludedFqcns": first["excludedFqcns"],
            "excludedPackages": first["excludedPackages"],
            "blocked": first["blocked"],
        }
        atomic_write_json(state_dir / "generated-code-index.json", out)
    print(f"[OK] {state_dir/'generated-code-index.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
