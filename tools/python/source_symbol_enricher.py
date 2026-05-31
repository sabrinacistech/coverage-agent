"""source_symbol_enricher.py — enrich bytecode contracts with source-only semantics.

Purpose
-------
`javap` is the source of truth for compiled constructors and methods, but it cannot
reliably answer some generation questions that matter for test generation:

* whether a type was declared as a FreeBuilder interface in source;
* whether the source declares a nested `class Builder extends X_Builder`;
* which FreeBuilder/hand-written builder setter names are legal;
* whether the safe fixture strategy is builder, mock, factory or none.

This script is intentionally conservative. It only adds information that can be
seen in source or generated sources. If a symbol cannot be proven, it is not added.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from common import atomic_write_json, validate

PACKAGE_RE = re.compile(r"\bpackage\s+([\w\.]+)\s*;")
TYPE_RE = re.compile(r"(?P<ann>(?:@[\w\.]+(?:\([^)]*\))?\s*)*)\b(?P<mods>(?:public|protected|private|abstract|static|final|sealed|non-sealed|strictfp|\s)+)?\b(?P<kind>interface|class|abstract\s+class|enum|record)\s+(?P<name>\w+)")
FREEBUILDER_RE = re.compile(r"@(?:[\w\.]+\.)?FreeBuilder\b")
BUILDER_DECL_RE = re.compile(r"\b(?:public\s+)?(?:static\s+)?class\s+Builder\s+extends\s+([\w\.]+_Builder)\b")
# FreeBuilder abstract setter shape inside generated/source builder: Builder field(Type value);
SETTER_RE = re.compile(r"\b(?:public\s+|abstract\s+|final\s+|\s)*(?:Builder|[\w\.]+\.Builder)\s+(?P<name>\w+)\s*\(\s*(?P<type>[\w\.<>\[\], ?]+)\s+\w+\s*\)\s*;")
# Manual builder fluent setters returning Builder.
MANUAL_SETTER_RE = re.compile(r"\b(?:public\s+)?(?:Builder|[\w\.]+\.Builder)\s+(?P<name>\w+)\s*\(\s*(?P<type>[\w\.<>\[\], ?]+)\s+\w+\s*\)")


def eid(prefix: str, key: str) -> str:
    return f"{prefix}:{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_contract(path: Path, contract: dict) -> None:
    try:
        validate("symbol-contract", contract)
    except Exception as exc:  # keep warning behavior; schema may lag new optional fields
        print(f"[WARN] schema failed for {contract.get('fqcn')}: {exc}")
    atomic_write_json(path, contract)


def iter_java_files(module: Path):
    for rel in ("src/main/java", "target/generated-sources", "target/generated-test-sources"):
        base = module / rel
        if base.exists():
            yield from base.rglob("*.java")


def index_sources(repo: Path, module: str | None) -> dict[str, dict]:
    if module:
        modules = [repo / module]
    else:
        modules = sorted((p for p in repo.iterdir() if p.is_dir()), key=lambda p: p.name) + [repo]
    out: dict[str, dict] = {}
    for mod in modules:
        if not mod.exists():
            continue
        for jf in sorted(iter_java_files(mod)):
            txt = jf.read_text(encoding="utf-8", errors="ignore")
            pkg_m = PACKAGE_RE.search(txt)
            pkg = pkg_m.group(1) if pkg_m else ""
            for tm in TYPE_RE.finditer(txt):
                name = tm.group("name")
                fqcn = f"{pkg}.{name}" if pkg else name
                annotations = re.findall(r"@([\w\.]+)", tm.group("ann") or "")
                out[fqcn] = {
                    "path": str(jf),
                    "text": txt,
                    "kind_decl": tm.group("kind").replace("abstract class", "abstract"),
                    "annotations": annotations,
                    "is_freebuilder": bool(FREEBUILDER_RE.search(tm.group("ann") or "")),
                    "has_declared_builder": bool(BUILDER_DECL_RE.search(txt)),
                }
    return out


def collect_builder_setters(fqcn: str, src: dict, source_index: dict[str, dict]) -> list[dict]:
    txt = src["text"]
    setters = []
    seen = set()
    for rx in (SETTER_RE, MANUAL_SETTER_RE):
        for m in rx.finditer(txt):
            name = m.group("name")
            if name in {"build", "clear", "mergeFrom"} or name.startswith("map"):
                continue
            typ = " ".join(m.group("type").split())
            key = (name, typ)
            if key in seen:
                continue
            seen.add(key)
            setters.append({"name": name, "type": typ, "required": True})
    return setters


def enrich_contract(contract_path: Path, src: dict, source_index: dict[str, dict]) -> bool:
    c = read_json(contract_path)
    changed = False
    annotations = set(c.get("annotations", []))
    for ann in src.get("annotations", []):
        if ann not in annotations:
            annotations.add(ann)
            changed = True
    c["annotations"] = sorted(annotations)

    fqcn = c["fqcn"]
    simple = fqcn.rsplit(".", 1)[-1]
    is_freebuilder = src.get("is_freebuilder") or any(a.endswith("FreeBuilder") for a in annotations)

    if is_freebuilder:
        changed = True
        c["kind"] = "interface" if src.get("kind_decl") == "interface" else c.get("kind", "interface")
        setters = collect_builder_setters(fqcn, src, source_index)
        builder_exists = src.get("has_declared_builder")
        if builder_exists:
            builder = {
                # evidenceId must match the canonical strict grammar
                # `builder:<fqcn>:<8hex>` (symbol-contract.schema.json#/definitions/evidenceId);
                # the builder kind lives in the `kind` field, NOT in the id string. The
                # hash key still folds in the kind so distinct builder kinds never collide.
                "evidenceId": eid(f"builder:{fqcn}", f"builder:{fqcn}:freebuilder"),
                "kind": "freebuilder",
                "entry": f"new {simple}.Builder()",
                "build": "build()",
                "setters": setters,
                "source": f"source:{src['path']}",
            }
            # Replace previous freebuilder entry for this type.
            existing = [b for b in c.get("builders", []) if not (b.get("kind") == "freebuilder" and b.get("entry", "").endswith(".Builder()"))]
            existing.append(builder)
            c["builders"] = existing
            c["instantiation"] = {
                "allowed": True,
                "strategy": "builder",
                "preferred": builder["evidenceId"],
                "fallbacks": [],
                "reason": "@FreeBuilder interface with declared nested Builder verified in source",
            }
        else:
            c["builders"] = [b for b in c.get("builders", []) if b.get("kind") != "freebuilder"]
            c["instantiation"] = {
                "allowed": True,
                "strategy": "mock",
                "preferred": None,
                "fallbacks": [],
                "reason": "@FreeBuilder type without declared nested Builder; use Mockito mock only for passive collaborators",
            }
    write_contract(contract_path, c)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True, help="state dir containing symbol-contracts")
    ap.add_argument("--module", default=None)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state = Path(args.out).resolve()
    contracts = state / "symbol-contracts"
    if not contracts.exists():
        print(f"[WARN] contracts dir not found: {contracts}")
        return 0

    idx = index_sources(repo, args.module)
    n = 0
    for cp in contracts.glob("*.json"):
        try:
            fqcn = read_json(cp).get("fqcn")
        except Exception:
            continue
        src = idx.get(fqcn)
        if not src:
            continue
        if enrich_contract(cp, src, idx):
            n += 1
    print(f"[OK] enriched {n} contracts from source metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
