"""batch_runner.py — incremental batch handoff driver (generation-mode=handoff-batch).

Turns the old per-target handoff (1 request → wait → 1 response → apply) into a
batch flow: up to ``batch_size`` targets → ONE generation request → ONE response
with many patch descriptors → apply all → run tests → request repair only for the
failures → apply repairs → decide whether to advance. The runner owns the I/O
(file handoff, test_patch_applier, narrow test runner, manifest); the pure
decisions (selection, request shape, response validation, state machine, advance
rules) live in batch_protocol.py.

Budget: the per-batch minute budget measures the runner's AUTOMATIC work only.
Every MANUAL handoff wait (Claude Code generating JSON, the user pressing ENTER)
is wrapped in budget_enforcer.paused(...), so BUDGET_EXCEEDED can only fire during
automatic work, never while waiting for the human (the bug this milestone fixes).

On-disk layout (under <state>/_llm):
  runs/run-YYYYMMDD-HHMMSS/
    manifest.json
    batches/batch-001/
      request-generation.json     response-generation.json
      validation-result.json
      request-repair-r1.json       response-repair-r1.json
      validation-result-r1.json

Usage (normally launched by run_all_deterministic.py --generation-mode handoff-batch):
  python -m orchestrator.batch_runner --state-dir <state> --repo <java-repo> \\
      [--batch-size 10] [--max-repair-rounds 2] [--max-batches N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import batch_protocol as bp
from . import config, one_cycle

# budget_enforcer lives in the deterministic core (tools/python), invoked by path.
sys.path.insert(0, str(config.TOOLS_PYTHON))
import budget_enforcer  # noqa: E402
import inherited_evidence  # noqa: E402  (shared Throwable-evidence source of truth)

# Exit codes.
RC_DONE = 0
RC_STOPPED = 6      # advance rule said stop (too many failures) or user quit
RC_NO_TARGETS = 7   # nothing pending — mirrors one_cycle/cycle_loop

# narrow_test_runner returns 2 for its own infra failures (no pom.xml / mvn not on
# PATH) and otherwise propagates Maven's exit code (1 on test failure). We treat
# its 2 as "tests not run" so a missing Maven never looks like a compile failure.
_RC_TESTS_NOT_RUN = 2

# ── Hermetic-payload tuning (self-contained request) ─────────────────────────────
# The SUT is shipped verbatim so the generator never reads the Git working tree.
# A larger SUT is truncated with a marker so the per-request token budget stays
# bounded (~4 bytes/token ⇒ 60 KB ≈ 15k tokens).
_MAX_SUT_SOURCE_BYTES = 60_000
# Cap project collaborators whose signatures are projected into the payload, and
# the bytes read from each, so a fan-out SUT never balloons the request.
_MAX_DEP_SIGNATURES = 25
_MAX_DEP_FILE_BYTES = 200_000


# ── small JSON helpers ──────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_run_id() -> str:
    return time.strftime("run-%Y%m%d-%H%M%S")


# ── manifest persistence ─────────────────────────────────────────────────────────

def _manifest_path(run_dir: Path) -> Path:
    return run_dir / "manifest.json"


def _save_manifest(run_dir: Path, manifest: dict) -> None:
    bp.recompute_totals(manifest)
    _write_json(_manifest_path(run_dir), manifest)


# ── handoff wait (budget-paused) ─────────────────────────────────────────────────

def _print(msg: str) -> None:
    print(msg, flush=True)


def _handoff_banner(kind: str, batch_id: str, request: Path, response: Path,
                    repair_round: int | None) -> None:
    tag = "HANDOFF-BATCH" if kind == "generation" else "HANDOFF-REPAIR"
    extra = f", repair round {repair_round}" if repair_round else ""
    _print("\n" + "=" * 72)
    _print(f"[{tag}] Falta {'generar' if kind=='generation' else 'reparar'} tests "
           f"para batch {batch_id}{extra}.")
    _print("Claude Code debe leer:\n  " + str(request))
    _print("y escribir:\n  " + str(response))
    _print("\nCuando Claude Code termine, volvé a esta consola y presioná ENTER.")
    _print("También podés escribir:  skip (saltar este batch) · status (estado) · quit (cortar).")
    _print("Mientras espera, el budget está PAUSADO (no dispara BUDGET_EXCEEDED).")
    _print("=" * 72)


def _wait_for_response(
    request: Path,
    response: Path,
    *,
    state_path: Path,
    manifest: dict,
    kind: str,
    batch_id: str,
    repair_round: int | None = None,
) -> tuple[str, dict | None]:
    """Block until the response JSON is present (and parseable), wrapping the wait
    in a budget pause. Returns (outcome, response_dict):
      ("ok", dict)  response present and JSON-parseable
      ("skip", None) user skipped this batch
      ("quit", None) user aborted the run
    """
    _handoff_banner(kind, batch_id, request, response, repair_round)
    interactive = config.ide_interactive()
    with budget_enforcer.paused(state_path, f"manual handoff: {kind} {batch_id}"):
        _print(f"[handoff] waiting for response JSON: {response.name}")
        if interactive:
            return _wait_interactive(response, manifest)
        return _wait_polling(response)


def _wait_interactive(response: Path, manifest: dict) -> tuple[str, dict | None]:
    while True:
        try:
            ans = input("[handoff] ENTER=listo · skip · status · quit > ").strip().lower()
        except EOFError:
            return _wait_polling(response)
        if ans in ("quit", "q"):
            return "quit", None
        if ans in ("skip", "s"):
            return "skip", None
        if ans == "status":
            _print(json.dumps(manifest.get("totals", {}), ensure_ascii=False))
            continue
        if not response.exists():
            _print(f"[handoff] no encuentro {response}; creala y presioná ENTER.")
            continue
        try:
            return "ok", _load_json(response)
        except Exception as exc:  # noqa: BLE001
            _print(f"[handoff] JSON inválido ({exc}); corregilo y presioná ENTER.")


def _wait_polling(response: Path) -> tuple[str, dict | None]:
    timeout = config.ide_timeout()
    poll = config.ide_poll_seconds()
    deadline = time.time() + timeout
    last_hb = time.time()
    _print(f"[handoff] (no-interactivo) esperando {response.name} hasta {timeout:.0f}s...")
    while time.time() < deadline:
        if response.exists():
            try:
                return "ok", _load_json(response)
            except Exception as exc:  # noqa: BLE001
                _print(f"[handoff] JSON inválido: {exc}; reintento al próximo poll.")
        if time.time() - last_hb >= 30:
            _print(f"[handoff] sigo esperando {response.name}...")
            last_hb = time.time()
        time.sleep(poll)
    _print(f"[handoff] TIMEOUT esperando {response.name}; salto este batch.")
    return "skip", None


# ── patch application + test classification ──────────────────────────────────────

def _apply_patch(patch: dict, *, state_dir: Path, repo: Path,
                 repair_attempts: list[dict] | None = None) -> int:
    """Apply ONE patch descriptor through the sanctioned patcher (gates + budget +
    Java string-literal safety by construction). Returns its exit code
    (0 ok · 2 budget · 3 gate/perimeter · other = patch failed).

    ``repair_attempts`` carries the G7 anti-loop triplets for a repair patch; see
    _repair_triplets. None for first-time generation."""
    sut = bp_patch_sut(patch)
    pack_path = state_dir / "context-packs" / f"{sut}.json"
    return one_cycle.apply_patch(patch, state_dir=state_dir, repo=repo,
                                 context_pack_path=pack_path,
                                 repair_attempts=repair_attempts)


# Default fixId per errorCode, from skills/09-repair/repair-decision-matrix.md.
# Used only to make the G7 anti-loop triplet meaningful — the actual fix is the
# model's; this just labels the attempt deterministically. Unknown codes get a
# generic id.
_FIX_BY_CODE = {
    "E_IMPORT_UNRESOLVED":       "FIX_REPLACE_IMPORT_WHITELIST",
    "E_PACKAGE_UNRESOLVED":      "FIX_DROP_IMPORT",
    "E_METHOD_UNRESOLVED":       "FIX_USE_CONTRACT_METHOD",
    "E_CONSTRUCTOR_UNRESOLVED":  "FIX_USE_CONTRACT_CTOR",
    "E_INTERFACE_INSTANTIATION": "FIX_USE_MOCK_OR_BUILDER",
    "E_TYPE_MISMATCH":           "FIX_ADJUST_FIXTURE_TYPE",
    "E_GENERIC_INFERENCE":       "FIX_EXPLICIT_GENERICS",
    "E_VARARGS":                 "FIX_CAST_FIRST_VARARG",
    "E_OVERRIDE":                "FIX_REMOVE_OVERRIDE",
    "E_ACCESS":                  "FIX_USE_PUBLIC_API",
}
_FIX_GENERIC = "FIX_REPAIR"


def _repair_triplets(failed_items: list[dict], *, state_dir: Path) -> dict[str, list[dict]]:
    """Derive the deterministic G7 anti-loop triplets (errorCode, symbolFQN,
    fixId) for each failing target, so the sanctioned patcher can be invoked with
    --repair-attempt. The ORCHESTRATOR owns this metadata — the model never sees
    it — sourced from state/compile-error-index.json (written per failure by the
    narrow test runner). A target with no indexed compile error (e.g. an
    assertion failure, or the index is absent) gets a single generic triplet keyed
    by its test class, so the attempt is still declared; G7 only blocks a triplet
    that has already FAILED repeatedly, never a first-time, well-formed one."""
    idx_path = state_dir / "compile-error-index.json"
    errors: list[dict] = []
    if idx_path.exists():
        try:
            errors = (_load_json(idx_path) or {}).get("errors", []) or []
        except Exception:
            errors = []

    out: dict[str, list[dict]] = {}
    for item in failed_items:
        tid = item["targetId"]
        test_class = item.get("testClass", "")
        rel = (test_class.replace(".", "/") + ".java") if test_class else ""
        triplets: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        if rel:
            for e in errors:
                fpath = str(e.get("file", "")).replace("\\", "/")
                if not fpath.endswith(rel):
                    continue
                code = str(e.get("code") or "E_OTHER")
                sym = str(e.get("symbolFQN") or "").strip() or test_class
                fix = _FIX_BY_CODE.get(code, _FIX_GENERIC)
                key = (code, sym, fix)
                if key in seen:
                    continue
                seen.add(key)
                triplets.append({"errorCode": code, "symbolFQN": sym, "fixId": fix})
        if not triplets:
            # No per-file compile error (test/assertion failure, or no index):
            # declare one generic attempt so G7 sees a triplet.
            code = ("E_OTHER" if item.get("failureKind") == "COMPILATION_ERROR"
                    else "E_TEST_FAILURE")
            triplets.append({"errorCode": code,
                             "symbolFQN": test_class or tid,
                             "fixId": _FIX_GENERIC})
        out[tid] = triplets
    return out


def bp_patch_sut(patch: dict) -> str:
    sut = patch.get("sut", "")
    if isinstance(sut, dict):
        return sut.get("fqcn", "")
    return sut


def _canonical_test_class(sut: str) -> str:
    return f"{sut}Test" if sut else ""


def _context_allowed_imports(state_dir: Path, sut: str) -> list[str]:
    data = _load_context_pack(state_dir, sut)
    imports = data.get("allowedImports") or []
    if not isinstance(imports, list):
        return []
    return [str(i) for i in imports if isinstance(i, str) and i]


def _load_context_pack(state_dir: Path, sut: str) -> dict:
    if not sut:
        return {}
    pack = state_dir / "context-packs" / f"{sut}.json"
    if not pack.exists():
        return {}
    try:
        loaded = _load_json(pack)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _context_evidence(state_dir: Path, sut: str) -> tuple[list[str], list[dict]]:
    data = _load_context_pack(state_dir, sut)
    ids: list[str] = []
    refs: list[dict] = []

    def add_ref(kind: str, entry: dict) -> None:
        evidence_id = entry.get("evidenceId")
        if not isinstance(evidence_id, str) or not evidence_id:
            return
        if evidence_id not in ids:
            ids.append(evidence_id)
        refs.append({
            "evidenceId": evidence_id,
            "kind": kind,
            "name": entry.get("name") or kind,
            "returnType": entry.get("returnType"),
            "params": entry.get("params", []),
        })

    for ctor in data.get("constructors") or []:
        if isinstance(ctor, dict):
            add_ref("constructor", ctor)
    for method in data.get("methods") or []:
        if isinstance(method, dict):
            add_ref("method", method)

    classification = data.get("classification") if isinstance(data.get("classification"), dict) else {}
    class_type = classification.get("type")
    # Inherited Throwable evidence comes from the shared module so the gate (G2)
    # accepts the exact same synthetic ids the request advertises (no drift).
    if inherited_evidence.is_throwable_sut(sut, class_type):
        for ref in inherited_evidence.throwable_evidence_refs(sut):
            if ref["evidenceId"] not in ids:
                ids.append(ref["evidenceId"])
            refs.append(ref)
    return ids, refs


def _target_method_name(target: dict, sut: str) -> str:
    raw = str(target.get("method") or "")
    if not raw and "#" in str(target.get("targetId") or ""):
        raw = str(target.get("targetId")).split("#", 1)[1]
    name = raw.split("(", 1)[0].strip()
    if " " in name:
        name = name.rsplit(" ", 1)[-1]
    if name == sut.rsplit(".", 1)[-1]:
        return "<init>"
    return name


def _target_evidence_ids(target: dict, sut: str, evidence_refs: list[dict]) -> tuple[str, bool, list[str]]:
    name = _target_method_name(target, sut)
    if not name or name == "<clinit>" or name.startswith("lambda$"):
        return name, False, []

    matched: list[str] = []
    for ref in evidence_refs:
        evidence_id = ref.get("evidenceId")
        if not isinstance(evidence_id, str) or not evidence_id:
            continue
        kind = ref.get("kind")
        ref_name = ref.get("name")
        if name == "<init>" and kind == "constructor":
            matched.append(evidence_id)
        elif kind == "method" and ref_name == name:
            matched.append(evidence_id)
    return name, True, matched


# ── Hermetic payload: SUT source + dependency signatures ─────────────────────────

def _source_path(repo: Path, fqcn: str) -> Path:
    """Conventional production-source path for an FQCN under the Maven repo."""
    return repo / ("src/main/java/" + fqcn.replace(".", "/") + ".java")


def _match_brace(code: str, open_idx: int) -> int:
    """Index of the `}` that closes the `{` at ``open_idx`` (brace-matched,
    string/char-literal aware). Returns the last index if unbalanced."""
    depth = 0
    in_str = in_chr = esc = False
    i, n = open_idx, len(code)
    while i < n:
        c = code[i]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
        elif in_chr:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == "'": in_chr = False
        elif c == '"': in_str = True
        elif c == "'": in_chr = True
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


_TYPE_KEYWORD = re.compile(r"\b(?:class|interface|enum|record)\b")
# A member header is a method/constructor when, ignoring leading annotations, it
# ENDS in `name(params)` (+ optional throws) — anchored at end so an annotation
# like @Table(name="x") earlier in the header never triggers a false positive.
_METHOD_HEADER_TAIL = re.compile(
    r"[A-Za-z_$][\w$]*\s*\([^;{}]*\)\s*(?:throws[\s\w.,<>$]+)?$"
)


def _looks_like_method(header: str) -> bool:
    h = header.strip()
    if not h or _TYPE_KEYWORD.search(h):
        return False
    return bool(_METHOD_HEADER_TAIL.search(h))


def _extract_method_bodies(source: str, sut: str) -> str:
    """Project ONLY the method & constructor bodies (with their signature as an
    anchor) out of a Java source. Package/imports/class declaration/field
    declarations are dropped on purpose — they are already carried by the other
    request fields (allowedImports, evidenceRefs, constructors/methods,
    dependencySignatures). What is NOT anywhere else is the behaviour inside the
    bodies, which is what the generator needs to derive expected outputs and
    branch coverage.

    Implemented with a brace-matching scan at class-body depth (depth 1), so it
    is robust to nested control flow, strings and comments. Returns "" when the
    SUT has no method bodies (e.g. a pure-field DTO or an abstract interface)."""
    code = _JAVA_LINE_COMMENT.sub("", _JAVA_BLOCK_COMMENT.sub("", source))
    blocks: list[str] = []
    i, n = 0, len(code)
    depth = 0
    member_start = 0
    in_str = in_chr = esc = False
    while i < n:
        c = code[i]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
            i += 1; continue
        if in_chr:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == "'": in_chr = False
            i += 1; continue
        if c == '"':
            in_str = True; i += 1; continue
        if c == "'":
            in_chr = True; i += 1; continue
        if c == "{":
            if depth == 1 and _looks_like_method(code[member_start:i]):
                end = _match_brace(code, i)
                header = code[member_start:i].strip()
                blocks.append(header + " " + code[i:end + 1])
                i = end + 1
                member_start = i
                continue
            depth += 1
            if depth == 1:
                member_start = i + 1
            i += 1; continue
        if c == "}":
            depth = max(0, depth - 1)
            if depth == 1:
                member_start = i + 1
            i += 1; continue
        if c == ";" and depth == 1:
            member_start = i + 1
            i += 1; continue
        i += 1
    if not blocks:
        return ""
    # Concise marker only (the selfContainedPolicy already explains that this
    # field is bodies-only); a long banner would dwarf a tiny SUT's actual code.
    simple = sut.rsplit(".", 1)[-1]
    return f"// {simple}: method/constructor bodies\n\n" + "\n\n".join(b.strip() for b in blocks)


def _read_sut_source(repo: Path | None, sut: str) -> tuple[str, bool]:
    """Read the SUT production file and project its method/constructor bodies so
    they travel inside the request (the generator never reads the Git working
    tree). Only the bodies are shipped — the rest of the source is redundant with
    the other request fields.

    Returns (bodies, truncated). Missing repo/file ⇒ ("", False); a projection
    larger than _MAX_SUT_SOURCE_BYTES is clipped and flagged so the per-request
    token budget stays bounded."""
    if repo is None or not sut:
        return "", False
    path = _source_path(repo, sut)
    if not path.exists():
        return "", False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", False
    bodies = _extract_method_bodies(text, sut)
    if len(bodies.encode("utf-8")) > _MAX_SUT_SOURCE_BYTES:
        clipped = bodies.encode("utf-8")[:_MAX_SUT_SOURCE_BYTES].decode("utf-8", errors="ignore")
        marker = (f"\n// … [truncated by coverage-agent: SUT bodies exceed "
                  f"{_MAX_SUT_SOURCE_BYTES} bytes]")
        return clipped + marker, True
    return bodies, False


# Strip Java comments before scanning for member signatures (so a commented-out
# method never leaks into the projected signatures).
_JAVA_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_JAVA_LINE_COMMENT = re.compile(r"//[^\n]*")
# A public/protected member declaration: optional modifiers, optional generic and
# return type, a name, a parameter list, optional throws — up to the body/semicolon.
_MEMBER_SIG = re.compile(
    r"(?P<sig>(?:public|protected)(?:\s+(?:static|final|abstract|synchronized|native|default))*"
    r"[^=;{}()]*?[A-Za-z_$][\w$]*\s*\([^;{}]*\)(?:\s*throws\s[^{;]+)?)\s*[{;]",
    re.S,
)
# Interface methods are implicitly public and have no executable body, so a
# top-of-line `<returnType> name(params);` is a safe capture (no statement bodies
# to confuse it). Covers the modifier-less declarations the public/protected
# anchor above would otherwise miss.
_INTERFACE_METHOD_SIG = re.compile(
    r"(?m)^\s*(?P<sig>(?:default\s+|static\s+)?"
    r"[\w.$<>\[\],\s?]+\s+[A-Za-z_$][\w$]*\s*\([^;{}]*\)(?:\s*throws\s[^;{]+)?)\s*;"
)
_TYPE_DECL = re.compile(
    r"(?:public\s+)?(?:final\s+|abstract\s+)?(?:class|interface|enum|record)\s+[A-Za-z_$][\w$]*[^{]*"
)


def _extract_signatures(repo: Path, fqcn: str) -> dict | None:
    """Best-effort public API surface of a PROJECT source class (constructors +
    public/protected methods), so the generator understands a collaborator's
    shape without leaving the JSON. Returns None for non-project FQCNs (JDK,
    frameworks, deps) — they have no source file under src/main/java."""
    path = _source_path(repo, fqcn)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")[:_MAX_DEP_FILE_BYTES]
    except Exception:
        return None
    code = _JAVA_LINE_COMMENT.sub("", _JAVA_BLOCK_COMMENT.sub("", raw))
    type_decl = ""
    m = _TYPE_DECL.search(code)
    if m:
        type_decl = re.sub(r"\s+", " ", m.group(0)).strip()
    signatures: list[str] = []
    seen: set[str] = set()

    def _add(raw_sig: str) -> None:
        sig = re.sub(r"\s+", " ", raw_sig).strip()
        if sig and sig not in seen:
            seen.add(sig)
            signatures.append(sig)

    for sm in _MEMBER_SIG.finditer(code):
        _add(sm.group("sig"))
    if "interface" in type_decl:
        for sm in _INTERFACE_METHOD_SIG.finditer(code):
            _add(sm.group("sig"))
    if not type_decl and not signatures:
        return None
    return {"fqcn": fqcn, "sourceFile": "src/main/java/" + fqcn.replace(".", "/") + ".java",
            "typeDeclaration": type_decl, "signatures": signatures}


def _dependency_signatures(repo: Path | None, allowed_imports: list[str], sut: str) -> list[dict]:
    """Project the signatures of every allowedImport that is a project source
    class (skipping the SUT itself and all JDK/framework imports), capped to
    _MAX_DEP_SIGNATURES so a fan-out target never balloons the request."""
    if repo is None:
        return []
    out: list[dict] = []
    for fqcn in allowed_imports:
        if fqcn == sut or len(out) >= _MAX_DEP_SIGNATURES:
            continue
        sig = _extract_signatures(repo, fqcn)
        if sig:
            out.append(sig)
    return out


def _enrich_targets_with_imports(
    targets: list[dict], *, state_dir: Path, repo: Path | None = None
) -> list[dict]:
    enriched: list[dict] = []
    for target in targets:
        row = dict(target)
        sut = str(row.get("sut") or "")
        evidence_ids, evidence_refs = _context_evidence(state_dir, sut)
        target_method, target_required, target_evidence = _target_evidence_ids(
            row, sut, evidence_refs
        )
        allowed_imports = _context_allowed_imports(state_dir, sut)
        row["allowedImports"] = allowed_imports
        row["allowedEvidenceIds"] = evidence_ids
        row["evidenceRefs"] = evidence_refs
        row["targetMethodName"] = target_method
        row["targetEvidenceRequired"] = target_required
        row["targetEvidenceIds"] = target_evidence
        # Hermetic payload: ship the SUT verbatim + project-collaborator signatures
        # so the generator never reads the Git working tree (avoids stale/ghost code).
        sut_source, sut_truncated = _read_sut_source(repo, sut)
        row["sutSourceCode"] = sut_source
        row["sutSourceTruncated"] = sut_truncated
        row["dependencySignatures"] = _dependency_signatures(repo, allowed_imports, sut)
        enriched.append(row)
    return enriched


def _run_tests(repo: Path, state_dir: Path, test_classes: list[str]) -> int:
    """Run all applied test classes in ONE narrow invocation (M5 batching).
    Returns the runner's exit code; 0 = every class passed. -1 if Maven absent."""
    if not test_classes:
        return 0
    args = ["--repo", str(repo), "--state", str(state_dir)]
    for tc in test_classes:
        args += ["--test-class", tc]
    rc = one_cycle._run_tool("narrow_test_runner.py", args)
    return rc


def _surefire_status(repo: Path, test_class: str) -> str | None:
    """Read the surefire report for a test class. Returns 'passed', 'failed', or
    None when no report exists (class did not compile/run)."""
    name = f"TEST-{test_class}.xml"
    for report in repo.glob(f"**/surefire-reports/{name}"):
        try:
            text = report.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Cheap, dependency-free parse of the <testsuite ... failures errors> attrs.
        f = re.search(r'failures="(\d+)"', text)
        e = re.search(r'errors="(\d+)"', text)
        fail = (int(f.group(1)) if f else 0) + (int(e.group(1)) if e else 0)
        return "failed" if fail else "passed"
    return None


def _classify_batch(
    manifest: dict, *, repo: Path, applied: dict[str, str], rc: int
) -> dict[str, int]:
    """Map the test outcome onto per-target states. ``applied`` is {targetId:
    testClass}. Returns {'passed', 'failed', 'compile'} counts for the advance rule.

    rc == 0 → every applied class passed. rc != 0 → per-class surefire decides;
    a class with no report is treated as a COMPILE_FAILED (did not run)."""
    passed = failed = compile_failed = 0
    for tid, test_class in applied.items():
        if rc == 0:
            bp.set_status(manifest, tid, bp.PASSED)
            passed += 1
            continue
        status = _surefire_status(repo, test_class)
        if status == "passed":
            bp.set_status(manifest, tid, bp.PASSED)
            passed += 1
        elif status == "failed":
            bp.set_status(manifest, tid, bp.TEST_FAILED)
            failed += 1
        else:
            bp.set_status(manifest, tid, bp.COMPILE_FAILED, note="no surefire report — likely compile error")
            compile_failed += 1
    return {"passed": passed, "failed": failed, "compile": compile_failed}


def _render_test_source_from_descriptor(patch: dict) -> str:
    """Best-effort reconstruction of the test that just failed from its patch
    descriptor, used when the patcher rejected the patch (gate/perimeter) before
    writing any file to disk — so currentTestSource is never empty on a repair.

    This is a faithful-but-approximate rendering (the canonical renderer lives in
    the patcher); it is clearly marked so the model treats it as the failing test
    rather than a file on disk it could re-read."""
    if not isinstance(patch, dict) or not patch:
        return ""
    test_class = str(patch.get("testClass") or "")
    pkg, simple = (test_class.rsplit(".", 1) if "." in test_class else ("", test_class))
    lines: list[str] = [
        "// reconstructed from the rejected patchDescriptor "
        "(the patcher did not write this file to disk)",
    ]
    if pkg:
        lines += [f"package {pkg};", ""]
    for imp in patch.get("allowedImports") or []:
        lines.append(f"import {imp};")
    if patch.get("allowedImports"):
        lines.append("")
    lines.append(f"class {simple or 'GeneratedTest'} {{")
    for field in patch.get("fields") or []:
        if not isinstance(field, dict):
            continue
        ann = field.get("annotation")
        decl = field.get("declaration") or field.get("source") or ""
        if ann:
            lines.append(f"    {ann}")
        if decl:
            lines.append(f"    {decl}")
    for method in patch.get("methods") or []:
        if not isinstance(method, dict):
            continue
        annotations = [str(a) for a in (method.get("annotations") or [])]
        if not annotations:
            annotations = ["@Test"]
        for ann in annotations:
            lines.append(f"    {ann}")
        name = str(method.get("name") or "test")
        body = str(method.get("body") or "")
        lines.append(f"    void {name}() {{")
        for bl in body.splitlines():
            lines.append("        " + bl)
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines)


def _compiler_error_details(state_dir: Path, test_class: str) -> str:
    """Project the verbatim javac/Maven errors for ONE test class out of
    state/compile-error-index.json (written per failure by the narrow runner).

    Returns one block per indexed error — `[code] file:line: message` plus the
    raw compiler line — so the repair payload carries the exact constructor /
    type / arity error instead of a generic "patcher rc=3"."""
    idx = state_dir / "compile-error-index.json"
    if not idx.exists() or not test_class:
        return ""
    try:
        errors = (_load_json(idx) or {}).get("errors", []) or []
    except Exception:
        return ""
    rel = test_class.replace(".", "/") + ".java"
    blocks: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for e in errors:
        fpath = str(e.get("file", "")).replace("\\", "/")
        if not fpath.endswith(rel):
            continue
        code = e.get("code") or "E_OTHER"
        line = str(e.get("line", ""))
        msg = str(e.get("message") or "").strip()
        # Maven prints each diagnostic twice (COMPILATION ERROR section + goal
        # failure); dedupe so the payload carries one block per real error.
        key = (str(code), line, msg)
        if key in seen:
            continue
        seen.add(key)
        block = f"[{code}] {e.get('file', '')}:{line}: {msg}"
        raw = str(e.get("raw") or "").strip()
        if raw and raw != msg:
            block += f"\n    {raw}"
        blocks.append(block)
    return "\n".join(blocks)


def _patcher_rejection_details(state_dir: Path, test_class: str) -> str:
    """The patcher's captured rejection output for a test class (gate code +
    [BLOCKED-DETAIL] JSON), written by one_cycle when an apply returns rc!=0.

    This is what makes a non-compiler rejection (rc=3 gate/perimeter) legible to
    the repair model instead of a bare "patcher rc=3"."""
    if not test_class:
        return ""
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", test_class)
    path = state_dir / "_summaries" / "patcher-decisions" / f"{safe}.json"
    if not path.exists():
        return ""
    try:
        return str((_load_json(path) or {}).get("output") or "").strip()
    except Exception:
        return ""


def _failed_items_for_repair(manifest: dict, *, state_dir: Path, repo: Path,
                             batch_ids: list[str], applied: dict[str, str]) -> list[dict]:
    """Shape the repair payload for the targets still failing in this batch."""
    items = []
    build_output = ""
    blog = state_dir / "_summaries" / "build-output.log"
    if blog.exists():
        try:
            build_output = blog.read_text(encoding="utf-8", errors="replace")[-4000:]
        except Exception:
            build_output = ""
    for tid in bp.failing_target_ids(manifest, batch_ids):
        rec = manifest["targets"].get(tid, {})
        sut = rec.get("sut", "")
        allowed_imports = _context_allowed_imports(state_dir, sut)
        allowed_evidence_ids, evidence_refs = _context_evidence(state_dir, sut)
        target_method, target_required, target_evidence = _target_evidence_ids(
            {"targetId": tid, "method": rec.get("method", "")},
            sut,
            evidence_refs,
        )
        rejected_test_class = applied.get(tid, rec.get("testClass", ""))
        canonical_test_class = _canonical_test_class(sut)
        test_class = canonical_test_class or rejected_test_class
        test_file = "src/test/java/" + test_class.replace(".", "/") + ".java" if test_class else ""
        current_src = ""
        if test_file:
            f = repo / test_file
            if f.exists():
                try:
                    current_src = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    current_src = ""
        # When the patcher rejected the patch (rc=3 gate/perimeter) the file was
        # never written → disk read is empty. Fall back to the rejected descriptor
        # so currentTestSource ALWAYS carries the test that just failed.
        if not current_src:
            current_src = _render_test_source_from_descriptor(rec.get("lastPatchDescriptor") or {})
        # Verbatim javac/Maven errors for this exact test class (constructors that
        # differ in size, invalid types, …) instead of a generic "patcher rc=3".
        compiler_error_details = _compiler_error_details(state_dir, test_class)
        # The patcher's own gate/perimeter rejection (rc=3), captured per testClass —
        # WHY the patch was blocked (e.g. G2 orphan evidenceId), which never reaches
        # the Maven compiler and so is absent from compilerErrorDetails.
        patcher_error_details = _patcher_rejection_details(state_dir, test_class)
        status = rec.get("status")
        if status == bp.COMPILE_FAILED:
            kind = "COMPILATION_ERROR"
        elif status == bp.PATCH_FAILED:
            kind = "PATCH_REJECTED"
        else:
            kind = "TEST_FAILURE"
        # Best one-line summary: concrete compiler line, else the patcher's
        # [BLOCKED] gate line, else the lifecycle note.
        blocked_line = next((ln for ln in patcher_error_details.splitlines()
                             if "[BLOCKED]" in ln), "")
        if compiler_error_details:
            error_summary = compiler_error_details.splitlines()[0]
        elif blocked_line:
            error_summary = blocked_line.strip()
        else:
            error_summary = rec.get("note", kind)
        items.append({
            "targetId": tid,
            "failureKind": kind,
            "sut": sut,
            "canonicalTestClass": canonical_test_class,
            "canonicalTestFile": (
                "src/test/java/" + canonical_test_class.replace(".", "/") + ".java"
                if canonical_test_class else ""
            ),
            "allowedImports": allowed_imports,
            "forbiddenImports": bp._import_policy(allowed_imports)["forbiddenUnlessExplicitlyAllowed"],
            "importPolicy": bp._import_policy(allowed_imports),
            "allowedEvidenceIds": allowed_evidence_ids,
            "evidenceRefs": evidence_refs,
            "targetMethodName": target_method,
            "targetEvidenceRequired": target_required,
            "targetEvidenceIds": target_evidence,
            "evidencePolicy": bp._evidence_policy(allowed_evidence_ids),
            "rejectedTestClass": (
                rejected_test_class
                if rejected_test_class and rejected_test_class != canonical_test_class
                else ""
            ),
            "testClass": test_class,
            "testFile": test_file,
            "errorSummary": error_summary,
            "compilerErrorDetails": compiler_error_details,
            "patcherErrorDetails": patcher_error_details,
            "buildOutput": build_output,
            "currentTestSource": current_src,
        })
    return items


# ── one batch ─────────────────────────────────────────────────────────────────

def _process_generation(
    response_items: list[dict], manifest: dict, *, state_dir: Path, repo: Path,
    batch_ids: list[str],
) -> dict[str, str]:
    """Apply the generation response. Returns {targetId: testClass} for APPLIED
    targets (the ones to test). skipped/failed items are recorded, never fatal."""
    applied: dict[str, str] = {}
    by_id = {it["targetId"]: it for it in response_items}
    for tid in batch_ids:
        it = by_id.get(tid)
        if it is None:
            # The model omitted this target → treat as generation failure (not fatal).
            bp.set_status(manifest, tid, bp.GENERATION_FAILED, note="omitted from response")
            continue
        status = it.get("status")
        if status == "skipped":
            bp.set_status(manifest, tid, bp.SKIPPED, reason=it.get("reason"))
            continue
        if status == "failed":
            bp.set_status(manifest, tid, bp.GENERATION_FAILED, reason=it.get("reason"))
            continue
        patch = it.get("patchDescriptor") or {}
        test_class = patch.get("testClass", "")
        # Persist the descriptor so the repair payload can reconstruct the exact
        # test that failed even when the patcher rejected it before writing a file.
        bp.set_status(manifest, tid, bp.GENERATED, testClass=test_class,
                      lastPatchDescriptor=patch)
        rc = _apply_patch(patch, state_dir=state_dir, repo=repo)
        if rc == 0:
            bp.set_status(manifest, tid, bp.APPLIED, testClass=test_class)
            applied[tid] = test_class
        else:
            # 2 budget, 3 gate/perimeter, other → could not apply this one; do not
            # tear down the rest of the batch.
            bp.set_status(manifest, tid, bp.PATCH_FAILED, note=f"patcher rc={rc}")
            _print(f"[batch] patch no aplicado para {tid} (rc={rc}); sigo con el resto.")
    return applied


def _process_repair(
    response_items: list[dict], manifest: dict, *, state_dir: Path, repo: Path,
    triplets: dict[str, list[dict]] | None = None,
) -> dict[str, str]:
    """Apply a repair response. Returns {targetId: testClass} for re-applied targets.

    ``triplets`` maps targetId → its G7 anti-loop attempts (see _repair_triplets);
    they are forwarded to the patcher so a repair patch is not blocked with
    G7_REPAIR_WITHOUT_TRIPLET."""
    triplets = triplets or {}
    applied: dict[str, str] = {}
    for it in response_items:
        tid = it["targetId"]
        status = it.get("status")
        if status == "abandoned":
            bp.set_status(manifest, tid, bp.ABANDONED, reason=it.get("reason"))
            continue
        if status in ("skipped", "failed"):
            # leave the prior failed state; it may be retried next round or abandoned
            continue
        patch = it.get("patchDescriptor") or {}
        test_class = patch.get("testClass", manifest["targets"].get(tid, {}).get("testClass", ""))
        rc = _apply_patch(patch, state_dir=state_dir, repo=repo,
                          repair_attempts=triplets.get(tid))
        if rc == 0:
            bp.set_status(manifest, tid, bp.REPAIRED, testClass=test_class,
                          lastPatchDescriptor=patch)
            applied[tid] = test_class
        else:
            bp.set_status(manifest, tid, bp.PATCH_FAILED, note=f"repair patcher rc={rc}",
                          lastPatchDescriptor=patch)
    return applied


def run_batches(
    state_dir: Path, repo: Path, *,
    batch_size: int, max_repair_rounds: int, max_batches: int | None,
    module: str = ".",
) -> int:
    state_dir = state_dir.resolve()
    repo = repo.resolve()
    plan_path = state_dir / "batch-plan.json"
    if not plan_path.exists():
        _print(f"[batch] no existe {plan_path}; corré primero la fase 0 (run_all_deterministic).")
        return RC_NO_TARGETS
    plan_items = _load_json(plan_path).get("items", [])

    state_path = state_dir / "execution-state.json"
    run_id = _now_run_id()
    run_dir = config.ide_dir(state_dir) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = bp.new_manifest(run_id, str(repo), generation_mode="handoff-batch",
                               batch_size=batch_size, max_repair_rounds=max_repair_rounds)
    _save_manifest(run_dir, manifest)

    processed = set(one_cycle._processed_ids(state_dir))
    batch_no = 0
    final_rc = RC_DONE

    while True:
        if max_batches is not None and batch_no >= max_batches:
            _print(f"[batch] alcanzado --max-batches={max_batches}; freno.")
            break
        targets = bp.select_batch(plan_items, processed, batch_size)
        if not targets:
            _print("[batch] no quedan targets pendientes.")
            break
        batch_no += 1
        batch_id = f"batch-{batch_no:03d}"
        batch_dir = run_dir / "batches" / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_ids = [t.get("targetId") for t in targets]
        manifest.setdefault("batches", []).append({"batchId": batch_id, "targetIds": batch_ids})
        for t in targets:
            bp.ensure_target(
                manifest,
                t.get("targetId"),
                sut=t.get("sut", ""),
                batch_id=batch_id,
                method=t.get("method", ""),
            )
            bp.set_status(manifest, t.get("targetId"), bp.GENERATION_REQUESTED)

        # Per-batch budget: tick (automatic work starts), check, pause during handoff.
        budget_enforcer.tick(state_path)
        crc, payload = budget_enforcer.check(state_path)
        if crc != 0:
            _print(f"[budget] exceeded during automatic work: {payload.get('reason')}")
            budget_enforcer.reset(state_path)
            manifest["status"] = "STOPPED"
            _save_manifest(run_dir, manifest)
            return budget_enforcer.EXIT_EXCEEDED  # RC 2 == budget exceeded

        # ── generation handoff ──────────────────────────────────────────────────
        request_targets = _enrich_targets_with_imports(targets, state_dir=state_dir, repo=repo)
        req = bp.build_generation_request(run_id, batch_id, request_targets, batch_size=batch_size)
        req_path = batch_dir / "request-generation.json"
        resp_path = batch_dir / "response-generation.json"
        _write_json(req_path, req)
        _save_manifest(run_dir, manifest)

        outcome, resp = _wait_for_response(
            req_path, resp_path, state_path=state_path, manifest=manifest,
            kind="generation", batch_id=batch_id)
        if outcome == "quit":
            manifest["status"] = "STOPPED"
            _save_manifest(run_dir, manifest)
            return RC_STOPPED
        if outcome == "skip":
            for tid in batch_ids:
                bp.set_status(manifest, tid, bp.SKIPPED, reason="batch skipped by user")
                processed.add(tid)
                one_cycle.mark_processed(state_dir, tid)
            _save_manifest(run_dir, manifest)
            continue

        try:
            items = bp.validate_generation_response(resp, request_targets, batch_id=batch_id)
        except bp.BatchResponseError as exc:
            _print(f"[batch] response-generation inválida: {exc}; salto el batch.")
            for tid in batch_ids:
                bp.set_status(manifest, tid, bp.GENERATION_FAILED, note=str(exc))
                processed.add(tid)
                one_cycle.mark_processed(state_dir, tid)
            _save_manifest(run_dir, manifest)
            continue

        applied = _process_generation(items, manifest, state_dir=state_dir, repo=repo,
                                      batch_ids=batch_ids)
        _save_manifest(run_dir, manifest)

        # ── run tests + classify ─────────────────────────────────────────────────
        rc_tests = _run_tests(repo, state_dir, list(applied.values()))
        if applied and rc_tests == _RC_TESTS_NOT_RUN:
            # narrow_test_runner could not run Maven (no pom.xml / mvn not on PATH).
            # Do NOT mark applied targets as failed — that would spawn spurious
            # repair rounds. Leave them APPLIED, persist, and stop with a clear hint.
            _print("[batch] tests NO ejecutados (Maven/pom ausente); dejo los targets "
                   "en APPLIED y freno. Instalá Maven / verificá el --repo y re-corré.")
            budget_enforcer.reset(state_path)
            for tid in batch_ids:
                processed.add(tid)
            manifest["status"] = "STOPPED"
            _save_manifest(run_dir, manifest)
            return RC_STOPPED
        counts = _classify_batch(manifest, repo=repo, applied=applied, rc=rc_tests)
        _write_json(batch_dir / "validation-result.json",
                    {"batchId": batch_id, "rc": rc_tests, "counts": counts,
                     "applied": applied})
        _save_manifest(run_dir, manifest)

        # ── repair rounds (only failures) ────────────────────────────────────────
        had_compile = counts["compile"] > 0
        for rnd in range(1, max_repair_rounds + 1):
            failing = bp.failing_target_ids(manifest, batch_ids)
            if not failing:
                break
            failed_payload = _failed_items_for_repair(manifest, state_dir=state_dir, repo=repo,
                                                       batch_ids=batch_ids, applied=applied)
            # Orchestrator-owned G7 anti-loop triplets, derived from the compile-error
            # index — forwarded to the patcher so the repair is not blocked with
            # G7_REPAIR_WITHOUT_TRIPLET (the model never produces these).
            repair_triplets = _repair_triplets(failed_payload, state_dir=state_dir)
            rreq = bp.build_repair_request(run_id, batch_id, rnd, failed_payload)
            rreq_path = batch_dir / f"request-repair-r{rnd}.json"
            rresp_path = batch_dir / f"response-repair-r{rnd}.json"
            _write_json(rreq_path, rreq)
            for tid in failing:
                bp.set_status(manifest, tid, bp.REPAIR_REQUESTED)
                bp.bump_repair_round(manifest, tid)
            _save_manifest(run_dir, manifest)

            outcome, rresp = _wait_for_response(
                rreq_path, rresp_path, state_path=state_path, manifest=manifest,
                kind="repair", batch_id=batch_id, repair_round=rnd)
            if outcome == "quit":
                manifest["status"] = "STOPPED"
                _save_manifest(run_dir, manifest)
                return RC_STOPPED
            if outcome == "skip":
                break
            try:
                ritems = bp.validate_repair_response(
                    rresp,
                    set(failing),
                    batch_id=batch_id,
                    repair_round=rnd,
                    requested_items=failed_payload,
                )
            except bp.BatchResponseError as exc:
                _print(f"[batch] response-repair-r{rnd} inválida: {exc}; corto repair.")
                break

            reapplied = _process_repair(ritems, manifest, state_dir=state_dir, repo=repo,
                                        triplets=repair_triplets)
            rc_tests = _run_tests(repo, state_dir, list(reapplied.values()))
            rcounts = _classify_batch(manifest, repo=repo, applied=reapplied, rc=rc_tests)
            _write_json(batch_dir / f"validation-result-r{rnd}.json",
                        {"batchId": batch_id, "repairRound": rnd, "rc": rc_tests,
                         "counts": rcounts, "reapplied": reapplied})
            # Targets still failing AND out of rounds → ABANDON.
            for tid in bp.failing_target_ids(manifest, batch_ids):
                if bp.should_abandon(manifest, tid, max_repair_rounds):
                    bp.set_status(manifest, tid, bp.ABANDONED, note="exceeded maxRepairRounds")
            _save_manifest(run_dir, manifest)

        # Anything still failing after the rounds is abandoned.
        for tid in bp.failing_target_ids(manifest, batch_ids):
            bp.set_status(manifest, tid, bp.ABANDONED, note="still failing after repair rounds")

        # Mark every target in the batch processed so the next batch advances.
        for tid in batch_ids:
            processed.add(tid)
            one_cycle.mark_processed(state_dir, tid)
        budget_enforcer.reset(state_path)

        # ── advance decision ─────────────────────────────────────────────────────
        total = len(batch_ids)
        passed = sum(1 for tid in batch_ids
                     if manifest["targets"].get(tid, {}).get("status") == bp.PASSED)
        decision = bp.advance_decision(passed, total, had_global_compile_error=had_compile)
        _print(f"[batch] {batch_id}: {passed}/{total} passed → {decision['action']} "
               f"({decision['reason']})")
        _save_manifest(run_dir, manifest)
        if decision["action"] == bp.ADVANCE_STOP:
            _print("[batch] freno automático. Recomendación: re-correr con --batch-size menor.")
            manifest["status"] = "STOPPED"
            _save_manifest(run_dir, manifest)
            final_rc = RC_STOPPED
            break

    if manifest.get("status") == "RUNNING":
        manifest["status"] = "DONE"
    _save_manifest(run_dir, manifest)
    if manifest.get("status") == "DONE" and final_rc == RC_DONE:
        _print("[batch] generando reporte final con JaCoCo...")
        rc_report = one_cycle._run_tool("batch_final_report.py", [
            "--state-dir", str(state_dir),
            "--repo", str(repo),
            "--run-dir", str(run_dir),
            "--module", module,
        ])
        if rc_report != 0:
            _print(f"[batch] reporte final no pudo completarse (rc={rc_report}); "
                   "el manifest queda disponible.")
    _print(f"[batch] manifest: {_manifest_path(run_dir)}")
    _print(f"[batch] totals: {json.dumps(manifest['totals'], ensure_ascii=False)}")
    return final_rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Driver de handoff por batches (handoff-batch).")
    ap.add_argument("--state-dir", required=True, type=Path)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--batch-size", type=int, default=None,
                    help="Targets por batch (default: config.batch_size / 10).")
    ap.add_argument("--max-repair-rounds", type=int, default=None,
                    help="Rondas de reparación por batch (default: config / 2).")
    ap.add_argument("--max-batches", type=int, default=None,
                    help="Tope de batches por corrida (calibración). Default: sin tope.")
    ap.add_argument("--module", default=".",
                    help="Maven module for the final JaCoCo report (default '.').")
    args = ap.parse_args(argv)

    batch_size = args.batch_size if args.batch_size is not None else config.batch_size()
    max_repair_rounds = (args.max_repair_rounds if args.max_repair_rounds is not None
                         else config.max_repair_rounds())
    return run_batches(args.state_dir, args.repo, batch_size=batch_size,
                       max_repair_rounds=max_repair_rounds,
                       max_batches=args.max_batches,
                       module=args.module)


if __name__ == "__main__":
    sys.exit(main())
