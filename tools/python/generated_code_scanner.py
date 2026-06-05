"""generated_code_scanner.py — detect CXF, OpenAPI Generator and APs.

Emits state/generated-code-index.json per module (merged in a single file).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from lxml import etree

from common import atomic_write_json, find_pom_modules, long_path, validate

NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def _resolve(s: str | None, module_dir: Path) -> str | None:
    if not s:
        return None
    return (
        s.replace("${project.build.directory}", str(module_dir / "target"))
        .replace("${project.basedir}", str(module_dir))
        .replace("${basedir}", str(module_dir))
    )


def _jaxb_pkg_from_namespace(ns: str | None) -> str | None:
    """Map an XML namespace to the Java package CXF/JAXB generates by default.

    Mirrors the JAXB default mapping (spec App. D.5.1, common case): reverse the
    host labels, append the path segments, lowercase, replace non-identifier chars
    with '_', and prefix '_' to segments starting with a digit. Example:
      http://webservices.ws.bancogalicia.com.ar/abmcinfoclientes/personafisica/3.0.0
      → ar.com.bancogalicia.ws.webservices.abmcinfoclientes.personafisica._3_0_0
    Best effort: if the mapping is slightly off, bytecode_scanner's skip-invalid
    net still drops the oversized generated contract, so nothing aborts the run.
    """
    ns = (ns or "").strip()
    if not ns:
        return None
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/]+)(/.*)?$", ns)
    if m:
        host = m.group(1).split(":")[0]  # drop port
        host_labels = [l for l in host.split(".") if l]
        if host_labels and host_labels[0].lower() == "www":
            host_labels = host_labels[1:]
        segs = list(reversed(host_labels)) + [s for s in (m.group(2) or "").split("/") if s]
    else:
        # urn:foo:bar or other: treat ':' and '/' as separators, no host reversal.
        segs = [s for s in re.split(r"[:/]", ns) if s and s.lower() not in ("urn",)]
    out: list[str] = []
    for s in segs:
        s = re.sub(r"[^A-Za-z0-9_]", "_", s)
        if not s:
            continue
        if s[0].isdigit():
            s = "_" + s
        out.append(s.lower())
    return ".".join(out) if out else None


def _namespaces_from_wsdl(wsdl_path: Path) -> set[str]:
    """All targetNamespace values in a WSDL (the <definitions> plus every embedded
    <xsd:schema>) — the generated DTO packages derive from these."""
    found: set[str] = set()
    try:
        tree = etree.parse(str(long_path(wsdl_path)))
        for el in tree.iter():
            tns = el.get("targetNamespace")
            if tns:
                found.add(tns)
    except Exception:
        pass
    return found


def scan_module(pom_path: Path) -> dict:
    mod_dir = pom_path.parent
    tree = etree.parse(str(pom_path))
    root = tree.getroot()

    generators: list[dict] = []
    excluded_packages: set[str] = set()
    excluded_fqcns: set[str] = set()
    blocked: list[dict] = []
    extra_source_roots: set[Path] = set()

    # CXF Codegen — like OpenAPI, but the generated package is NOT declared in the
    # POM: CXF/JAXB derives it from each WSDL's targetNamespace. So we parse the
    # WSDL(s) and map their namespaces to packages, adding them to excludedPackages
    # exactly as openapi modelPackage/apiPackage are. We also resolve the plugin's
    # <sourceRoot> so the FQCN walk below can read the actual generated .java.
    for plugin in root.xpath(
        "//m:plugin[m:artifactId='cxf-codegen-plugin']", namespaces=NS
    ):
        src_root = _resolve(plugin.findtext(".//m:sourceRoot", namespaces=NS), mod_dir)
        if not src_root:
            # CXF's default output directory when <sourceRoot> is omitted.
            src_root = str(mod_dir / "target" / "generated-sources" / "cxf")
        extra_source_roots.add(Path(src_root))
        excluded_packages.add(src_root.replace("\\", "/").rstrip("/") + "/**")

        for wsdl_node in plugin.xpath(".//m:wsdl", namespaces=NS):
            raw = wsdl_node.text or ""
            wsdl = _resolve(raw.strip(), mod_dir)
            exists = bool(wsdl) and Path(wsdl).exists()
            pkgs: list[str] = []
            if exists:
                for tns in _namespaces_from_wsdl(Path(wsdl)):
                    pkg = _jaxb_pkg_from_namespace(tns)
                    if pkg:
                        pkgs.append(pkg)
                        excluded_packages.add(pkg)
            entry = {
                "kind": "cxf",
                "wsdl": wsdl or "",
                "wsdlExists": exists,
                "packages": sorted(set(pkgs)),
            }
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

    # Walk generated source roots to collect FQCNs (best effort). Includes any
    # explicit CXF <sourceRoot> in case it lives outside the default dirs.
    for gen_root in {
        mod_dir / "target" / "generated-sources",
        mod_dir / "build" / "generated",
        *extra_source_roots,
    }:
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
