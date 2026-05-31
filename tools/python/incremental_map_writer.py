"""incremental_map_writer.py — Phase 3: compute incremental execution scope.

Propagates git changes to a typed scope that narrows compilation + JaCoCo runs:

    changedFiles → affectedClasses → affectedTests → coverageDeltaScope

Algorithm (all deterministic — no LLM):
1. changedFiles  = git diff --name-only <since>..HEAD filtered to *.java|pom.xml|*.gradle
2. affectedClasses = FQCNs of changedFiles + transitive reverse-dependencies from
                     state/index/dependencies.json (limited depth to avoid explosion)
3. affectedTests = test FQCNs whose source imports any FQCN in affectedClasses
                   (checked against state/index/imports.json or source scan fallback)
4. coverageDeltaScope = FQCNs from state/coverage-targets.json ∩ affectedClasses

Output: state/incremental-map.json (atomic write)

Anti-patterns prevented:
- Never triggers full pipeline from VS Code on single-file change.
- Never recomputes dependency graph from scratch (uses state/index/dependencies.json).
- Never loads JaCoCo XML globally (scoped to coverageDeltaScope).
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from common import atomic_write_json, load_json, sha256_file

# Max transitive depth for reverse-dependency propagation.
# Prevents explosion in highly-connected codebases.
MAX_REVERSE_DEPTH = 3


# ─────────────────────────────────────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────────────────────────────────────

def _git_changed_files(repo: Path, since: str) -> list[str]:
    """Return list of changed file paths relative to repo root."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{since}..HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"[WARN] git diff failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _git_head_sha(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _filter_relevant(files: list[str]) -> list[str]:
    return [f for f in files if re.search(r"\.(java|xml|gradle|kts)$", f)]


# ─────────────────────────────────────────────────────────────────────────────
# FQCN helpers
# ─────────────────────────────────────────────────────────────────────────────

_PACKAGE_RE = re.compile(r"\bpackage\s+([\w\.]+)\s*;")
_CLASS_RE = re.compile(r"\b(?:class|interface|enum|record)\s+(\w+)")
_IMPORT_RE = re.compile(r"^import\s+(?:static\s+)?([\w\.]+)\s*;", re.MULTILINE)

_SRC_ROOTS = ("src/main/java", "src/test/java", "target/generated-sources")


def _fqcn_from_java_file(repo: Path, rel_path: str) -> str | None:
    """Derive FQCN from a .java file path using package + class name."""
    full = repo / rel_path
    if not full.exists():
        return None
    try:
        txt = full.read_text(encoding="utf-8", errors="ignore")
        pkg_m = _PACKAGE_RE.search(txt)
        cls_m = _CLASS_RE.search(txt)
        if not cls_m:
            return None
        pkg = pkg_m.group(1) if pkg_m else ""
        return f"{pkg}.{cls_m.group(1)}" if pkg else cls_m.group(1)
    except Exception:
        return None


def _imports_of_java_file(repo: Path, rel_path: str) -> list[str]:
    """Extract all imported FQCNs from a .java file."""
    full = repo / rel_path
    if not full.exists():
        return []
    try:
        txt = full.read_text(encoding="utf-8", errors="ignore")
        return [m.group(1).rstrip("*").rstrip(".") for m in _IMPORT_RE.finditer(txt)]
    except Exception:
        return []


def _is_test_file(rel_path: str) -> bool:
    return "src/test/" in rel_path or rel_path.endswith("Test.java") or rel_path.endswith("Tests.java")


# ─────────────────────────────────────────────────────────────────────────────
# Dependency propagation
# ─────────────────────────────────────────────────────────────────────────────

def _build_reverse_index(dep_index: dict) -> dict[str, set[str]]:
    """Build FQCN → set(FQCNs that depend on it) reverse map."""
    reverse: dict[str, set[str]] = {}
    for fqcn, data in dep_index.items():
        all_deps: list[str] = (
            data.get("uses", [])
            + data.get("injects", [])
            + data.get("repositories", [])
            + data.get("clients", [])
        )
        if data.get("extends"):
            all_deps.append(data["extends"])
        all_deps += data.get("implements", [])
        for dep in all_deps:
            if dep:
                reverse.setdefault(dep, set()).add(fqcn)
    return reverse


def _propagate_affected(
    seed_fqcns: set[str],
    reverse_index: dict[str, set[str]],
    max_depth: int,
) -> set[str]:
    """BFS over reverse-dependency graph to find all transitively affected classes."""
    visited: set[str] = set(seed_fqcns)
    frontier = set(seed_fqcns)
    for _ in range(max_depth):
        next_frontier: set[str] = set()
        for fqcn in frontier:
            for dependent in reverse_index.get(fqcn, set()):
                if dependent not in visited:
                    visited.add(dependent)
                    next_frontier.add(dependent)
        if not next_frontier:
            break
        frontier = next_frontier
    return visited


# ─────────────────────────────────────────────────────────────────────────────
# Affected tests detection
# ─────────────────────────────────────────────────────────────────────────────

def _find_affected_tests(
    repo: Path,
    module: str | None,
    affected_fqcns: set[str],
    dep_index: dict,
) -> list[str]:
    """Find test classes that import or depend on any affected FQCN."""
    affected_tests: set[str] = set()

    # Strategy 1: scan test source files for imports of affected FQCNs
    scan_roots: list[Path] = []
    if module:
        mod_path = repo / module
        scan_roots = [mod_path / "src" / "test" / "java"]
    else:
        scan_roots = sorted(repo.rglob("src/test/java"))[:20]  # cap for large mono-repos

    for root in scan_roots:
        if not root.exists():
            continue
        for jf in sorted(root.rglob("*.java")):
            rel = str(jf.relative_to(repo))
            imports = _imports_of_java_file(repo, rel)
            # Check if any import matches an affected FQCN
            if any(
                any(imp == aff or imp.startswith(aff + ".") for imp in imports)
                for aff in affected_fqcns
            ):
                fqcn = _fqcn_from_java_file(repo, rel)
                if fqcn:
                    affected_tests.add(fqcn)

    # Strategy 2: test classes already in dep_index that depend on affected FQCNs
    for fqcn, data in dep_index.items():
        if not (fqcn.endswith("Test") or fqcn.endswith("Tests") or "test" in fqcn.lower()):
            continue
        all_deps = (
            data.get("uses", []) + data.get("injects", [])
            + ([data["extends"]] if data.get("extends") else [])
            + data.get("implements", [])
        )
        if any(d in affected_fqcns for d in all_deps):
            affected_tests.add(fqcn)

    return sorted(affected_tests)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compute incremental execution scope from git diff (Phase 3)."
    )
    ap.add_argument("--repo", required=True, help="Repository root")
    ap.add_argument("--out", required=True, help="State directory")
    ap.add_argument("--since", required=True,
                    help="Git ref to diff from (e.g. HEAD~1, main, abc1234)")
    ap.add_argument("--module", default=None, help="Restrict scope to one Maven module")
    ap.add_argument("--max-depth", type=int, default=MAX_REVERSE_DEPTH,
                    help=f"Max reverse-dependency traversal depth (default: {MAX_REVERSE_DEPTH})")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state_dir = Path(args.out).resolve()
    out_path = state_dir / "incremental-map.json"

    # ── 1. Get changed files from git ─────────────────────────────────────────
    head_sha = _git_head_sha(repo)
    all_changed = _git_changed_files(repo, args.since)
    relevant = _filter_relevant(all_changed)

    print(f"[INFO] {len(all_changed)} total changed, {len(relevant)} relevant (java/pom/gradle)")

    # ── 2. Derive FQCNs for changed .java files ───────────────────────────────
    seed_fqcns: set[str] = set()
    affected_modules: set[str] = set()

    for rel in relevant:
        if rel.endswith(".java"):
            fqcn = _fqcn_from_java_file(repo, rel)
            if fqcn:
                seed_fqcns.add(fqcn)
        # Track affected modules (first path component if multi-module)
        parts = rel.split("/")
        if len(parts) > 1 and not parts[0].startswith("."):
            affected_modules.add(parts[0])

    # If module filter is active, restrict to that module's changes
    if args.module:
        affected_modules = {args.module} & affected_modules if affected_modules else {args.module}

    # ── 3. Load dependency index and propagate ────────────────────────────────
    dep_index_path = state_dir / "index" / "dependencies.json"
    dep_data: dict = {}
    if dep_index_path.exists():
        try:
            dep_data = load_json(dep_index_path).get("classes", {})
        except Exception as e:
            print(f"[WARN] could not load dependencies.json: {e}", file=sys.stderr)
    else:
        # Fallback: try legacy dependency-graph.json
        legacy = state_dir / "dependency-graph.json"
        if legacy.exists():
            try:
                dep_data = load_json(legacy).get("classes", {})
                print("[INFO] Using legacy dependency-graph.json (index not found)")
            except Exception:
                pass

    reverse_index = _build_reverse_index(dep_data)
    affected_classes = _propagate_affected(seed_fqcns, reverse_index, args.max_depth)

    print(f"[INFO] {len(seed_fqcns)} seed FQCNs → {len(affected_classes)} affected classes "
          f"(depth≤{args.max_depth})")

    # ── 4. Find affected tests ────────────────────────────────────────────────
    affected_tests = _find_affected_tests(repo, args.module, affected_classes, dep_data)
    print(f"[INFO] {len(affected_tests)} affected tests")

    # ── 5. Compute coverage delta scope ──────────────────────────────────────
    coverage_delta_scope: list[str] = []
    cov_targets_path = state_dir / "coverage-targets.json"
    if cov_targets_path.exists():
        try:
            targets = load_json(cov_targets_path)
            target_list = targets if isinstance(targets, list) else targets.get("targets", [])
            coverage_delta_scope = [
                t["fqcn"] for t in target_list
                if t.get("fqcn") in affected_classes
            ]
        except Exception as e:
            print(f"[WARN] could not load coverage-targets.json: {e}", file=sys.stderr)

    # ── 6. Compute fingerprint of this run ────────────────────────────────────
    fingerprint_sources = [head_sha or "unknown", args.since]
    for rel in sorted(relevant):
        full = repo / rel
        if full.exists():
            fingerprint_sources.append(sha256_file(full)[:8])
    run_fingerprint = hashlib.sha256("|".join(fingerprint_sources).encode()).hexdigest()[:16]

    # ── 7. Write incremental-map.json ─────────────────────────────────────────
    atomic_write_json(out_path, {
        "$schemaRef": "state/_schemas/incremental-map.schema.json",
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "since": {
            "git": args.since,
            "head": head_sha,
            "fingerprint": run_fingerprint,
        },
        "changedFiles": sorted(relevant),
        "affectedClasses": sorted(affected_classes),
        "affectedTests": affected_tests,
        "affectedModules": sorted(affected_modules),
        "coverageDeltaScope": sorted(coverage_delta_scope),
        "_meta": {
            "totalChangedFiles": len(all_changed),
            "seedFqcns": sorted(seed_fqcns),
            "reverseDepth": args.max_depth,
            "depIndexSource": str(dep_index_path) if dep_index_path.exists() else "legacy",
        },
    })

    print(f"[OK] incremental-map.json → {len(affected_classes)} classes, "
          f"{len(affected_tests)} tests, {len(coverage_delta_scope)} coverage targets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
