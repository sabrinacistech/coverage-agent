"""run_pipeline.py — orchestrate the deterministic Python pre-stage.

Runs (in order):
   1. pom_parser                → state/build-tool-contract.json
   2. archetype_detector        → state/archetype-profile.json
   3. generated_code_scanner    → state/generated-code-index.json
   4. classpath_resolver        → state/import-whitelist.json
   5. stack_profile_detector    → state/stack-profile.json
   6. bytecode_scanner          → state/symbol-contracts/<fqcn>.json  (if --module)
   7. source_symbol_enricher    → enrich contracts (FreeBuilder/Lombok source-only semantics)
   8. jacoco_parser (targets)   → state/coverage-targets.json         (if --jacoco-xml)
   9. semantic_index_writer     → state/index/{classes,methods,imports,dependencies,annotations}.json
  10. classification_analyzer   → state/classification-index.json
  11. dependency_graph_extractor→ state/dependency-graph.json
  12. fixture_catalog_builder   → state/fixture-catalog.json
  13. incremental_map_writer    → state/incremental-map.json           (if --since)
  14. coverage_planner          → state/batch-plan.json (narrowed by --incremental-only when --since was given)
  15. state_validator           → validates all state/*.json
  16. context_pack_builder      → state/context-packs/<safe_fqcn>.json (one per SUT in batch)

After this, the LLM only consumes state/context-packs/*.json.  Token consumption drops
because no agent re-parses POMs, classpath, javap output or JaCoCo XML.  The context-pack
is the single source of truth for LLM agents — raw source code is NEVER passed to them.

Phase 1 (semantic index): step 9 projects all prior state into state/index/ so agents
query a single consistent index instead of re-reading raw sources, eliminating
O(agents × files) redundant reads.

Phase 2 (graph + fixtures + plan): steps 11-13 build the dependency graph, fixture
catalog, and ranked batch plan deterministically — without LLM.

Phase 3 (incremental): step 14 computes changed/affected scope from git diff when
--since is provided; the orchestrator uses this to narrow compilation and JaCoCo runs.

Skip names for --skip flag
--------------------------
  pom            step  1
  archetype      step  2
  generated      step  3
  classpath      step  4
  stack          step  5
  bytecode       step  6  (also skipped automatically when --module is absent)
  source         step  7
  jacoco         step  8  (also skipped automatically when --jacoco-xml is absent)
  index          step  9
  classification step 10
  deps           step 11
  fixtures       step 12
  planning       step 13
  incremental    step 14  (also skipped automatically when --since is absent)
  validate       step 15
  context        step 16  (builds state/context-packs/ — the LLM's only input)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, emit_tool_summary  # noqa: E402

# ── P4.1: conservative input-hash cache ───────────────────────────────────────
# Only these steps are cacheable. Anything else always runs.
# "index" added post-audit 2026-05-28: semantic_index_writer also has its own
# fingerprint short-circuit, but caching at the orchestrator level avoids the
# Python subprocess spawn entirely when symbol-contracts/ is unchanged.
# "bytecode" and "source" added 2026-05-29: signature uses mtime+size for
# .class / .java files because full content hashing of thousands of files
# would dominate the cache-check cost.
_CACHEABLE_STEPS: frozenset[str] = frozenset({
    "stack", "bytecode", "source", "classification", "index", "planning", "context",
})


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Field names that change on every emission and would otherwise invalidate
# every downstream cache entry. Stripped recursively from JSON inputs before
# hashing (post-audit 2026-05-28).
_VOLATILE_JSON_FIELDS: frozenset[str] = frozenset({
    "generatedAt", "generated_at", "timestampUtc", "timestamp_utc",
})


def _strip_volatile(obj):
    """Recursively drop volatile fields from a parsed JSON tree."""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in _VOLATILE_JSON_FIELDS}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def _sha256_stable(p: Path) -> str:
    """Hash a file's content with volatile timestamps stripped if it's JSON.

    Falls back to the raw binary hash on non-JSON files or when parsing
    fails — that keeps the function safe for pom.xml and other inputs.
    """
    if p.suffix.lower() == ".json":
        try:
            with p.open("r", encoding="utf-8") as f:
                doc = json.load(f)
            canonical = json.dumps(
                _strip_volatile(doc),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except (json.JSONDecodeError, OSError):
            pass
    return _sha256_file(p)


def _safe_hash(parts: list[str]) -> str:
    h = hashlib.sha256()
    for s in sorted(parts):
        h.update(s.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _file_stamp(p: Path) -> str:
    """Lightweight (mtime_ns, size) stamp for cheap cache signatures.

    Used when full SHA-256 of the file would be too slow because the input
    set may contain thousands of bytecode or source files. The kernel
    updates mtime on every write, so this is reliable for detecting changes
    without paying for content hashing.
    """
    st = p.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def _step_input_signature(step: str, args, out_dir: Path) -> list[str] | None:
    """Return list of input identifiers for a cacheable step, or None if
    inputs cannot be enumerated (treat as always-miss).
    """
    parts: list[str] = [f"step={step}"]
    if step == "stack":
        repo = Path(args.repo)
        parts.append(f"repo={repo}")
        try:
            poms = [
                p for p in sorted(repo.rglob("pom.xml"))
                if "target" not in p.parts and "build" not in p.parts
            ]
        except OSError:
            return None
        for p in poms:
            try:
                parts.append(f"{p}:{_sha256_file(p)}")
            except OSError:
                return None
        # cp.txt feeds framework-version resolution (stack_profile_detector);
        # include it so a classpath change re-runs the detector even when no
        # pom.xml changed.
        try:
            for cp in sorted(repo.rglob("cp.txt")):
                if cp.parent.name == "target":
                    parts.append(f"{cp}:{_file_stamp(cp)}")
        except OSError:
            pass
        return parts
    if step == "bytecode":
        # bytecode_scanner consumes target/classes/**/*.class of the active
        # module, filtered by --include-fqcn and optionally --sut. Use
        # mtime+size stamps to keep the cache-check sub-second on big repos
        # (post-audit 2026-05-29).
        repo = Path(args.repo)
        module = args.module or "."
        classes_dir = (repo / module if module != "." else repo) / "target" / "classes"
        if not classes_dir.exists():
            return None
        parts.append(f"module={module}")
        parts.append(f"include={args.include_fqcn or '.*'}")
        parts.append(f"sut={args.sut or ''}")
        try:
            for cf in sorted(classes_dir.rglob("*.class")):
                if "$" in cf.name:
                    continue
                parts.append(f"{cf.relative_to(classes_dir)}:{_file_stamp(cf)}")
        except OSError:
            return None
        return parts
    if step == "source":
        # source_symbol_enricher reads .java sources AND rewrites the
        # symbol-contracts produced by bytecode_scanner. Because the step
        # mutates its own contract inputs, hashing the contract *contents*
        # would make the signature differ between pre-step and post-step
        # state — leading to permanent cache misses. Instead we use:
        #   - .java file stamps (source code change = re-enrich)
        #   - the *set* of contract filenames (new SUT = re-enrich)
        # Contract content changes are already covered by the bytecode
        # step's stamps, so this is sufficient (post-audit 2026-05-29).
        repo = Path(args.repo)
        module = args.module
        mod_dir = (repo / module) if module else repo
        parts.append(f"module={module or ''}")
        try:
            for rel in ("src/main/java", "target/generated-sources", "target/generated-test-sources"):
                base = mod_dir / rel
                if base.exists():
                    for jf in sorted(base.rglob("*.java")):
                        parts.append(f"{jf.relative_to(mod_dir)}:{_file_stamp(jf)}")
        except OSError:
            return None
        contracts_dir = out_dir / "symbol-contracts"
        if contracts_dir.exists():
            for p in sorted(contracts_dir.glob("*.json")):
                # Names only — see docstring above for why content is excluded.
                parts.append(f"contract-name:{p.name}")
        return parts
    if step == "classification":
        idx_dir = out_dir / "index"
        if not idx_dir.exists():
            return None
        for p in sorted(idx_dir.glob("*.json")):
            try:
                parts.append(f"{p.name}:{_sha256_stable(p)}")
            except OSError:
                return None
        return parts
    if step == "index":
        # Step 9 consumes state/symbol-contracts/*.json + import-whitelist.json.
        # NOTE: dependency-graph.json is NOT an input — it is produced by
        # step 11 (deps), which runs *after* index. Including it would make
        # the hash unstable between cold and warm runs (cold: file absent,
        # warm: file present → hashes never match). Discovered post-audit
        # while validating cache hits end-to-end (2026-05-28).
        if getattr(args, "full_index", False):
            return None
        contracts_dir = out_dir / "symbol-contracts"
        if not contracts_dir.exists():
            return None
        for p in sorted(contracts_dir.glob("*.json")):
            try:
                parts.append(f"{p.name}:{_sha256_stable(p)}")
            except OSError:
                return None
        wl = out_dir / "import-whitelist.json"
        if wl.exists():
            try:
                parts.append(f"import-whitelist.json:{_sha256_stable(wl)}")
            except OSError:
                return None
        return parts
    if step == "planning":
        parts.append(f"mode={args.coverage_mode}")
        # --plan-limit changes the plan size, so it must bust the cache: without
        # this, switching --plan-limit (e.g. 0 → 50) would HIT_CACHE and reuse the
        # stale plan, silently ignoring the new limit.
        parts.append(f"plan-limit={getattr(args, 'plan_limit', 0)}")
        for name in (
            "coverage-targets.json",
            "classification-index.json",
            "dependency-graph.json",
            "fixture-catalog.json",
            "incremental-map.json",
            "stack-profile.json",
        ):
            p = out_dir / name
            if p.exists():
                try:
                    parts.append(f"{name}:{_sha256_stable(p)}")
                except OSError:
                    return None
        return parts
    if step == "context":
        parts.append(f"sut={args.sut or ''}")
        for name in (
            "batch-plan.json",
            "stack-profile.json",
            "classification-index.json",
            "dependency-graph.json",
            "fixture-catalog.json",
            "coverage-targets.json",
            "import-whitelist.json",
        ):
            p = out_dir / name
            if p.exists():
                try:
                    parts.append(f"{name}:{_sha256_stable(p)}")
                except OSError:
                    return None
        return parts
    return None


def _cache_path(out_dir: Path) -> Path:
    return out_dir / "_summaries" / "cache.json"


def _cache_load(out_dir: Path) -> dict:
    p = _cache_path(out_dir)
    if not p.exists():
        return {"schemaVersion": 1, "entries": {}}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "entries" not in data:
            return {"schemaVersion": 1, "entries": {}}
        return data
    except Exception:
        return {"schemaVersion": 1, "entries": {}}


def _cache_write(out_dir: Path, data: dict) -> None:
    target = _cache_path(out_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def snapshot_baseline(jacoco_xml: Path, out_dir: Path, *, force: bool = False) -> Path | None:
    """Snapshot the pre-generation JaCoCo report as state/jacoco-baseline.xml (M4).

    This is the canonical ``--before`` image for per-cycle delta computation
    (``jacoco_parser.py --mode delta --before state/jacoco-baseline.xml``).
    The report passed to ``--jacoco-xml`` reflects coverage with the EXISTING
    tests only — i.e. exactly the "before this session" baseline — so it is
    copied verbatim.

    Write-if-absent by default: re-running the pre-stage (e.g. with ``--since``)
    must NOT move the baseline forward once tests have been generated, or every
    later delta would shrink. Delete the file (or pass ``force=True``) to
    recapture. Returns the baseline path when written, else None.
    """
    if not jacoco_xml.exists():
        return None
    baseline = out_dir / "jacoco-baseline.xml"
    if baseline.exists() and not force:
        return None
    baseline.parent.mkdir(parents=True, exist_ok=True)
    tmp = baseline.with_suffix(".xml.tmp")
    tmp.write_bytes(jacoco_xml.read_bytes())
    os.replace(tmp, baseline)
    return baseline


def _write_empty_batch_plan(out_dir: Path, mode: str, plan_limit: int = 0) -> None:
    """Write a schema-valid, EMPTY batch-plan so downstream (cycle_loop/one_cycle)
    sees a definitive '0 targets' instead of a missing file.

    Used by the early-exit (A): when JaCoCo reports nothing uncovered, the
    expensive middle steps (classpath/index/classification/planning/context) are
    skipped, so no batch-plan would otherwise be produced. The loop then reports
    'no quedan targets' (RC_NO_TARGETS) cleanly instead of erroring on a missing file.

    Keeps the same metadata shape coverage_planner emits (with neutral values) so
    the empty plan validates against the same schema regardless of new fields.
    """
    from common import atomic_write_json  # local: common is on sys.path
    atomic_write_json(out_dir / "batch-plan.json", {
        "schemaVersion": 1,
        "cycle": 1,
        "mode": mode,
        "sizeChosen": 0,
        "totalEligibleTargets": 0,
        "planLimit": plan_limit,
        "rankingStrategy": "",
        "note": "no targets to rank (run_pipeline early-exit)",
        "items": [],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "reason": "no uncovered targets reported by JaCoCo (run_pipeline early-exit)",
    })


def run_step(args: list[str]) -> int:
    print(f"\n$ python {' '.join(str(a) for a in args)}")
    return subprocess.call([sys.executable, *[str(a) for a in args]])


def _write_last_failure(out_dir: Path, step: str, exit_code: int, cmd: list[str]) -> None:
    target = out_dir / "_summaries" / "last-failure.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "step": step,
        "exitCode": exit_code,
        "command": [str(a) for a in cmd],
        "status": "FAIL",
        "timestampUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def run_required_step(
    step_name: str,
    args: list,
    out_dir: Path,
    continue_on_error: bool,
) -> int:
    """Run a pipeline step. Fail-fast by default; opt into legacy chaining
    via *continue_on_error*. On failure, atomically writes
    state/_summaries/last-failure.json and (unless continue_on_error)
    aborts the pipeline via sys.exit(rc).
    """
    rc = run_step(args)
    if rc != 0:
        _write_last_failure(out_dir, step_name, rc, args)
        if not continue_on_error:
            print(
                f"\n[FAIL] Pipeline aborted at step '{step_name}' (exit {rc}). "
                f"See {out_dir / '_summaries' / 'last-failure.json'}",
                file=sys.stderr,
            )
            sys.exit(rc)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Run the deterministic Python pre-stage for the Java test-coverage architecture.\n"
            "After completion, all state/*.json files are ready for LLM agents to consume.\n"
            "No agent needs to re-parse POMs, classpaths, javap output or JaCoCo XML."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--repo",
        required=True,
        help="Root of the Java repository to analyse (must contain pom.xml)",
    )
    ap.add_argument(
        "--out",
        required=True,
        help="State directory where all JSON files will be written (e.g. state/)",
    )
    ap.add_argument(
        "--module",
        default=".",
        help="Module name for bytecode scan / classpath resolution. Default '.' (repo "
             "apunta al módulo). Sin esto los symbol-contracts quedan vacíos y el handoff "
             "se BLOQUEA. Para multi-módulo con --repo en el parent, pasar el nombre del módulo.",
    )
    ap.add_argument(
        "--include-fqcn",
        default=".*",
        help="Regex filter for bytecode scanner: only scan FQCNs matching this pattern "
             "(default: .* — all classes)",
    )
    ap.add_argument(
        "--jacoco-xml",
        default=None,
        help="Path to a JaCoCo jacoco.xml report.  When provided, step 8 runs and "
             "state/coverage-targets.json is populated.",
    )
    ap.add_argument(
        "--coverage-mode",
        default="coverage",
        choices=["coverage", "branch-coverage", "mutation-hardening"],
        help="Coverage scoring mode for jacoco_parser (default: coverage)",
    )
    ap.add_argument(
        "--since",
        default=None,
        help="Git ref (commit/branch/tag) to compute incremental scope from "
             "(e.g. HEAD~1, main).  When provided, step 11 runs and "
             "state/incremental-map.json is populated.",
    )
    ap.add_argument(
        "--full-index",
        action="store_true",
        help="Force full semantic index rebuild even if fingerprints match (step 9)",
    )
    ap.add_argument(
        "--skip",
        nargs="*",
        default=[],
        metavar="STEP",
        help=(
            "Step names to skip (space-separated).  Valid names:\n"
            "  pom, archetype, generated, classpath, stack, bytecode,\n"
            "  source, jacoco, index, classification, deps, fixtures,\n"
            "  planning, incremental, validate, context"
        ),
    )
    ap.add_argument(
        "--continue-on-error",
        action="store_true",
        help=(
            "Legacy mode: continue executing subsequent steps even when one "
            "fails (the previous default). By default the pipeline now aborts "
            "at the first non-zero exit and writes state/_summaries/last-failure.json."
        ),
    )
    ap.add_argument(
        "--no-compact-packs",
        action="store_true",
        help=argparse.SUPPRESS,  # DEPRECATED — ignored (compact packs are mandatory).
    )
    ap.add_argument(
        "--sut",
        default=None,
        metavar="FQCN",
        help=(
            "Restrict Phase 0 to a single FQCN end to end (P3.a): propagated "
            "as --fqcn to bytecode_scanner, --sut to coverage_planner, and "
            "--sut to context_pack_builder. Symbol contracts, batch plan and "
            "context packs are all scoped to this class only."
        ),
    )
    ap.add_argument(
        "--plan-limit",
        type=int,
        default=0,
        help=(
            "How many ranked targets coverage_planner writes to batch-plan.json. "
            "0 = no limit (rank ALL eligible targets, default). N>0 = top N. "
            "This is the PLAN size, not the LLM request size (the batch runner "
            "controls that via --batch-size / --max-batches)."
        ),
    )
    args = ap.parse_args()

    skip: set[str] = set(args.skip or [])
    out_dir = Path(args.out)
    coe = bool(args.continue_on_error)
    rc = 0

    # ── P4.1 cache state ─────────────────────────────────────────────────────
    cache_data = _cache_load(out_dir)
    cache_entries: dict = cache_data.setdefault("entries", {})

    def _try_cache_hit(name: str) -> bool:
        """Return True and emit HIT_CACHE summary when inputs match the
        recorded hash. Otherwise return False."""
        if name not in _CACHEABLE_STEPS:
            return False
        sig = _step_input_signature(name, args, out_dir)
        if sig is None:
            return False
        h = _safe_hash(sig)
        entry = cache_entries.get(name)
        # Set DEBUG_CACHE=1 in the environment to surface the computed vs
        # cached hash for each cacheable step (useful for diagnosing misses).
        if os.environ.get("DEBUG_CACHE"):
            cached = (entry or {}).get("inputHash", "<none>")
            print(f"[DEBUG_CACHE] {name}: computed={h[:16]} cached={str(cached)[:16]}",
                  file=sys.stderr)
        if entry and entry.get("inputHash") == h:
            emit_tool_summary(
                name,
                "HIT_CACHE",
                inputHash=h,
            )
            return True
        # Miss: remember new hash AFTER step executes (handled in step()).
        cache_entries[name] = {
            "_pendingHash": h,
        }
        return False

    def _commit_cache_after(name: str, rc_local: int) -> None:
        if name not in _CACHEABLE_STEPS:
            return
        entry = cache_entries.get(name) or {}
        pending = entry.pop("_pendingHash", None)
        if rc_local == 0 and pending:
            entry["inputHash"] = pending
            entry["timestampUtc"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            cache_entries[name] = entry
            try:
                _cache_write(out_dir, cache_data)
            except OSError:
                pass

    def step(name: str, cmd: list) -> None:
        nonlocal rc
        if _try_cache_hit(name):
            print(f"\n[CACHE HIT] {name} — skipping (inputs unchanged)")
            return
        step_rc = run_required_step(name, cmd, out_dir, coe)
        rc |= step_rc
        _commit_cache_after(name, step_rc)

    # ── Step 1: POM / build-tool contract ────────────────────────────────────
    if "pom" not in skip:
        step("pom", [HERE / "pom_parser.py", "--repo", args.repo, "--out", args.out])

    # ── Step 2: Archetype detection ───────────────────────────────────────────
    if "archetype" not in skip:
        step("archetype", [HERE / "archetype_detector.py", "--repo", args.repo, "--out", args.out])

    # ── Step 3: Generated code scanner ───────────────────────────────────────
    if "generated" not in skip:
        step("generated", [
            HERE / "generated_code_scanner.py", "--repo", args.repo, "--out", args.out,
        ])

    # ── Step 3.5: JaCoCo parser → coverage-targets.json (MOVED EARLY) ─────────
    # jacoco_parser solo necesita el jacoco.xml (no pom/classpath/contracts), y su
    # resultado gatea los pasos caros de abajo. Si JaCoCo no halló NINGÚN método sin
    # cubrir, no hay nada que generar: salteamos classpath (~5s) + bytecode (~5s) +
    # index/clasificación/planner/context y terminamos limpio.
    if "jacoco" not in skip and args.jacoco_xml:
        step("jacoco", [
            HERE / "jacoco_parser.py",
            "--mode", "targets",
            "--xml", args.jacoco_xml,
            "--out", str(Path(args.out) / "coverage-targets.json"),
            "--coverage-mode", args.coverage_mode,
        ])
        # M4: snapshot del mismo reporte como baseline del delta (lo lee el loop).
        b = snapshot_baseline(Path(args.jacoco_xml), Path(args.out))
        if b is not None:
            print(f"[OK] {b}  (delta baseline for jacoco_parser --mode delta --before)")

        # A — early-exit: 0 targets sin cubrir ⇒ nada que generar.
        cov = Path(args.out) / "coverage-targets.json"
        if cov.exists():
            try:
                n_targets = len(json.loads(cov.read_text(encoding="utf-8")).get("targets", []))
            except Exception:
                n_targets = -1
            if n_targets == 0:
                print("[DONE] JaCoCo: 0 uncovered targets — nothing to generate. "
                      "Skipping classpath/index/classification/planning/context.")
                _write_empty_batch_plan(Path(args.out), args.coverage_mode, args.plan_limit)
                return rc
    elif "jacoco" not in skip:
        # #2: make the auto-skip loud and actionable instead of silent.
        print(
            "[WARN] step 'jacoco' skipped: no --jacoco-xml given. "
            "state/coverage-targets.json will be empty, so the batch-plan is "
            "empty and no targets reach the LLM. Generate a JaCoCo report "
            "(mvn test jacoco:report) and pass --jacoco-xml.",
            file=sys.stderr,
        )

    # ── Step 4: Classpath resolver → import-whitelist.json ───────────────────
    if "classpath" not in skip:
        cp_args = [HERE / "classpath_resolver.py", "--repo", args.repo, "--out", args.out]
        if args.module:
            cp_args += ["--module", args.module]
        step("classpath", cp_args)

    # ── Step 5: Stack profile detector → stack-profile.json ──────────────────
    if "stack" not in skip:
        step("stack", [
            HERE / "stack_profile_detector.py", "--repo", args.repo, "--out", args.out,
        ])

    # ── Step 6: Bytecode scanner → symbol-contracts/<fqcn>.json ─────────────
    if "bytecode" not in skip and args.module:
        bc_args = [
            HERE / "bytecode_scanner.py",
            "--repo", args.repo, "--out", args.out,
            "--module", args.module, "--include", args.include_fqcn,
        ]
        if args.sut:
            # P3.a: propagate --sut as an exact FQCN whitelist so the scanner
            # only emits the contracts the user actually wants.
            bc_args += ["--fqcn", args.sut]
        step("bytecode", bc_args)
        # Early validation (post-audit 2026-05-28): validate the contracts as
        # soon as the scanner produced them. Failing here saves 8+ downstream
        # steps when the bytecode/javap output drifts from the schema.
        if "validate" not in skip:
            step("validate-contracts", [
                HERE / "state_validator.py",
                "--state", args.out,
                "--scope", "contracts",
            ])
    elif "bytecode" not in skip:
        # #2: make the auto-skip loud and actionable instead of silent.
        print(
            "[WARN] step 'bytecode' skipped: no --module given. "
            "state/symbol-contracts/ will be empty, so G2 (symbol-evidence) has "
            "nothing to verify against. Pass --module to scan target/classes.",
            file=sys.stderr,
        )

    # ── Step 7: Source symbol enricher ───────────────────────────────────────
    if "source" not in skip:
        source_args = [
            HERE / "source_symbol_enricher.py", "--repo", args.repo, "--out", args.out,
        ]
        if args.module:
            source_args += ["--module", args.module]
        step("source", source_args)

    # ── Step 9 [Phase 1]: Semantic index writer ───────────────────────────────
    if "index" not in skip:
        idx_args = [HERE / "semantic_index_writer.py", "--out", args.out]
        if args.full_index:
            idx_args.append("--full")
        step("index", idx_args)
        # Early validation: catch broken state/index/ before classification/
        # planning consume it.
        if "validate" not in skip:
            step("validate-index", [
                HERE / "state_validator.py",
                "--state", args.out,
                "--scope", "index",
            ])

    # ── Step 10: Classification analyzer → classification-index.json ──────────
    if "classification" not in skip:
        step("classification", [HERE / "classification_analyzer.py", "--out", args.out])

    # ── Step 11 [Phase 2]: Dependency graph extractor ─────────────────────────
    if "deps" not in skip:
        step("deps", [HERE / "dependency_graph_extractor.py", "--out", args.out])

    # ── Step 12 [Phase 2]: Fixture catalog builder ────────────────────────────
    if "fixtures" not in skip:
        step("fixtures", [HERE / "fixture_catalog_builder.py", "--out", args.out])

    # ── Step 13 [Phase 3]: Incremental map writer ────────────────────────────
    # Post-audit 2026-05-28: moved BEFORE planning so the planner can both
    # boost AND optionally filter to affectedClasses in a single pass.
    if "incremental" not in skip and args.since:
        inc_args = [
            HERE / "incremental_map_writer.py",
            "--repo", args.repo,
            "--out", args.out,
            "--since", args.since,
        ]
        if args.module:
            inc_args += ["--module", args.module]
        step("incremental", inc_args)

    # ── Step 14 [Phase 2]: Coverage planner → batch-plan.json ────────────────
    if "planning" not in skip:
        plan_args = [
            HERE / "coverage_planner.py",
            "--out", args.out,
            "--mode", args.coverage_mode,
            "--plan-limit", str(args.plan_limit),
        ]
        if args.sut:
            # P3.a: planning honours --sut end to end (no more "context only"
            # caveat). The batch-plan.json now contains targets for this FQCN
            # only.
            plan_args += ["--sut", args.sut]
        # When --since drove an incremental scan, narrow the batch to the
        # affected SUTs so the LLM context stays scoped to what actually
        # changed (post-audit 2026-05-28).
        if args.since:
            plan_args.append("--incremental-only")
        step("planning", plan_args)

    # ── Step 15: State validator ──────────────────────────────────────────────
    if "validate" not in skip:
        step("validate", [HERE / "state_validator.py", "--state", args.out])

    # ── Step 16: Context pack builder → state/context-packs/<safe_fqcn>.json ─
    # P1.a (post-audit 2026-05-28): --compact is now mandatory. The LLM-facing
    # pack is the minified one under state/context-packs-compact/; the verbose
    # pack under state/context-packs/ is kept only for human inspection.
    # --no-compact-packs survives as a deprecated no-op for backwards compat.
    if args.no_compact_packs:
        print(
            "[WARN] --no-compact-packs is deprecated and ignored. "
            "Compact context-packs are now always produced (audit 2026-05-28).",
            file=sys.stderr,
        )
    if "context" not in skip:
        ctx_args = [HERE / "context_pack_builder.py", "--out", args.out, "--compact"]
        if args.sut:
            ctx_args += ["--sut", args.sut]
        step("context", ctx_args)

    # ── Handoff gate (post-audit 2026-05-28) ─────────────────────────────────
    # Emits state/_summaries/handoff-summary.json so the LLM can skip the
    # old "phases 1-7 read JSON" ceremony and start directly at Generation.
    # Failure here means BLOCKED_PRE_STAGE_MISSING — surfaced via the same
    # last-failure.json plumbing as any other step.
    if "context" not in skip:
        step("handoff", [HERE / "validate_handoff.py", "--state", args.out])

    print("\nDone." if rc == 0 else "\nDone with errors.")
    return rc


if __name__ == "__main__":
    with _TimedRun("run_pipeline") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
