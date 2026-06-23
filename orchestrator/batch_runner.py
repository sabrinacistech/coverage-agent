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
from dataclasses import dataclass
from pathlib import Path

from . import batch_protocol as bp
from . import config, cost_telemetry, one_cycle, prompts, workspace_volumetry

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

# Copy-paste handoff prompt written next to each batch's request, so the human
# pastes a prompt with the REAL resolved run/batch paths (never the placeholder
# run-YYYYMMDD-HHMMSS) into Claude Code / Codex.
HANDOFF_PROMPT_NAME = "handoff-prompt.txt"


# ── small JSON helpers ──────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_run_id() -> str:
    return time.strftime("run-%Y%m%d-%H%M%S")


# ── Run paths: single source of truth (task 5) ───────────────────────────────────

@dataclass(frozen=True)
class RunPaths:
    """The ONE place that composes every on-disk artifact of a run.

    Every request/response/validation path is DERIVED from ``run_dir`` + the
    ``batchId`` (+ repair round), so the runner can never work on ``runs/run-XXXX``
    and accidentally read or write a mirror like ``runs/run-XXXXS``: there is a
    single composition point, and :meth:`assert_consistent` proves each derived
    path stays under ``run_dir`` and carries the right ``runId`` / ``batchId``.

    All members return absolute, resolved :class:`pathlib.Path` objects."""
    state_dir: Path
    run_id: str

    @property
    def run_dir(self) -> Path:
        return (config.ide_dir(self.state_dir) / "runs" / self.run_id).resolve()

    def manifest(self) -> Path:
        return self.run_dir / "manifest.json"

    def batch_dir(self, batch_id: str) -> Path:
        return self.run_dir / "batches" / batch_id

    def request_generation(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "request-generation.json"

    def response_generation(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "response-generation.json"

    def validation_result(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "validation-result.json"

    def preflight_result(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "preflight-result.json"

    def request_repair(self, batch_id: str, rnd: int) -> Path:
        return self.batch_dir(batch_id) / f"request-repair-r{rnd}.json"

    def response_repair(self, batch_id: str, rnd: int) -> Path:
        return self.batch_dir(batch_id) / f"response-repair-r{rnd}.json"

    def validation_result_repair(self, batch_id: str, rnd: int) -> Path:
        return self.batch_dir(batch_id) / f"validation-result-r{rnd}.json"

    def handoff_prompt(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / HANDOFF_PROMPT_NAME

    def assert_consistent(self, batch_id: str, repair_round: int = 1) -> None:
        """Prove every derived path belongs to exactly this runId/batchId and stays
        under run_dir (no mirror folder with a stray suffix). Used by tests and as a
        cheap in-process guard before the handoff."""
        run_dir = self.run_dir
        run_root = str(run_dir)
        run_level = [self.manifest()]
        batch_level = [
            self.batch_dir(batch_id), self.request_generation(batch_id),
            self.response_generation(batch_id), self.validation_result(batch_id),
            self.preflight_result(batch_id), self.handoff_prompt(batch_id),
            self.request_repair(batch_id, repair_round),
            self.response_repair(batch_id, repair_round),
            self.validation_result_repair(batch_id, repair_round),
        ]
        for p in run_level + batch_level:
            rp = str(p.resolve())
            if not rp.startswith(run_root):
                raise ValueError(f"path {p} escapes run_dir {run_dir}")
            # The run folder name must appear EXACTLY (defends against run-XXXX vs
            # run-XXXXS): the path component equal to run_id must be present.
            if self.run_id not in p.parts:
                raise ValueError(f"path {p} does not carry runId {self.run_id!r}")
        for p in batch_level:
            if batch_id not in p.parts:
                raise ValueError(f"path {p} does not carry batchId {batch_id!r}")


# ── manifest persistence ─────────────────────────────────────────────────────────

def _manifest_path(run_dir: Path) -> Path:
    return run_dir / "manifest.json"


def _save_manifest(run_dir: Path, manifest: dict) -> None:
    bp.recompute_totals(manifest)
    _write_json(_manifest_path(run_dir), manifest)


# ── handoff wait (budget-paused) ─────────────────────────────────────────────────

def _print(msg: str) -> None:
    print(msg, flush=True)


_PASTE_OPEN = "───────────── COPIÁ DESDE ACÁ (pegar en Claude Code / Codex) ─────────────"
_PASTE_CLOSE = "───────────── COPIÁ HASTA ACÁ ─────────────"


def _path_run_batch(request: Path) -> tuple[str, str]:
    """(runId, batchId) derived from a request path under
    ``run_dir/batches/<batchId>/request-*.json``. Best-effort: empty strings when
    the path is shorter than expected (only used to fill the prompt template)."""
    batch_id = request.parent.name
    run_id = request.parents[2].name if len(request.parents) >= 3 else ""
    return run_id, batch_id


def _build_handoff_prompt(kind: str, request: Path, response: Path,
                          repair_round: int | None = None) -> str:
    """The ready-to-paste handoff prompt with ABSOLUTE paths already interpolated
    from the real request/response Paths — never the placeholder
    ``run-YYYYMMDD-HHMMSS``. Distinguishes generation vs repair.

    The text comes from the human-editable ``.md`` template under ``prompts/``
    (see prompts/README.md); when the template is absent/unreadable we fall back
    to the built-in prompt below so a missing file never stops a run."""
    schema = (bp.SCHEMA_GENERATION_RESPONSE if kind == "generation"
              else bp.SCHEMA_REPAIR_RESPONSE)
    run_id, batch_id = _path_run_batch(request)
    rendered = prompts.render_handoff_prompt(
        kind,
        request_path=str(request),
        response_path=str(response),
        schema_version=schema,
        run_id=run_id,
        batch_id=batch_id,
        repair_round=repair_round,
    )
    if rendered is not None:
        return rendered
    return _build_handoff_prompt_fallback(kind, request, response, repair_round)


def _build_handoff_prompt_fallback(kind: str, request: Path, response: Path,
                                   repair_round: int | None = None) -> str:
    """Built-in handoff prompt used only when the ``prompts/`` template is missing."""
    if kind == "generation":
        title = "Resolvé el handoff batch de coverage-agent."
        schema = bp.SCHEMA_GENERATION_RESPONSE
        rules = (
            f'- schemaVersion "{schema}".\n'
            "- Un target en la respuesta por cada target del request.\n"
            "- NO devuelvas patchDescriptor ni testSource: el runner construye el "
            "patchDescriptor canónico. Por target devolvé SOLO status + methods + reason + missingSymbols.\n"
            "- Por target: status \"generated\" + methods[], o \"skipped\"+reason, o "
            "\"failed\"+reason, o \"NEED_MORE_CONTEXT\"+missingSymbols.\n"
            "- Cada método: {name, annotations (default [\"@Test\"]), body, evidenceIds}.\n"
            "- No modificar código productivo. No inventar imports/métodos/constructores/clases.\n"
            "- method.evidenceIds ⊆ target.allowedEvidenceIds."
        )
    else:
        rnd = repair_round or 1
        title = f"Resolvé el repair batch de coverage-agent (round {rnd})."
        schema = bp.SCHEMA_REPAIR_RESPONSE
        rules = (
            f'- schemaVersion "{schema}".\n'
            "- Reparar SOLO los tests generados, nunca src/main.\n"
            "- Por item: status \"repaired\" + patchDescriptor válido, o \"abandoned\"/\"skipped\"/\"failed\"+reason.\n"
            "- Usá exclusivamente failedItem.allowedImports y failedItem.allowedEvidenceIds.\n"
            "- patchDescriptor.testClass debe ser EXACTAMENTE failedItem.canonicalTestClass.\n"
            "- En repair, patchId debe empezar con \"repair:\"."
        )
    return (
        f"{_PASTE_OPEN}\n"
        f"{title}\n\n"
        f"Leé este request:\n{request}\n\n"
        f"Escribí la respuesta acá:\n{response}\n\n"
        "Reglas:\n"
        "- La respuesta debe ser SOLO JSON válido.\n"
        f"{rules}\n"
        f"{_PASTE_CLOSE}"
    )


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
    # Ready-to-paste prompt with the REAL resolved paths (no placeholder to edit).
    _print("\n" + _build_handoff_prompt(kind, request, response, repair_round))
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
    # Persist the same ready-to-paste prompt next to the request so the human can
    # open and copy it without scrolling the console. request.parent == batch_dir,
    # already proven consistent by RunPaths.assert_consistent().
    try:
        prompt_path = request.parent / HANDOFF_PROMPT_NAME
        prompt_path.write_text(
            _build_handoff_prompt(kind, request, response, repair_round),
            encoding="utf-8",
        )
    except OSError as exc:
        _print(f"[handoff] no pude escribir {HANDOFF_PROMPT_NAME}: {exc}")
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


_TEST_METHOD_RE = re.compile(r"\bvoid\s+([A-Za-z_$][\w$]*)\s*\(")


def _existing_test_methods(repo: Path | None, sut: str) -> list[str]:
    """@Test method names already in the SUT's test class so the generator avoids
    exact duplicates and knows what coverage already exists. Returns [] when the
    repo is absent or no test file exists yet (a new SUT with no tests)."""
    if repo is None or not sut:
        return []
    test_file = repo / ("src/test/java/" + sut.replace(".", "/") + "Test.java")
    if not test_file.exists():
        return []
    try:
        lines = test_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    names: list[str] = []
    for i, line in enumerate(lines):
        if "@Test" not in line:
            continue
        # Handle `@Test void name(` on the same line (compact style) or
        # the standard `@Test` alone followed by the method signature.
        m = _TEST_METHOD_RE.search(line)
        if m:
            name = m.group(1)
            if name not in names:
                names.append(name)
            continue
        for j in range(i + 1, min(i + 5, len(lines))):
            m = _TEST_METHOD_RE.search(lines[j])
            if m:
                name = m.group(1)
                if name not in names:
                    names.append(name)
                break
    return names


def _expected_behavior_hints(item: dict) -> list[str]:
    """Human-readable behavior hints sourced from the plan item's context block.
    The planner writes generationHint (free text) and syntheticCoverageTargets
    (lambda/branch sub-targets with descriptions) — these tell the generator which
    branches to cover without requiring a repo read."""
    context = item.get("context") or {}
    hints: list[str] = []
    hint = str(context.get("generationHint") or "").strip()
    if hint:
        hints.append(hint)
    for sc in (context.get("syntheticCoverageTargets") or []):
        if isinstance(sc, dict):
            desc = str(sc.get("description") or sc.get("label") or sc.get("id") or "").strip()
            if desc:
                hints.append(desc)
        elif isinstance(sc, str) and sc.strip():
            hints.append(sc.strip())
    return hints


# ── fixturePlan: deterministic construction recipe (task 1) ──────────────────────
# Safe literal sample values for primitive / String / boxed / CharSequence types,
# so a fixturePlan collaborator of one of these is created by COPYING a value
# instead of the model inventing one. Mirrors fixture_catalog_builder._TYPE_DEFAULTS
# (kept local so the runner carries no import-time dependency on that tool).
_LITERAL_SAMPLE = {
    "String": '""', "CharSequence": '""',
    "int": "0", "Integer": "0", "long": "0L", "Long": "0L",
    "short": "(short) 0", "Short": "(short) 0", "byte": "(byte) 0", "Byte": "(byte) 0",
    "char": "'a'", "Character": "'a'", "boolean": "false", "Boolean": "false",
    "double": "0.0d", "Double": "0.0d", "float": "0.0f", "Float": "0.0f",
}
_LITERAL_TYPES = frozenset(_LITERAL_SAMPLE)
# Context-pack instantiation strategies (fixture-catalog / dependency-graph) that
# denote a CONCRETELY constructible collaborator → fixturePlan 'new'.
_CONSTRUCTIBLE_STRATEGIES = frozenset({"constructor", "new", "builder", "factory"})


def _simple_name(fqcn: str) -> str:
    """Simple class name of an FQCN, stripping any generic suffix."""
    return fqcn.split("<", 1)[0].strip().rsplit(".", 1)[-1] if fqcn else ""


def _camel(fqcn_or_type: str) -> str:
    """Conventional camelCase local-variable name for a type (Foo → foo)."""
    simple = _simple_name(fqcn_or_type)
    return (simple[:1].lower() + simple[1:]) if simple else ""


def _is_literal_type(java_type: str) -> bool:
    return _simple_name(java_type) in _LITERAL_TYPES


def _literal_value(java_type: str) -> str:
    return _LITERAL_SAMPLE.get(_simple_name(java_type), '""')


def _mockito_available(allowed_imports: list[str]) -> bool:
    return any(isinstance(i, str) and i.startswith("org.mockito.") for i in allowed_imports)


def _collaborator_strategies(pack: dict) -> dict[str, str]:
    """type → instantiation strategy, from the context-pack's fixtures and
    dependencies. This is the ONLY evidence the runner has about how a
    collaborator can be built; absence ⇒ the strategy cannot be derived."""
    out: dict[str, str] = {}
    for fx in pack.get("fixtures") or []:
        if isinstance(fx, dict):
            t, s = fx.get("type"), fx.get("strategy")
            if isinstance(t, str) and t and isinstance(s, str) and t not in out:
                out[t] = s
    for dep in pack.get("dependencies") or []:
        if isinstance(dep, dict):
            t, s = dep.get("type"), dep.get("instantiationStrategy")
            if isinstance(t, str) and t and isinstance(s, str) and t not in out:
                out[t] = s
    return out


def _creation_strategy(java_type: str, strategies: dict[str, str], mockito_ok: bool) -> tuple[str, str | None]:
    """Deterministically map a collaborator type to a fixturePlan creationStrategy.

    Returns (strategy, value) where value is the literal expression for 'literal'
    and None otherwise. Strategy ∈ {literal, new, mock, unresolved}:
      * primitive/String/boxed/CharSequence → literal (with a safe sample value);
      * a type the context-pack can concretely build (constructor/builder/factory)
        → new;
      * a type known to need mocking (interface/abstract → 'mock' strategy in the
        pack) → mock if Mockito is available, else unresolved;
      * no derivable evidence at all → unresolved (do NOT guess)."""
    if _is_literal_type(java_type):
        return "literal", _literal_value(java_type)
    base = java_type.split("<", 1)[0].strip()
    strat = (strategies.get(java_type) or strategies.get(base)
             or strategies.get(_simple_name(java_type)))
    if strat in _CONSTRUCTIBLE_STRATEGIES:
        return "new", None
    if strat == "mock":
        return ("mock", None) if mockito_ok else ("unresolved", None)
    return "unresolved", None


# ── creationRecipe: HOW to build a `new` collaborator (round-2 task 1/2) ─────────
# A collaborator with creationStrategy 'new' carried no construction recipe before:
# the model still had to improvise `new Foo(...)`. These helpers project a DERIVED
# recipe — from the SUT's fixture-catalog entry, or from the collaborator's OWN
# context-pack constructor — and NEVER fabricate a snippet they cannot back with
# evidence (an undeterminable construction stays without an `expression`).

def _fixture_for_type(pack: dict, java_type: str) -> dict | None:
    """The fixture-catalog entry whose ``type`` matches ``java_type`` (by FQCN,
    its generic base, or simple name). None when the pack carries no such fixture."""
    base = java_type.split("<", 1)[0].strip()
    simple = _simple_name(java_type)
    for fx in pack.get("fixtures") or []:
        if not isinstance(fx, dict):
            continue
        t = fx.get("type")
        if not isinstance(t, str) or not t:
            continue
        if t == java_type or t == base or _simple_name(t) == simple:
            return fx
    return None


def _recipe_from_fixture(fx: dict, simple: str) -> dict | None:
    """A creationRecipe DERIVED from a fixture-catalog entry. Emits a concrete
    construction snippet ONLY when the evidence supports it (a constructor with
    projected ``values``, a builder with ``builderEvidence``); otherwise it cites
    the evidenced strategy without fabricating arguments the model would take as
    truth."""
    strategy = fx.get("strategy")
    values = fx.get("values") if isinstance(fx.get("values"), dict) else {}
    arg_values = [str(v) for v in values.values()]
    if strategy == "constructor":
        recipe = {"kind": "constructor", "evidenceId": fx.get("constructorEvidence"),
                  "argumentValues": arg_values, "source": "fixture-catalog"}
        if arg_values:
            recipe["expression"] = f"new {simple}({', '.join(arg_values)})"
        else:
            recipe["note"] = ("construct via the evidenced constructor; arguments "
                              "not projected — use dependencySignatures")
        return recipe
    if strategy == "builder":
        evidence = fx.get("builderEvidence")
        recipe = {"kind": "builder", "evidenceId": evidence,
                  "requiredValues": dict(values), "source": "fixture-catalog"}
        if isinstance(evidence, str) and evidence.strip():
            recipe["template"] = evidence.strip()
        return recipe
    if strategy == "factory":
        return {"kind": "factory", "evidenceId": fx.get("factoryEvidence"),
                "argumentValues": arg_values, "source": "fixture-catalog",
                "note": "construct via the evidenced static factory"}
    return None


def _recipe_from_pack_constructor(pack: dict, fqcn: str, *, state_dir: Path | None,
                                  mockito_ok: bool, seen: frozenset[str]) -> dict | None:
    """A fully-concrete ``new Type(...)`` recipe derived from the collaborator's own
    context-pack: its smallest public constructor, with every argument itself
    DERIVED (a literal, or a nested fully-derivable construction). Returns None
    unless the WHOLE construction can be derived — never emits a placeholder
    (round-2 task 2: rescue a project value-object that has an evidenced ctor)."""
    ctors = [c for c in (pack.get("constructors") or [])
             if isinstance(c, dict) and (c.get("visibility") or "public") == "public"]
    if not ctors:
        return None
    chosen = min(ctors, key=lambda c: len(c.get("params") or []))
    simple = _simple_name(fqcn)
    args: list[str] = []
    for p in (chosen.get("params") or []):
        ptype = str((p or {}).get("type") or "")
        if _is_literal_type(ptype):
            args.append(_literal_value(ptype))
            continue
        nested = _derive_new_recipe(ptype, sut_pack=pack, state_dir=state_dir,
                                    mockito_ok=mockito_ok, seen=seen)
        expr = nested.get("expression") if nested else None
        if not expr:
            return None  # cannot fully derive → do not invent
        args.append(expr)
    return {"kind": "constructor", "expression": f"new {simple}({', '.join(args)})",
            "evidenceId": chosen.get("evidenceId"), "argumentValues": args,
            "source": "collaborator-context-pack"}


def _derive_new_recipe(java_type: str, *, sut_pack: dict, state_dir: Path | None,
                       mockito_ok: bool, seen: frozenset[str]) -> dict | None:
    """creationRecipe for a 'new' collaborator (task 1) and the resolver that
    rescues an otherwise-unresolved collaborator from its OWN context-pack
    constructor (task 2). DERIVED only — returns None when nothing in the metadata
    lets us build the type without inventing. Recursion is bounded by ``seen`` so a
    constructor cycle (A needs B needs A) terminates instead of looping."""
    fx = _fixture_for_type(sut_pack, java_type)
    if fx is not None:
        recipe = _recipe_from_fixture(fx, _simple_name(java_type))
        if recipe is not None:
            return recipe
    base = java_type.split("<", 1)[0].strip()
    if state_dir is not None and base and base not in seen:
        collab_pack = _load_context_pack(state_dir, base)
        if collab_pack:
            recipe = _recipe_from_pack_constructor(
                collab_pack, base, state_dir=state_dir, mockito_ok=mockito_ok,
                seen=seen | {base})
            if recipe is not None:
                return recipe
    return None


# ── enum constants for a <clinit>/enum target (round-2 task 3) ────────────────────
# No evidenceRef kind carries enum constants, so an enum <clinit> target was almost
# always skipped (CLINIT_WITHOUT_ENUM_CONSTANTS). We DERIVE the constant names from
# the production source (already read for the hermetic payload) and attach them as a
# request hint the pre-flight accepts — never invented.
_ENUM_DECL_RE = re.compile(r"\benum\s+([A-Za-z_$][\w$]*)")


def _enum_constant_region(body: str) -> str:
    """The leading region of an enum body holding the constant declarations:
    everything up to the first top-level ';' (or the whole body when the enum has
    no members beyond its constants). Paren/brace aware so a constant with a
    constructor argument or a member body does not end the scan early."""
    depth = 0
    for i, c in enumerate(body):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(0, depth - 1)
        elif c == ";" and depth == 0:
            return body[:i]
    return body


def _split_enum_constants(region: str) -> list[str]:
    """Constant names from an enum constant region (top-level comma separated); for
    each element take the leading identifier (drops constructor args / member body)."""
    elements: list[str] = []
    token: list[str] = []
    depth = 0
    for c in region:
        if c in "([{":
            depth += 1; token.append(c)
        elif c in ")]}":
            depth = max(0, depth - 1); token.append(c)
        elif c == "," and depth == 0:
            elements.append("".join(token)); token = []
        else:
            token.append(c)
    if "".join(token).strip():
        elements.append("".join(token))
    names: list[str] = []
    for el in elements:
        m = re.match(r"\s*([A-Za-z_$][\w$]*)", el)
        if m:
            names.append(m.group(1))
    return names


def _enum_constants_from_source(source: str) -> list[str]:
    code = _JAVA_LINE_COMMENT.sub("", _JAVA_BLOCK_COMMENT.sub("", source))
    m = _ENUM_DECL_RE.search(code)
    if not m:
        return []
    brace = code.find("{", m.end())
    if brace < 0:
        return []
    end = _match_brace(code, brace)
    region = _enum_constant_region(code[brace + 1:end])
    out: list[str] = []
    for name in _split_enum_constants(region):
        if name not in out:
            out.append(name)
    return out


def _load_symbol_contract(state_dir: Path | None, fqcn: str) -> dict:
    """Load the persisted, schema-validated symbol-contract for an FQCN
    (``state/symbol-contracts/<fqcn>.json``) or {} when absent/unreadable. This is
    the state_validator-checked artifact, so its ``kind`` is the canonical type
    classification (no drift with the planner/classifier)."""
    if state_dir is None or not fqcn:
        return {}
    path = state_dir / "symbol-contracts" / f"{fqcn}.json"
    if not path.exists():
        return {}
    try:
        loaded = _load_json(path)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _is_enum_sut(sut: str, pack: dict, *, state_dir: Path | None = None) -> bool:
    """True when the SUT is an enum. Prefers the schema-validated symbol-contract
    (``kind == "enum"``) — the canonical source of truth, already validated by
    state_validator — and falls back to the context-pack ``classification.type`` when
    no contract is on disk, so the legacy behaviour is preserved verbatim."""
    contract = _load_symbol_contract(state_dir, sut)
    kind = contract.get("kind")
    if isinstance(kind, str) and kind:
        return kind == "enum"
    classification = pack.get("classification") if isinstance(pack.get("classification"), dict) else {}
    return classification.get("type") == "enum"


def _enum_constants(repo: Path | None, sut: str, pack: dict, *,
                    state_dir: Path | None = None) -> list[str]:
    """Enum-constant NAMES for an enum SUT (task 3), so a value/valueOf/getter target
    is testable batch-only instead of being skipped for want of enum-constant
    evidence. Returns [] when the SUT is not an enum, the repo/source is absent, or no
    constants parse (never invents — the pre-flight gate then skips the target so it
    never reaches the model).

    Enum CLASSIFICATION is taken from the schema-validated symbol-contract
    (``kind: enum``) when present — the canonical, state_validator-checked source —
    and falls back to the context-pack classification otherwise (retro-compat). This
    is what the failing run needed: ``_is_enum_sut`` no longer depends on a possibly
    absent/divergent context-pack ``classification.type``.

    The constant NAMES, however, are carried by NO persisted artifact: the
    symbol-contract schema is ``additionalProperties: false`` and the bytecode scanner
    emits only constructors/methods (it drops the static enum-constant fields), and
    neither the semantic-index nor the context-pack project the names. So the
    production .java stays the single source of the NAMES — read here on the same
    precondition the hermetic payload already relies on (``src/main/java/<fqcn>.java``
    exists for every target). When the source is genuinely absent the enum degrades to
    a clean pre-flight skip (better skipped than hallucinated)."""
    if not _is_enum_sut(sut, pack, state_dir=state_dir):
        return []
    if repo is None or not sut:
        return []
    path = _source_path(repo, sut)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return _enum_constants_from_source(text)


def _build_fixture_plan(pack: dict, sut: str, allowed_imports: list[str],
                        *, state_dir: Path | None = None) -> dict:
    """Derive the per-target fixturePlan (task 1) from the context-pack.

    Style is always ``local_variables`` (lowest ambiguity); a collaborator that can
    only be mocked is still allowed as a @Mock field via patchDescriptor.fields when
    Mockito is whitelisted. Every value is DERIVED — never invented: if any required
    collaborator cannot be resolved the plan is marked ``complete: false`` with the
    offending entries in ``unresolvedCollaborators`` (the model must then answer
    NEED_MORE_CONTEXT instead of improvising)."""
    simple = _simple_name(sut)
    mockito_ok = _mockito_available(allowed_imports)
    plan: dict = {
        "style": "local_variables",
        "sutVariable": _camel(sut) or "subject",
        "constructor": None,
        "requiredCollaborators": [],
        "unresolvedCollaborators": [],
        "mockFrameworkAvailable": mockito_ok,
        "complete": True,
    }
    ctors = [c for c in (pack.get("constructors") or []) if isinstance(c, dict)]
    public = [c for c in ctors if (c.get("visibility") or "public") == "public"]
    if not public:
        # No public constructor evidenced. If non-public ctors exist the SUT can't
        # be built from a test → incomplete. If there are NO ctors at all the SUT
        # has an implicit/default constructor or is exercised via statics/enum →
        # leave construction to the model (complete, no collaborators).
        if ctors:
            plan["complete"] = False
            plan["unresolvedCollaborators"] = [
                {"role": "constructor", "type": sut,
                 "reason": "no public constructor evidenced in the context-pack"}]
        return plan

    strategies = _collaborator_strategies(pack)
    # Seed the recursion guard with the SUT itself so a self-referential collaborator
    # never loops back into the SUT's own constructor.
    seen = frozenset({sut.split("<", 1)[0].strip()})
    chosen = min(public, key=lambda c: len(c.get("params") or []))
    params = chosen.get("params") or []
    used: set[str] = set()
    collaborators: list[dict] = []
    arg_names: list[str] = []
    for idx, p in enumerate(params):
        jtype = str((p or {}).get("type") or "java.lang.Object")
        name = (p or {}).get("name") or _camel(jtype) or f"arg{idx}"
        base, n = name, 1
        while name in used:
            n += 1
            name = f"{base}{n}"
        used.add(name)
        arg_names.append(name)
        strategy, value = _creation_strategy(jtype, strategies, mockito_ok)
        collab: dict = {"name": name, "type": jtype, "creationStrategy": strategy}
        if strategy == "literal":
            collab["value"] = value
        elif strategy == "new":
            # task 1: attach HOW to build this collaborator (from its fixture entry
            # or its own context-pack constructor) so the model copies it.
            recipe = _derive_new_recipe(jtype, sut_pack=pack, state_dir=state_dir,
                                        mockito_ok=mockito_ok, seen=seen)
            if recipe is not None:
                collab["creationRecipe"] = recipe
        elif strategy == "unresolved":
            # task 2: a collaborator with no SUT-level fixture/dependency strategy may
            # still be a project value-object whose OWN context-pack carries an
            # evidenced public constructor → rescue it to 'new' with a fully-derived
            # recipe (never on a partial/placeholder construction).
            recipe = _derive_new_recipe(jtype, sut_pack=pack, state_dir=state_dir,
                                        mockito_ok=mockito_ok, seen=seen)
            if recipe is not None and recipe.get("expression"):
                strategy = "new"
                collab["creationStrategy"] = "new"
                collab["creationRecipe"] = recipe
        collaborators.append(collab)
        if strategy == "unresolved":
            plan["unresolvedCollaborators"].append(
                {"name": name, "type": jtype,
                 "reason": "no literal value, evidenced constructor, or mock strategy derivable"})
    plan["requiredCollaborators"] = collaborators
    plan["constructor"] = {
        "evidenceId": chosen.get("evidenceId"),
        "params": [{"type": str((p or {}).get("type") or "java.lang.Object"), "name": nm}
                   for p, nm in zip(params, arg_names)],
        "invocation": f"new {simple}({', '.join(arg_names)})",
    }
    if plan["unresolvedCollaborators"]:
        plan["complete"] = False
    return plan


def _enrich_targets_with_imports(
    targets: list[dict], *, state_dir: Path, repo: Path | None = None
) -> list[dict]:
    enriched: list[dict] = []
    for target in targets:
        row = dict(target)
        sut = str(row.get("sut") or "")
        pack = _load_context_pack(state_dir, sut)
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
        # Deterministic construction recipe (task 1/2): how to declare the SUT and
        # create each constructor collaborator (with a per-collaborator creationRecipe
        # for 'new' types), derived from the context-pack so the model copies it
        # instead of inventing variables. state_dir lets _build_fixture_plan read a
        # collaborator's OWN context-pack to rescue an otherwise-unresolved value-object.
        row["fixturePlan"] = _build_fixture_plan(pack, sut, allowed_imports, state_dir=state_dir)
        # Enum-constant hint (task 3): an enum <clinit>/value target is only testable
        # batch-only when the constant names travel in the request. Enum-ness comes
        # from the schema-validated symbol-contract (kind: enum); the constant names
        # are parsed from the production source (no artifact persists them). The
        # sutIsEnum flag lets the pre-flight gate recognise an enum even when no
        # constant could be derived (→ a clean skip instead of a wasted handoff).
        if _is_enum_sut(sut, pack, state_dir=state_dir):
            row["sutIsEnum"] = True
        enum_constants = _enum_constants(repo, sut, pack, state_dir=state_dir)
        if enum_constants:
            row["enumConstants"] = enum_constants
        # Hermetic payload: ship the SUT verbatim + project-collaborator signatures
        # so the generator never reads the Git working tree (avoids stale/ghost code).
        sut_source, sut_truncated = _read_sut_source(repo, sut)
        row["sutSourceCode"] = sut_source
        row["sutSourceTruncated"] = sut_truncated
        row["dependencySignatures"] = _dependency_signatures(repo, allowed_imports, sut)
        # Populated from the existing test file + plan-item context hints so the
        # generator knows what coverage exists and which branches to target,
        # without reading any repository file (structuredContext in the request).
        row["existingRelatedTests"] = _existing_test_methods(repo, sut)
        row["expectedBehavior"] = _expected_behavior_hints(row)
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


def _is_need_more_context_skip(rec: dict) -> bool:
    """True when a target ended SKIPPED because the model answered NEED_MORE_CONTEXT
    (reason MISSING_CONTEXT) — or, defensively, a CLINIT_WITHOUT_ENUM_CONSTANTS skip.
    Such a target was never a generation FAILURE — the system cleanly recognised it
    lacked context — so it must NOT count against the batch pass-rate (Fix 3)."""
    if rec.get("status") != bp.SKIPPED:
        return False
    reason = str(rec.get("reason") or "")
    return (reason.startswith(bp.ABANDON_MISSING_CONTEXT)
            or bp.PREFLIGHT_CLINIT_NO_CONSTANTS in reason)


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


def _validation_counts(gen_counts: dict, test_counts: dict, *, applied: int) -> dict:
    """Merge the per-item hydration counts with the test-run outcome into the
    validation-result counts. ``compile`` is preserved verbatim because the advance
    rule reads it; generation 'failed' (LLM-reported) is kept distinct from test
    'failed' (a surefire failure) so the two are never conflated."""
    return {
        "received": gen_counts.get("received", 0),
        "generatedValid": gen_counts.get("generatedValid", 0),
        "generatedInvalid": gen_counts.get("generatedInvalid", 0),
        "applied": applied,
        "passed": test_counts.get("passed", 0),
        "failed": test_counts.get("failed", 0),
        "compile": test_counts.get("compile", 0),
        "skipped": gen_counts.get("skipped", 0),
        "needMoreContext": gen_counts.get("needMoreContext", 0),
        "omitted": gen_counts.get("omitted", 0),
        "generationFailed": gen_counts.get("failed", 0),
        "duplicated": gen_counts.get("duplicated", 0),
        "unknown": gen_counts.get("unknown", 0),
    }


def _empty_validation_counts(*, received: int = 0, omitted: int = 0) -> dict:
    """Zeroed validation counts (shape-compatible with _validation_counts), for the
    paths that never reached the hydrator (no sendable targets / unparseable
    response). Carries ``compile`` so the advance rule never KeyErrors."""
    return _validation_counts({"received": received, "omitted": omitted}, {}, applied=0)


def _validation_targets(manifest: dict, target_ids: list[str], diagnostics: list[dict]) -> list[dict]:
    """Per-target rows for validation-result.json: the target's final lifecycle
    status plus the hydration diagnostic (reason/message) when one exists."""
    diag_by_id: dict = {}
    for d in diagnostics or []:
        tid = d.get("targetId")
        if tid is not None and tid not in diag_by_id:
            diag_by_id[tid] = d
    out: list[dict] = []
    for tid in target_ids:
        rec = manifest.get("targets", {}).get(tid, {})
        d = diag_by_id.get(tid, {})
        out.append({
            "targetId": tid,
            "status": rec.get("status"),
            "reason": rec.get("reason") or d.get("reason") or "",
            "message": d.get("message", ""),
        })
    return out


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
        item = {
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
        }
        # Structured diagnosis (task 7): never a bare "patcher rc=3". failureSignature
        # lets the repair loop detect the same cause recurring across rounds.
        item["failureSignature"] = bp.failure_signature(item)
        item["repairCause"] = bp.build_repair_cause(
            item, previous_signature=rec.get("lastFailureSignature"))
        items.append(item)
    return items


# ── one batch ─────────────────────────────────────────────────────────────────

def _process_generation(
    response_items: list[dict], manifest: dict, *, state_dir: Path, repo: Path,
    batch_ids: list[str], fixture_plans: dict[str, dict] | None = None,
) -> tuple[dict[str, str], list[dict]]:
    """Apply the generation response. Returns (applied, complianceWarnings) where
    ``applied`` is {targetId: testClass} for the APPLIED targets (the ones to test)
    and ``complianceWarnings`` is the auditable fixturePlan-signal list (task 4),
    persisted alongside the batch in validation-result.json. skipped/failed items
    are recorded, never fatal."""
    applied: dict[str, str] = {}
    compliance_warnings: list[dict] = []
    plans = fixture_plans or {}
    by_id = {it["targetId"]: it for it in response_items}
    for tid in batch_ids:
        it = by_id.get(tid)
        if it is None:
            # The model omitted this target → treat as generation failure (not fatal).
            bp.set_status(manifest, tid, bp.GENERATION_FAILED, note="omitted from response")
            continue
        status = it.get("status")
        if bp._is_needs_context(status):
            # contextPolicy answer: the request lacked a needed symbol. Persist the
            # missing symbols for audit and skip (never invent / read the repo).
            reason = it.get("reason") or "model requested more context"
            bp.set_status(manifest, tid, bp.SKIPPED,
                          reason=f"{bp.ABANDON_MISSING_CONTEXT}: {reason}",
                          missingSymbols=it.get("missingSymbols") or [])
            continue
        if status == "skipped":
            bp.set_status(manifest, tid, bp.SKIPPED, reason=it.get("reason"))
            continue
        if status == "failed":
            bp.set_status(manifest, tid, bp.GENERATION_FAILED, reason=it.get("reason"))
            continue
        patch = it.get("patchDescriptor") or {}
        test_class = patch.get("testClass", "")
        # fixturePlan advisory SIGNAL (task 4): the model may still reference a
        # collaborator variable that is neither declared locally nor in the plan.
        # This NEVER blocks the patch — it is collected into fixtureComplianceWarnings
        # (written to validation-result.json, auditable alongside the batch) and
        # printed, so the undeclared-symbol failure mode is visible before the test runs.
        signals = bp.undeclared_fixture_signal(patch, plans.get(tid))
        if signals:
            _print(f"[fixturePlan] {tid}: referencias no declaradas fuera del plan: "
                   f"{', '.join(signals)} (señal advisory, no bloquea).")
            compliance_warnings.append(
                {"targetId": tid, "undeclaredReferences": signals})
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
    return applied, compliance_warnings


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
        if bp._is_needs_context(status):
            bp.set_status(manifest, tid, bp.ABANDONED,
                          reason=f"{bp.ABANDON_MISSING_CONTEXT}: {it.get('reason') or 'repair needs more context'}",
                          missingSymbols=it.get("missingSymbols") or [])
            continue
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


def _record_llm_telemetry(
    run_dir: Path, *, run_id: str, role: str, rnd: int,
    request: dict | None, response: dict | None, target_ids: list,
    duration_seconds: float,
) -> None:
    """Register FinOps telemetry for ONE handoff + print a one-line console summary.

    Never raises: a telemetry failure (bad payload, disk error) must never break
    the run, so everything here is best-effort and swallowed with a warning."""
    try:
        model = config.model_for_role(role)
    except Exception:  # noqa: BLE001 — rol desconocido no debe romper la telemetría
        model = None
    try:
        recorded = cost_telemetry.record_handoff(
            run_dir, run_id=run_id, role=role, rnd=rnd,
            request=request or {}, response=response or {},
            target_ids=list(target_ids or []),
            duration_seconds=duration_seconds, model=model)
    except Exception as exc:  # noqa: BLE001
        _print(f"[finops] no se pudo registrar telemetría ({role} r{rnd}): {exc}")
        return
    if not recorded:
        return
    tin = sum(i["tokens_in"] for i in recorded)
    tout = sum(i["tokens_out"] for i in recorded)
    cost = round(sum(i["cost_usd"] for i in recorded), 6)
    src = recorded[0].get("source")
    flag = " (estimado)" if recorded[0].get("estimated") else ""
    _print(f"[finops] {role} r{rnd}: {len(recorded)} target(s) · in={tin} out={tout} tok · "
           f"${cost:.4f} · {duration_seconds:.3f}s · {src}{flag}")


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
    paths = RunPaths(state_dir, run_id)        # single source of truth for run I/O
    run_dir = paths.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = bp.new_manifest(run_id, str(repo), generation_mode="handoff-batch",
                               batch_size=batch_size, max_repair_rounds=max_repair_rounds)
    _save_manifest(run_dir, manifest)

    # ── Volumetría del workspace (demostración de eficiencia de contexto) ─────────
    # Tamaño físico real del repo SUT analizado, medido ANTES de generar nada. Se
    # compara al final contra el contexto realmente enviado al LLM. Tolerante a
    # fallos de FS por construcción (workspace_volumetry nunca levanta).
    repo_bytes = workspace_volumetry.directory_size_bytes(repo)
    _print(f"[efficiency] repo SUT real: {workspace_volumetry.human_mb(repo_bytes)} "
           f"(excluye .git/target/node_modules/build) → {repo}")

    processed = set(one_cycle._processed_ids(state_dir))
    pending_count = sum(1 for it in plan_items if it.get("targetId") not in processed)
    estimated_batches = (pending_count + batch_size - 1) // batch_size if pending_count else 0
    _print(
        f"[batch] batch_size={batch_size} | targets pendientes={pending_count} | "
        f"batches estimados≈{estimated_batches}"
    )
    _print(
        "[batch] guía batch_size:  "
        "1-3 = debug/prueba inicial (lento, controlado)  |  "
        "5 = recomendado proyectos medianos  |  "
        "10 = targets simples (DTOs, value objects)  |  "
        "15-20 = proyectos grandes (riesgo: el freno automático actúa si el pass-rate baja)"
    )
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
        batch_dir = paths.batch_dir(batch_id)
        batch_dir.mkdir(parents=True, exist_ok=True)
        paths.assert_consistent(batch_id)  # guard: no run-XXXX vs run-XXXXS drift
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

        # ── pre-flight evidence gate (task 2) ─────────────────────────────────────
        # Decide, BEFORE any LLM call, which targets carry enough evidence to be
        # generated batch-only. Those that do not are SKIPPED with an audit reason
        # and never sent to the model (avoids a wasted handoff + a G2 rollback).
        enriched = _enrich_targets_with_imports(targets, state_dir=state_dir, repo=repo)
        request_targets: list[dict] = []
        preflight_skipped: list[dict] = []
        preflight_sendable: list[dict] = []
        for t in enriched:
            reason = bp.preflight_evidence_gate(t)
            if reason:
                tid = t.get("targetId")
                bp.set_status(manifest, tid, bp.SKIPPED, reason=reason)
                processed.add(tid)
                one_cycle.mark_processed(state_dir, tid)
                preflight_skipped.append({"targetId": tid, "sut": t.get("sut", ""),
                                          "reason": reason})
            else:
                preflight_sendable.append({"targetId": t.get("targetId"),
                                           "sut": t.get("sut", "")})
                request_targets.append(t)
        # Always write preflight-result.json so the user sees the complete picture
        # for this batch: which targets were sent (sendable) and which were filtered (skipped).
        _write_json(paths.preflight_result(batch_id), {
            "batchId": batch_id,
            "sendable": preflight_sendable,
            "skipped": preflight_skipped,
        })
        if preflight_skipped:
            _print(f"[preflight] {len(preflight_skipped)} target(s) saltados por falta "
                   f"de evidencia (no se envían al LLM).")
        sendable_ids = [t.get("targetId") for t in request_targets]
        if not sendable_ids:
            # Every target in this batch lacked evidence → nothing to generate.
            _write_json(paths.validation_result(batch_id),
                        {"batchId": batch_id, "rc": 0,
                         "counts": _empty_validation_counts(),
                         "targets": [], "applied": {},
                         "preflightSkipped": preflight_skipped})
            _save_manifest(run_dir, manifest)
            continue

        # ── generation handoff ──────────────────────────────────────────────────
        req = bp.build_generation_request(run_id, batch_id, request_targets,
                                          batch_size=len(request_targets))
        req_path = paths.request_generation(batch_id)
        resp_path = paths.response_generation(batch_id)
        _write_json(req_path, req)
        _save_manifest(run_dir, manifest)

        _gen_t0 = time.perf_counter()
        outcome, resp = _wait_for_response(
            req_path, resp_path, state_path=state_path, manifest=manifest,
            kind="generation", batch_id=batch_id)
        _gen_dur = time.perf_counter() - _gen_t0
        if outcome == "quit":
            manifest["status"] = "STOPPED"
            _save_manifest(run_dir, manifest)
            return RC_STOPPED
        if outcome == "skip":
            for tid in sendable_ids:
                bp.set_status(manifest, tid, bp.SKIPPED, reason="batch skipped by user")
                processed.add(tid)
                one_cycle.mark_processed(state_dir, tid)
            _save_manifest(run_dir, manifest)
            continue

        # Envelope first — the ONLY batch-level abort: a malformed/foreign response
        # wrapper we cannot trust at all. A per-item problem never aborts here.
        try:
            bp.validate_generation_envelope(resp, batch_id=batch_id)
        except bp.BatchResponseError as exc:
            _print(f"[batch] response-generation inválida (envelope): {exc}; salto el batch.")
            for tid in sendable_ids:
                bp.set_status(manifest, tid, bp.GENERATION_FAILED, note=str(exc))
                processed.add(tid)
                one_cycle.mark_processed(state_dir, tid)
            # Always write validation-result.json — even on an unparseable response.
            _write_json(paths.validation_result(batch_id), {
                "batchId": batch_id, "rc": 1,
                "counts": _empty_validation_counts(omitted=len(sendable_ids)),
                "targets": [{"targetId": tid, "status": bp.GENERATION_FAILED,
                           "reason": bp.HYDRATION_COMPLETION_SCHEMA_ERROR,
                           "message": str(exc)} for tid in sendable_ids],
                "applied": {}, "preflightSkipped": preflight_skipped})
            _save_manifest(run_dir, manifest)
            continue

        # Python builds the canonical patchDescriptor per target (the LLM no longer
        # ships it); a single invalid target fails ONLY itself instead of dragging the
        # whole batch into GENERATION_FAILED.
        hydrated = bp.hydrate_generation_response(req, resp)
        gen_targets = hydrated["targets"]
        gen_diagnostics = hydrated["diagnostics"]
        gen_counts = hydrated["counts"]

        # FinOps: contabiliza tokens + costo + duración de este handoff de generación
        # (round 0). Tolerante: cualquier fallo de telemetría jamás corta el run.
        _record_llm_telemetry(run_dir, run_id=run_id, role="generation", rnd=0,
                              request=req, response=resp, target_ids=sendable_ids,
                              duration_seconds=_gen_dur)

        fixture_plans = {t.get("targetId"): (t.get("fixturePlan") or {})
                         for t in request_targets}
        applied, compliance_warnings = _process_generation(
            gen_targets, manifest, state_dir=state_dir, repo=repo,
            batch_ids=sendable_ids, fixture_plans=fixture_plans)
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
        test_counts = _classify_batch(manifest, repo=repo, applied=applied, rc=rc_tests)
        counts = _validation_counts(gen_counts, test_counts, applied=len(applied))
        _write_json(paths.validation_result(batch_id),
                    {"batchId": batch_id, "rc": rc_tests, "counts": counts,
                     "targets": _validation_targets(manifest, sendable_ids, gen_diagnostics),
                     "applied": applied, "preflightSkipped": preflight_skipped,
                     "fixtureComplianceWarnings": compliance_warnings})
        _save_manifest(run_dir, manifest)

        # ── repair rounds (only failures, strict admission — task 6) ─────────────
        had_compile = counts["compile"] > 0
        for rnd in range(1, max_repair_rounds + 1):
            failing = bp.failing_target_ids(manifest, sendable_ids)
            if not failing:
                break
            failed_payload = _failed_items_for_repair(manifest, state_dir=state_dir, repo=repo,
                                                       batch_ids=sendable_ids, applied=applied)

            # Repair admission gate: drop items not worth another handoff instead of
            # spending tokens on them. Track the failure signature per round so a
            # recurring identical cause is abandoned (REPEATED_FAILURE_SIGNATURE).
            admissible: list[dict] = []
            for fi in failed_payload:
                tid = fi["targetId"]
                prev_sig = manifest["targets"].get(tid, {}).get("lastFailureSignature")
                ok, reason = bp.repair_admission(fi, previous_signature=prev_sig)
                manifest["targets"][tid]["lastFailureSignature"] = fi["failureSignature"]
                # An item admitted on a weak (logs-less) summary gets at most one
                # round: if it already consumed a round, abandon instead of guessing.
                if ok and bp.weak_diagnostics(fi) and \
                        int(manifest["targets"][tid].get("repairRounds", 0)) >= 1:
                    ok, reason = False, bp.ABANDON_NO_ACTIONABLE_LOGS
                if ok:
                    admissible.append(fi)
                else:
                    bp.set_status(manifest, tid, bp.ABANDONED, reason=reason)
            if not admissible:
                _print("[repair] sin items accionables; no se llama al LLM "
                       "(ahorro de tokens).")
                _save_manifest(run_dir, manifest)
                break

            requested_ids = {fi["targetId"] for fi in admissible}
            # Orchestrator-owned G7 anti-loop triplets, derived from the compile-error
            # index — forwarded to the patcher so the repair is not blocked with
            # G7_REPAIR_WITHOUT_TRIPLET (the model never produces these).
            repair_triplets = _repair_triplets(admissible, state_dir=state_dir)
            rreq = bp.build_repair_request(run_id, batch_id, rnd, admissible)
            rreq_path = paths.request_repair(batch_id, rnd)
            rresp_path = paths.response_repair(batch_id, rnd)
            _write_json(rreq_path, rreq)
            for tid in requested_ids:
                bp.set_status(manifest, tid, bp.REPAIR_REQUESTED)
                bp.bump_repair_round(manifest, tid)
            _save_manifest(run_dir, manifest)

            _rep_t0 = time.perf_counter()
            outcome, rresp = _wait_for_response(
                rreq_path, rresp_path, state_path=state_path, manifest=manifest,
                kind="repair", batch_id=batch_id, repair_round=rnd)
            _rep_dur = time.perf_counter() - _rep_t0
            if outcome == "quit":
                manifest["status"] = "STOPPED"
                _save_manifest(run_dir, manifest)
                return RC_STOPPED
            if outcome == "skip":
                break
            # FinOps: tokens + costo + duración del handoff de repair (round = rnd).
            _record_llm_telemetry(run_dir, run_id=run_id, role="repair", rnd=rnd,
                                  request=rreq, response=rresp,
                                  target_ids=sorted(requested_ids),
                                  duration_seconds=_rep_dur)
            try:
                ritems = bp.validate_repair_response(
                    rresp,
                    requested_ids,
                    batch_id=batch_id,
                    repair_round=rnd,
                    requested_items=admissible,
                )
            except bp.BatchResponseError as exc:
                _print(f"[batch] response-repair-r{rnd} inválida: {exc}; corto repair.")
                break

            reapplied = _process_repair(ritems, manifest, state_dir=state_dir, repo=repo,
                                        triplets=repair_triplets)
            rc_tests = _run_tests(repo, state_dir, list(reapplied.values()))
            rcounts = _classify_batch(manifest, repo=repo, applied=reapplied, rc=rc_tests)
            _write_json(paths.validation_result_repair(batch_id, rnd),
                        {"batchId": batch_id, "repairRound": rnd, "rc": rc_tests,
                         "counts": rcounts, "reapplied": reapplied})
            # Targets still failing AND out of rounds → ABANDON.
            for tid in bp.failing_target_ids(manifest, sendable_ids):
                if bp.should_abandon(manifest, tid, max_repair_rounds):
                    bp.set_status(manifest, tid, bp.ABANDONED, note="exceeded maxRepairRounds")
            _save_manifest(run_dir, manifest)

            # NO_PROGRESS guard: a round that re-applied nothing (the model skipped/
            # failed every item, or every re-apply was rejected) will not improve on
            # the next handoff → abandon the requested items that are not yet resolved
            # (still failing OR stuck in REPAIR_REQUESTED) instead of spending another
            # round.
            if not reapplied:
                for tid in requested_ids:
                    if manifest["targets"].get(tid, {}).get("status") not in bp.TERMINAL_STATES:
                        bp.set_status(manifest, tid, bp.ABANDONED, reason=bp.ABANDON_NO_PROGRESS)
                _save_manifest(run_dir, manifest)
                break

        # Anything still failing after the rounds is abandoned.
        for tid in bp.failing_target_ids(manifest, sendable_ids):
            bp.set_status(manifest, tid, bp.ABANDONED, note="still failing after repair rounds")

        # Mark every target in the batch processed so the next batch advances.
        for tid in batch_ids:
            processed.add(tid)
            one_cycle.mark_processed(state_dir, tid)
        budget_enforcer.reset(state_path)

        # ── advance decision ─────────────────────────────────────────────────────
        # Pass rate is over the EFFECTIVE attempted targets. Pre-flight skips never
        # entered sendable_ids; on top of that, a target the model itself answered
        # NEED_MORE_CONTEXT for (SKIPPED/MISSING_CONTEXT) is excluded from the
        # denominator — it did not "fail", the system cleanly recognised it lacked
        # context. Counting it as a failure let an all-NMC batch (0/N) trip the < 50%
        # brake and abort targets in LATER batches that had nothing to do with it.
        effective_ids = [tid for tid in sendable_ids
                         if not _is_need_more_context_skip(manifest["targets"].get(tid, {}))]
        total = len(effective_ids)
        passed = sum(1 for tid in effective_ids
                     if manifest["targets"].get(tid, {}).get("status") == bp.PASSED)
        nmc = len(sendable_ids) - total
        all_nmc = total == 0 and nmc > 0
        decision = bp.advance_decision(passed, total, had_global_compile_error=had_compile,
                                       all_need_more_context=all_nmc)
        nmc_note = f" ({nmc} need-more-context excluded)" if nmc else ""
        _print(f"[batch] {batch_id}: {passed}/{total} passed{nmc_note} → {decision['action']} "
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
            "--run-id", run_id,   # lets batch_final_report validate path consistency
            "--module", module,
        ])
        if rc_report != 0:
            _print(f"[batch] reporte final no pudo completarse (rc={rc_report}); "
                   "el manifest queda disponible.")

    # ── Métrica de eficiencia de contexto (tabla prominente en STDOUT) ────────────
    # Contexto enviado = bytes reales de los request-*.json (lo único que viajó al
    # LLM). Carpeta de salida = run_dir completo (requests + responses + reportes +
    # logs + telemetría). Todo tolerante a fallos de FS.
    try:
        context_bytes = workspace_volumetry.sum_file_sizes(run_dir.rglob("request-*.json"))
        output_bytes = workspace_volumetry.directory_size_bytes(run_dir)
        _print("\n" + workspace_volumetry.format_efficiency_table(repo_bytes, context_bytes))
        _print(f"[efficiency] carpeta de salida del run: "
               f"{workspace_volumetry.human_mb(output_bytes)} → {run_dir}")
    except Exception as exc:  # noqa: BLE001 — la métrica nunca rompe el cierre del run
        _print(f"[efficiency] no se pudo calcular la métrica de volumetría: {exc}")

    # Resumen FinOps del run (si hubo interacciones contabilizadas).
    try:
        tele_path = cost_telemetry.telemetry_path(run_dir)
        if tele_path.exists():
            tele = _load_json(tele_path)
            _print(f"[finops] run {run_id}: ${tele.get('total_accumulated_usd', 0.0):.4f} · "
                   f"in={tele.get('total_prompt_tokens', 0)} "
                   f"out={tele.get('total_completion_tokens', 0)} tok · "
                   f"{len(tele.get('interactions', []))} interacción(es) · {tele_path.name}")
    except Exception as exc:  # noqa: BLE001
        _print(f"[finops] no se pudo leer el resumen de costos: {exc}")

    _print(f"[batch] manifest: {_manifest_path(run_dir)}")
    _print(f"[batch] totals: {json.dumps(manifest['totals'], ensure_ascii=False)}")
    return final_rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Driver de handoff por batches (handoff-batch).")
    ap.add_argument("--state-dir", required=True, type=Path)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument(
        "--batch-size", type=int, default=None,
        help=(
            "Cantidad de targets enviados al LLM por handoff (default: config.batch_size). "
            "Guía de selección: "
            "1-3 = debug o prueba inicial, control máximo; "
            "5 = recomendado para proyectos medianos (balance velocidad/riesgo); "
            "10 = proyectos con targets simples (DTOs, value objects, enums con métodos); "
            "15-20 = proyectos grandes con muchos targets, pero el freno automático se activa "
            "si el pass-rate por batch es bajo — reducir si la corrida se detiene frecuentemente."
        ),
    )
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
