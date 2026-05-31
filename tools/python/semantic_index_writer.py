"""semantic_index_writer.py — Phase 1: build state/index/ from existing state artifacts.

Reads (already produced by prior pipeline steps):
  - state/symbol-contracts/<fqcn>.json  (per-SUT bytecode contracts)
  - state/import-whitelist.json         (resolved classpath packages + classes)
  - state/dependency-graph.json         (inter-class dependency map)
  - state/classification-index.json     (classification with annotations)
  - state/build-tool-contract.json      (module / Java version metadata)

Writes:
  - state/index/classes.json     — FQCN → {kind, modifiers, annotations, file, parents}
  - state/index/methods.json     — FQCN → [{name, params, returnType, modifiers, evidenceId}]
  - state/index/imports.json     — {packages: [...], classes: [...]} (from whitelist)
  - state/index/dependencies.json — {classes: {fqcn: {uses, implements, extends_, injects}}}
  - state/index/annotations.json — {classes: {fqcn: [annotations]}, methods: {fqcn#method: [...]}}

Goal: after this step every agent queries state/index/ instead of re-reading sources,
POMs, classpath or javap output — eliminating O(agents × files) redundant work.

Design:
  - Pure projection of existing state; no new parsing.
  - Atomic writes (*.tmp + rename) for each index file.
  - Incremental: re-reads only contracts whose mtime > index mtime (unless --full).
  - Idempotent: same inputs → same outputs byte-exact (sorted keys).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import atomic_write_json, load_json, sha256_file

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

VERSION = 1  # bump on schema changes → triggers full re-index

# Relative path from execution_folder/state/index/ to the static schema definition.
# Convention: execution folders live at the same repo-root level as java-test-coverage-architecture/.
_INDEX_SCHEMA_REF = "../../../java-test-coverage-architecture/state/_schemas/semantic-index.schema.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_safe(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return load_json(path)
    except Exception as exc:
        print(f"[WARN] could not load {path}: {exc}", file=sys.stderr)
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Index builders
# ─────────────────────────────────────────────────────────────────────────────

def build_classes_index(contracts_dir: Path) -> dict:
    """FQCN → {kind, modifiers, annotations, parents, instantiation}."""
    classes: dict[str, dict] = {}
    for cp in sorted(contracts_dir.glob("*.json")):
        try:
            c = load_json(cp)
        except Exception:
            continue
        fqcn = c.get("fqcn")
        if not fqcn:
            continue
        classes[fqcn] = {
            "kind": c.get("kind", "class"),
            "modifiers": c.get("modifiers", []),
            "annotations": c.get("annotations", []),
            "parents": {
                "extends": c.get("extends"),
                "implements": c.get("implements", []),
            },
            "instantiation": c.get("instantiation", {}).get("strategy", "unknown"),
            "source": c.get("source"),
        }
    return {
        "version": VERSION,
        "generatedAt": _now(),
        "count": len(classes),
        "classes": classes,
    }


def build_methods_index(contracts_dir: Path) -> dict:
    """FQCN → list of method descriptors with evidenceId."""
    methods: dict[str, list] = {}
    for cp in sorted(contracts_dir.glob("*.json")):
        try:
            c = load_json(cp)
        except Exception:
            continue
        fqcn = c.get("fqcn")
        if not fqcn:
            continue
        entries = []
        # Constructors
        for ctor in c.get("constructors", []):
            entries.append({
                "kind": "constructor",
                "name": "<init>",
                "params": [p.get("type") for p in ctor.get("params", [])],
                "returnType": "void",
                "modifiers": [ctor.get("visibility", "public")],
                "throws": ctor.get("throws", []),
                "evidenceId": ctor.get("evidenceId"),
            })
        # Methods
        for m in c.get("methods", []):
            if not m.get("usable", True):
                continue
            entries.append({
                "kind": "method",
                "name": m.get("name"),
                "params": [p.get("type") for p in m.get("params", [])],
                "returnType": m.get("returnType"),
                "modifiers": m.get("modifiers", []),
                "throws": m.get("throws", []),
                "evidenceId": m.get("evidenceId"),
            })
        # Builders
        for b in c.get("builders", []):
            entries.append({
                "kind": "builder",
                "name": b.get("entry"),
                "builderKind": b.get("kind"),
                "setters": b.get("setters", []),
                "evidenceId": b.get("evidenceId"),
            })
        if entries:
            methods[fqcn] = entries
    return {
        "version": VERSION,
        "generatedAt": _now(),
        "count": sum(len(v) for v in methods.values()),
        "methods": methods,
    }


def build_imports_index(whitelist_path: Path) -> dict:
    """Flattened view of import-whitelist: packages + classes for quick lookup."""
    wl = _load_safe(whitelist_path, {})
    packages = [p["name"] for p in wl.get("packages", []) if "name" in p]
    classes = [c["fqcn"] for c in wl.get("classes", []) if "fqcn" in c]
    # Build fast lookup sets (serialised as sorted lists for determinism)
    return {
        "version": VERSION,
        "generatedAt": _now(),
        "module": wl.get("module"),
        "packageCount": len(packages),
        "classCount": len(classes),
        "packages": sorted(packages),
        "classes": sorted(classes),
    }


def build_dependencies_index(dep_graph_path: Path) -> dict:
    """Project dependency-graph.json into index/dependencies.json.

    Supports two source formats:

    1. Schema-canonical format (written by LLM agents, validated by schema):
       {"schemaVersion": 1, "graphs": [{"sut": "FQCN", "dependencies": [...], ...}]}

    2. Legacy flat format (written by some older agent versions):
       {"classes": {"FQCN": {"uses": [...], "injects": [...], ...}}}

    Both are normalised to the same index structure:
      FQCN → {uses, implements, extends, injects, repositories, clients, exceptions}
    """
    dg = _load_safe(dep_graph_path, {})
    normalised: dict[str, dict] = {}

    if "graphs" in dg:
        # ── Schema-canonical format ────────────────────────────────────────────
        for entry in dg.get("graphs", []):
            fqcn = entry.get("sut")
            if not fqcn:
                continue
            deps = entry.get("dependencies", [])
            collab = entry.get("collaboratorUsage", [])
            # DI injectables: constructor / field / setter injected types
            injects = [d["type"] for d in deps if "type" in d]
            # Collaborator types actually called in the SUT
            uses = [c["type"] for c in collab if "type" in c]
            # Heuristic: classify repositories and external clients from collab types
            repositories = [
                t for t in uses
                if any(suffix in t for suffix in ("Repository", "Repo", "Dao", "DAO"))
            ]
            clients = [c for c in entry.get("externalClients", [])]
            normalised[fqcn] = {
                "uses": uses,
                "implements": entry.get("implements", []),
                "extends": entry.get("extends"),
                "injects": injects,
                "repositories": repositories,
                "clients": clients,
                "exceptions": [],   # exceptions not in schema-format; populated by source enricher
            }
    else:
        # ── Legacy flat format ─────────────────────────────────────────────────
        raw = dg.get("classes", {})
        for fqcn, data in raw.items():
            normalised[fqcn] = {
                "uses": data.get("uses", []),
                "implements": data.get("implements", []),
                "extends": data.get("extends"),
                "injects": data.get("injects", []),
                "repositories": data.get("repositories", []),
                "clients": data.get("clients", []),
                "exceptions": data.get("exceptions", []),
            }

    return {
        "version": VERSION,
        "generatedAt": _now(),
        "count": len(normalised),
        "classes": normalised,
    }


def build_annotations_index(
    contracts_dir: Path,
    classification_path: Path,
) -> dict:
    """Map FQCN (and FQCN#method) → annotations list.

    Sources (in priority order):
    1. symbol-contracts/<fqcn>.json (from bytecode + source enricher)
    2. classification-index.json (framework annotations detected by classifier)
    """
    by_class: dict[str, list] = {}
    by_method: dict[str, list] = {}

    # Source 1: contracts (most authoritative — bytecode + source enricher)
    for cp in sorted(contracts_dir.glob("*.json")):
        try:
            c = load_json(cp)
        except Exception:
            continue
        fqcn = c.get("fqcn")
        if not fqcn:
            continue
        class_anns = list(c.get("annotations", []))
        if class_anns:
            by_class[fqcn] = sorted(set(class_anns))
        # Method-level annotations from method descriptors
        for m in c.get("methods", []):
            m_anns = m.get("annotations", [])
            if m_anns:
                key = f"{fqcn}#{m.get('name', '?')}"
                by_method.setdefault(key, [])
                by_method[key] = sorted(set(by_method[key] + m_anns))

    # Source 2: classification-index (adds framework tags not in bytecode contracts)
    ci = _load_safe(classification_path, {"classes": []})
    for entry in ci.get("classes", []):
        fqcn = entry.get("fqcn") or entry.get("class")
        if not fqcn:
            continue
        fw_anns = entry.get("annotations", []) or entry.get("frameworkAnnotations", [])
        if fw_anns:
            existing = by_class.get(fqcn, [])
            merged = sorted(set(existing + fw_anns))
            by_class[fqcn] = merged

    return {
        "version": VERSION,
        "generatedAt": _now(),
        "classCount": len(by_class),
        "methodCount": len(by_method),
        "classes": by_class,
        "methods": by_method,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprint / incremental helpers
# ─────────────────────────────────────────────────────────────────────────────

def _contracts_fingerprint(contracts_dir: Path) -> dict[str, str]:
    """SHA-256 per contract file — used to detect staleness."""
    return {
        cp.name: sha256_file(cp)
        for cp in sorted(contracts_dir.glob("*.json"))
    }


def _index_is_fresh(index_path: Path, fingerprints: dict[str, str]) -> bool:
    """Return True if the index exists and was built with the same fingerprints."""
    if not index_path.exists():
        return False
    try:
        idx = load_json(index_path)
        return idx.get("_fingerprints") == fingerprints and idx.get("version") == VERSION
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Manifest writer
# ─────────────────────────────────────────────────────────────────────────────

def write_contracts_manifest(contracts_dir: Path, state_dir: Path) -> None:
    """Write/update state/symbol-contracts.json as a manifest (index of FQCNs).

    This replaces the legacy empty global file with a useful lookup table.
    Individual per-FQCN files remain the authoritative source.
    """
    manifest: list[dict] = []
    for cp in sorted(contracts_dir.glob("*.json")):
        try:
            c = load_json(cp)
            fqcn = c.get("fqcn")
            if fqcn:
                manifest.append({
                    "fqcn": fqcn,
                    "kind": c.get("kind", "class"),
                    "file": cp.name,
                    "instantiation": c.get("instantiation", {}).get("strategy", "unknown"),
                })
        except Exception:
            continue
    atomic_write_json(
        state_dir / "symbol-contracts.json",
        {
            "schemaVersion": 1,
            "generatedAt": _now(),
            "note": "Manifest of per-FQCN contracts in symbol-contracts/. Agents load individual files by FQCN, not this manifest.",
            "count": len(manifest),
            "contracts": manifest,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build state/index/ from existing state artifacts (Phase 1)."
    )
    ap.add_argument("--out", required=True, help="State directory (contains symbol-contracts/, etc.)")
    ap.add_argument("--full", action="store_true", help="Force full re-index even if fingerprints match")
    args = ap.parse_args()

    state_dir = Path(args.out).resolve()
    index_dir = state_dir / "index"
    index_dir.mkdir(parents=True, exist_ok=True)

    contracts_dir = state_dir / "symbol-contracts"
    if not contracts_dir.exists():
        print(f"[WARN] No contracts dir found at {contracts_dir}. Creating empty index.", file=sys.stderr)
        contracts_dir.mkdir(parents=True, exist_ok=True)

    whitelist_path = state_dir / "import-whitelist.json"
    dep_graph_path = state_dir / "dependency-graph.json"
    classification_path = state_dir / "classification-index.json"

    # Compute fingerprints for incremental detection
    fingerprints = _contracts_fingerprint(contracts_dir)

    # ── classes.json ──────────────────────────────────────────────────────────
    classes_path = index_dir / "classes.json"
    if args.full or not _index_is_fresh(classes_path, fingerprints):
        data = build_classes_index(contracts_dir)
        data["$schema"] = _INDEX_SCHEMA_REF + "#/definitions/classesFile"
        data["_fingerprints"] = fingerprints
        atomic_write_json(classes_path, data)
        print(f"[OK] classes.json  — {data['count']} FQCNs")
    else:
        print(f"[SKIP] classes.json up-to-date")

    # ── methods.json ──────────────────────────────────────────────────────────
    methods_path = index_dir / "methods.json"
    if args.full or not _index_is_fresh(methods_path, fingerprints):
        data = build_methods_index(contracts_dir)
        data["$schema"] = _INDEX_SCHEMA_REF + "#/definitions/methodsFile"
        data["_fingerprints"] = fingerprints
        atomic_write_json(methods_path, data)
        print(f"[OK] methods.json  — {data['count']} method entries")
    else:
        print(f"[SKIP] methods.json up-to-date")

    # ── imports.json ──────────────────────────────────────────────────────────
    imports_path = index_dir / "imports.json"
    wl_fp = {whitelist_path.name: sha256_file(whitelist_path)} if whitelist_path.exists() else {}
    if args.full or not _index_is_fresh(imports_path, wl_fp):
        data = build_imports_index(whitelist_path)
        data["$schema"] = _INDEX_SCHEMA_REF + "#/definitions/importsFile"
        data["_fingerprints"] = wl_fp
        atomic_write_json(imports_path, data)
        print(f"[OK] imports.json  — {data['packageCount']} packages, {data['classCount']} classes")
    else:
        print(f"[SKIP] imports.json up-to-date")

    # ── dependencies.json ─────────────────────────────────────────────────────
    dependencies_path = index_dir / "dependencies.json"
    dg_fp = {dep_graph_path.name: sha256_file(dep_graph_path)} if dep_graph_path.exists() else {}
    if args.full or not _index_is_fresh(dependencies_path, dg_fp):
        data = build_dependencies_index(dep_graph_path)
        data["$schema"] = _INDEX_SCHEMA_REF + "#/definitions/dependenciesFile"
        data["_fingerprints"] = dg_fp
        atomic_write_json(dependencies_path, data)
        print(f"[OK] dependencies.json — {data['count']} classes")
    else:
        print(f"[SKIP] dependencies.json up-to-date")

    # ── annotations.json ──────────────────────────────────────────────────────
    annotations_path = index_dir / "annotations.json"
    ann_fp = {**fingerprints}
    if classification_path.exists():
        ann_fp[classification_path.name] = sha256_file(classification_path)
    if args.full or not _index_is_fresh(annotations_path, ann_fp):
        data = build_annotations_index(contracts_dir, classification_path)
        data["$schema"] = _INDEX_SCHEMA_REF + "#/definitions/annotationsFile"
        data["_fingerprints"] = ann_fp
        atomic_write_json(annotations_path, data)
        print(f"[OK] annotations.json — {data['classCount']} classes, {data['methodCount']} methods")
    else:
        print(f"[SKIP] annotations.json up-to-date")

    # ── contracts manifest ────────────────────────────────────────────────────
    write_contracts_manifest(contracts_dir, state_dir)
    print(f"[OK] symbol-contracts.json manifest updated")

    print(f"\n[DONE] Semantic index written to {index_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
