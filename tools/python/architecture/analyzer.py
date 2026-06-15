from __future__ import annotations

import re
from typing import Any

from . import rules
from .models import Finding, SourceFile

try:
    import javalang  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    javalang = None


def _regex_package(java_text: str) -> str | None:
    match = re.search(r"^\s*package\s+([\w.]+)\s*;", java_text, flags=re.MULTILINE)
    return match.group(1) if match else None


def _regex_imports(java_text: str) -> list[str]:
    return re.findall(r"^\s*import\s+([\w.]+)\s*;", java_text, flags=re.MULTILINE)


def _regex_annotations(java_text: str) -> list[str]:
    return sorted(set(re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)", java_text)))


def parse_java(java_text: str) -> dict[str, Any]:
    """Parse Java using javalang when available, falling back to regex signals."""
    if javalang is not None:
        try:
            tree = javalang.parse.parse(java_text)
            annotations: set[str] = set()
            type_refs: set[str] = set()
            for _, node in tree:
                for ann in getattr(node, "annotations", []) or []:
                    name = getattr(ann, "name", "")
                    if name:
                        annotations.add(str(name).split(".")[-1])
                type_node = getattr(node, "type", None)
                type_name = getattr(type_node, "name", None)
                if type_name:
                    type_refs.add(str(type_name).split(".")[-1])
                for param in getattr(node, "parameters", []) or []:
                    param_type = getattr(param, "type", None)
                    param_type_name = getattr(param_type, "name", None)
                    if param_type_name:
                        type_refs.add(str(param_type_name).split(".")[-1])
            return {
                "package": getattr(tree, "package", None).name if getattr(tree, "package", None) else None,
                "imports": [imp.path for imp in getattr(tree, "imports", [])],
                "annotations": sorted(annotations),
                "type_refs": sorted(type_refs),
                "parser": "javalang",
            }
        except Exception:
            pass
    return {
        "package": _regex_package(java_text),
        "imports": _regex_imports(java_text),
        "annotations": _regex_annotations(java_text),
        "type_refs": re.findall(r"\b([A-Z][A-Za-z0-9_]*(?:Repository|Service|Controller|Client))\b", java_text),
        "parser": "regex",
    }


def build_maps(files: list[SourceFile], contents: dict[str, str]) -> tuple[dict, dict, list[Finding]]:
    java_files = [f for f in files if f.path.endswith(".java")]
    packages: dict[str, list[str]] = {}
    components: dict[str, list[str]] = {
        "controllers": [],
        "services": [],
        "repositories": [],
        "entities": [],
        "dtos": [],
        "configs": [],
        "other_java": [],
    }
    edges: list[dict] = []
    findings: list[Finding] = []
    parser_counts = {"javalang": 0, "regex": 0}

    for f in java_files:
        text = contents.get(f.path, "")
        parsed = parse_java(text)
        parser_counts[parsed["parser"]] = parser_counts.get(parsed["parser"], 0) + 1
        pkg = parsed["package"] or "(default)"
        packages.setdefault(pkg, []).append(f.path)

        anns = set(parsed["annotations"])
        lower = f.path.lower()
        is_controller = "RestController" in anns or "Controller" in anns or "/controller/" in lower
        is_service = "Service" in anns or "/service/" in lower
        is_repository = "Repository" in anns or "/repository/" in lower
        is_entity = "Entity" in anns or "/entity/" in lower or "/model/" in lower
        is_dto = "/dto/" in lower or lower.endswith("dto.java")
        is_config = "Configuration" in anns or "/config/" in lower

        if is_controller:
            components["controllers"].append(f.path)
            item = rules.controller_repository_dependency(
                f.path,
                parsed["imports"],
                parsed["type_refs"],
            ) or rules.controller_repository_coupling(f.path, text)
            if item:
                findings.append(item)
        elif is_service:
            components["services"].append(f.path)
            item = rules.service_depends_on_web_layer(f.path, parsed["imports"], parsed["annotations"])
            if item:
                findings.append(item)
        elif is_repository:
            components["repositories"].append(f.path)
        elif is_entity:
            components["entities"].append(f.path)
            item = rules.entity_exposed_as_controller(f.path, parsed["annotations"])
            if item:
                findings.append(item)
        elif is_dto:
            components["dtos"].append(f.path)
        elif is_config:
            components["configs"].append(f.path)
        else:
            components["other_java"].append(f.path)

        for imp in parsed["imports"]:
            edges.append({"source": f.path, "target_import": imp})

        item = rules.system_out_usage(f.path, text)
        if item:
            findings.append(item)

    config_files = [f.path for f in files if f.kind == "config"]
    for path in config_files:
        item = rules.hardcoded_secret(path, contents.get(path, ""))
        if item:
            findings.append(item)

    framework_signals = {
        "spring_boot": any("SpringApplication" in contents.get(f.path, "") for f in java_files),
        "spring_web": any("RestController" in contents.get(f.path, "") for f in java_files),
        "spring_data_jpa": any("JpaRepository" in contents.get(f.path, "") for f in java_files),
        "spring_security": any(
            "SecurityFilterChain" in contents.get(f.path, "") or
            "EnableWebSecurity" in contents.get(f.path, "")
            for f in java_files
        ),
        "actuator_configured": any("management.endpoints" in contents.get(p, "") for p in config_files),
    }

    if components["controllers"] and not components["services"]:
        findings.append(rules.controllers_without_services(components["controllers"]))
    if components["entities"] and not components["dtos"]:
        findings.append(rules.entities_without_dtos(components["entities"]))
    if not framework_signals["actuator_configured"]:
        findings.append(rules.actuator_not_detectable(config_files))

    architecture_map = {
        "schemaVersion": 1,
        "packages": packages,
        "components": components,
        "framework_signals": framework_signals,
        "analysis": {"java_parser_counts": parser_counts},
        "ci_delivery": {
            "github_actions": [f.path for f in files if f.kind == "ci"],
            "dockerfiles": [f.path for f in files if f.kind == "docker"],
        },
    }
    dependency_map = {"schemaVersion": 1, "edges": edges, "edge_count": len(edges)}
    return architecture_map, dependency_map, findings
