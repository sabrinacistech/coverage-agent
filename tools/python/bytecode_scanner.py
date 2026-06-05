"""bytecode_scanner.py — produce state/symbol-contracts/<fqcn>.json from .class bytecode.

Uses `javap -p -s` (private + signatures). For each target FQCN under target/classes,
emit a symbol contract with constructors and methods (with `evidence-id`).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from common import (
    atomic_write_json,
    find_tool,
    load_json,
    resolve_target_dirs,
    run,
    validate,
)

# Reuse the classifier's exclusion matcher (single source of truth) so the
# scanner skips the SAME generated FQCNs the planner would later exclude —
# instead of emitting contracts for CXF/wsdl2java, JAXB or OpenAPI classes.
from classification_analyzer import (  # noqa: E402
    _build_exclusion_matchers,
    _is_excluded,
)

DESC_RE = re.compile(r"descriptor:\s*(\S+)")
ACCESS_RE = re.compile(
    r"^\s*(public|protected|private|default)?\s*"
    r"((?:static|final|abstract|synchronized|native|strictfp|transient|volatile)\s*)*"
    r"(?P<rest>[^;{]+);?$"
)
# Matches the class/interface/enum declaration line emitted by `javap`.
# We search for this in the full output because the first non-empty line is
# usually `Compiled from "Foo.java"`, which does not declare the type.
TYPE_DECL_RE = re.compile(r"\b(class|interface|enum)\s+([\w\.$]+)")


def _eid(prefix: str, key: str) -> str:
    return f"{prefix}:{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def _parse_desc_params(desc: str) -> list[str]:
    """Parse JVM method descriptor parameters to FQCN-like types."""
    # crude FQCN extraction from JVM desc; not 1:1 with generics but sufficient
    params_section = desc.split(")")[0][1:]
    out: list[str] = []
    i = 0
    while i < len(params_section):
        c = params_section[i]
        arr = ""
        while c == "[":
            arr += "[]"
            i += 1
            c = params_section[i]
        if c == "L":
            end = params_section.index(";", i)
            fq = params_section[i + 1 : end].replace("/", ".")
            out.append(fq + arr)
            i = end + 1
        else:
            primitives = {
                "B": "byte", "C": "char", "D": "double", "F": "float",
                "I": "int", "J": "long", "S": "short", "Z": "boolean", "V": "void",
            }
            out.append(primitives[c] + arr)
            i += 1
    return out


def _parse_desc_return(desc: str) -> str:
    ret = desc.split(")")[1]
    return _parse_desc_params("(" + ret + ")V")[0]  # reuse with a dummy method


def scan_class(class_file: Path, javap: str) -> dict | None:
    r = run([javap, "-p", "-s", str(class_file)])
    if r.returncode != 0:
        return None
    lines = r.stdout.splitlines()
    # `javap` typically prints `Compiled from "Foo.java"` first, then the type
    # declaration line. Skip ahead to the first line that actually declares a
    # class / interface / enum — taking the first non-empty line breaks here.
    header = ""
    header_idx = -1
    for idx, line in enumerate(lines):
        if TYPE_DECL_RE.search(line):
            header = line
            header_idx = idx
            break
    if not header:
        return None
    kind = "class"
    if " interface " in f" {header} ":
        kind = "interface"
    elif "abstract class " in header:
        kind = "abstract"
    elif " enum " in f" {header} ":
        kind = "enum"
    m = TYPE_DECL_RE.search(header)
    if not m:
        return None
    fqcn = m.group(2)
    # Skip past the header so member parsing starts on the next line.
    member_start = header_idx + 1

    constructors: list[dict] = []
    methods: list[dict] = []
    i = member_start
    while i < len(lines):
        line = lines[i].strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        # Member line + descriptor line
        desc_match = DESC_RE.match(nxt)
        if desc_match:
            desc = desc_match.group(1)
            decl = line.rstrip(";")
            # Constructor: "<FQCN>(...)"
            is_ctor = decl.endswith(")") and (
                decl.startswith("public " + fqcn) or
                decl.startswith("private " + fqcn) or
                decl.startswith("protected " + fqcn) or
                decl.startswith(fqcn)
            )
            visibility = "public"
            for v in ("public", "protected", "private"):
                if decl.startswith(v + " "):
                    visibility = v
                    break
            # parse generic params (raw types) from descriptor
            params = [{"type": t} for t in _parse_desc_params(desc)] if "(" in desc else []
            if is_ctor:
                key = f"{fqcn}({','.join(t['type'] for t in params)})"
                constructors.append(
                    {
                        "evidenceId": _eid(f"ctor:{fqcn}", key),
                        "visibility": visibility,
                        "params": params,
                        "throws": [],
                        "source": f"bytecode:{class_file}",
                    }
                )
            else:
                # method name: token immediately before '('
                name_m = re.search(r"(\w+)\s*\(", decl)
                if not name_m:
                    i += 2
                    continue
                name = name_m.group(1)
                ret = _parse_desc_return(desc)
                modifiers = [
                    m for m in ("public", "protected", "private", "static", "final", "abstract", "synchronized", "native")
                    if (" " + m + " ") in (" " + decl + " ")
                ]
                key = f"{fqcn}#{name}({','.join(t['type'] for t in params)})"
                methods.append(
                    {
                        "evidenceId": _eid(f"sym:{fqcn}#{name}", key),
                        "name": name,
                        "modifiers": modifiers,
                        "returnType": ret,
                        "params": params,
                        "throws": [],
                        "generics": {"typeParams": [], "signature": None},
                        "usable": "synthetic" not in modifiers and name != "<clinit>",
                        "source": "bytecode",
                    }
                )
            i += 2
        else:
            i += 1

    instantiation_allowed = kind == "class" and any(
        c["visibility"] == "public" for c in constructors
    )
    out = {
        "schemaVersion": 1,
        "fqcn": fqcn,
        "kind": kind,
        "modifiers": [],
        "annotations": [],
        "instantiation": {
            "allowed": instantiation_allowed,
            "strategy": "constructor" if instantiation_allowed else "mock",
            "preferred": (constructors[0]["evidenceId"] if instantiation_allowed and constructors else None),
            "fallbacks": [],
        },
        "constructors": constructors,
        "methods": methods,
        "builders": [],
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--module",
        default=None,
        help=(
            "Module dir name. Omit (or pass '.') for monolithic repos where "
            "target/classes lives at the repo root."
        ),
    )
    ap.add_argument(
        "--include",
        default=".*",
        help="Regex to filter FQCNs (e.g. '^com\\.acme\\.')",
    )
    ap.add_argument(
        "--fqcn",
        action="append",
        default=None,
        metavar="FQCN",
        help=(
            "P3.a: restrict the scan to one or more exact FQCNs. May be "
            "supplied multiple times. Intersects with --include (a class is "
            "scanned only if it matches the regex AND appears in this set)."
        ),
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state_dir = Path(args.out).resolve()

    classes_dirs = resolve_target_dirs(repo, args.module)
    if not classes_dirs:
        label = args.module or "<root>"
        print(
            f"[FAIL] no target/classes found under {repo} (module={label}). "
            "Run `mvn -DskipTests package` first.",
            file=sys.stderr,
        )
        return 2

    javap = find_tool("javap")
    include = re.compile(args.include)
    fqcn_whitelist: set[str] | None = set(args.fqcn) if args.fqcn else None

    # Generated-code exclusion (audit fix): the architecture must NOT create test
    # contracts for generated classes (CXF/wsdl2java, JAXB, OpenAPI). They are not
    # unit-test SUTs and their large generated APIs can even break the contract
    # schema (e.g. a DTO with >80 methods aborts validate-contracts). Read the same
    # generated-code-index.json the classifier/planner use and skip those FQCNs.
    gen_index_path = state_dir / "generated-code-index.json"
    gen_index = load_json(gen_index_path) if gen_index_path.exists() else {}
    excluded_fqcns, excluded_pkg_patterns = _build_exclusion_matchers(gen_index)

    out_dir = state_dir / "symbol-contracts"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect class file candidates in deterministic order (post-audit
    # 2026-05-28). Filtering happens before the parallel scan to avoid spawning
    # javap on synthetic/nested classes.
    candidates: list[Path] = []
    for classes_dir in classes_dirs:
        for cf in sorted(classes_dir.rglob("*.class")):
            if "$" in cf.name:
                continue
            candidates.append(cf)

    # Parallel javap invocations — each is an independent subprocess; cap the
    # pool at min(cpu_count, 8) to avoid oversubscribing the JVM launcher.
    # Order is preserved by ThreadPoolExecutor.map, so dedup-by-first stays
    # deterministic.
    worker_count = min(max(1, os.cpu_count() or 1), 8)

    def _scan_pair(cf: Path) -> tuple[Path, dict | None]:
        return (cf, scan_class(cf, javap))

    scanned: list[tuple[Path, dict | None]]
    if not candidates:
        scanned = []
    elif worker_count == 1 or len(candidates) <= 2:
        scanned = [_scan_pair(cf) for cf in candidates]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            scanned = list(pool.map(_scan_pair, candidates))

    n = 0
    skipped_generated = 0
    written: list[dict] = []
    seen_fqcns: set[str] = set()
    for cf, contract in scanned:
        if not contract:
            continue
        if not include.search(contract["fqcn"]):
            continue
        if _is_excluded(contract["fqcn"], excluded_fqcns, excluded_pkg_patterns):
            # Generated code (CXF/wsdl2java, JAXB, OpenAPI): never a unit-test SUT.
            skipped_generated += 1
            continue
        if fqcn_whitelist is not None and contract["fqcn"] not in fqcn_whitelist:
            continue
        if contract["fqcn"] in seen_fqcns:
            # Same class compiled into multiple modules — keep the first.
            continue
        seen_fqcns.add(contract["fqcn"])
        try:
            validate("symbol-contract", contract)
        except Exception as e:
            print(f"[WARN] schema failed for {contract['fqcn']}: {e}", file=sys.stderr)
        atomic_write_json(out_dir / f"{contract['fqcn']}.json", contract)
        written.append({
            "fqcn": contract["fqcn"],
            "kind": contract.get("kind", "class"),
            "file": f"{contract['fqcn']}.json",
            "instantiation": contract.get("instantiation", {}).get("strategy", "unknown"),
        })
        n += 1

    # Write/update the manifest (state/symbol-contracts.json) so it reflects
    # the per-FQCN files just written. Agents load individual files by FQCN;
    # the manifest is an index for quick lookup and freshness checks.
    manifest_path = state_dir / "symbol-contracts.json"
    # Merge with any existing entries from other modules
    existing_manifest: list[dict] = []
    if manifest_path.exists():
        try:
            existing_manifest = load_json(manifest_path).get("contracts", [])
            # Remove entries for FQCNs we just re-scanned (they'll be re-added)
            scanned_fqcns = {e["fqcn"] for e in written}
            existing_manifest = [e for e in existing_manifest if e["fqcn"] not in scanned_fqcns]
        except Exception:
            existing_manifest = []
    all_entries = existing_manifest + written
    atomic_write_json(manifest_path, {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "note": "Manifest index of per-FQCN contracts in symbol-contracts/. "
                "Agents load individual files by FQCN, not this manifest.",
        "count": len(all_entries),
        "contracts": sorted(all_entries, key=lambda e: e["fqcn"]),
    })
    if skipped_generated:
        print(
            f"[OK] skipped {skipped_generated} generated class(es) "
            "(generated-code-index.json) — not unit-test SUTs"
        )
    print(f"[OK] {n} contracts -> {out_dir}")
    print(f"[OK] manifest -> {manifest_path} ({len(all_entries)} total entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
