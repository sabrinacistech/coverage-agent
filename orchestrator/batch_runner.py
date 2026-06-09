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

# Exit codes.
RC_DONE = 0
RC_STOPPED = 6      # advance rule said stop (too many failures) or user quit
RC_NO_TARGETS = 7   # nothing pending — mirrors one_cycle/cycle_loop

# narrow_test_runner returns 2 for its own infra failures (no pom.xml / mvn not on
# PATH) and otherwise propagates Maven's exit code (1 on test failure). We treat
# its 2 as "tests not run" so a missing Maven never looks like a compile failure.
_RC_TESTS_NOT_RUN = 2


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

def _apply_patch(patch: dict, *, state_dir: Path, repo: Path) -> int:
    """Apply ONE patch descriptor through the sanctioned patcher (gates + budget +
    Java string-literal safety by construction). Returns its exit code
    (0 ok · 2 budget · 3 gate/perimeter · other = patch failed)."""
    sut = bp_patch_sut(patch)
    pack_path = state_dir / "context-packs" / f"{sut}.json"
    return one_cycle.apply_patch(patch, state_dir=state_dir, repo=repo, context_pack_path=pack_path)


def bp_patch_sut(patch: dict) -> str:
    sut = patch.get("sut", "")
    if isinstance(sut, dict):
        return sut.get("fqcn", "")
    return sut


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
        test_class = applied.get(tid, rec.get("testClass", ""))
        test_file = "src/test/java/" + test_class.replace(".", "/") + ".java" if test_class else ""
        current_src = ""
        if test_file:
            f = repo / test_file
            if f.exists():
                try:
                    current_src = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    current_src = ""
        kind = "COMPILATION_ERROR" if rec.get("status") == bp.COMPILE_FAILED else "TEST_FAILURE"
        items.append({
            "targetId": tid,
            "failureKind": kind,
            "testClass": test_class,
            "testFile": test_file,
            "errorSummary": rec.get("note", kind),
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
        bp.set_status(manifest, tid, bp.GENERATED, testClass=test_class)
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
) -> dict[str, str]:
    """Apply a repair response. Returns {targetId: testClass} for re-applied targets."""
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
        rc = _apply_patch(patch, state_dir=state_dir, repo=repo)
        if rc == 0:
            bp.set_status(manifest, tid, bp.REPAIRED, testClass=test_class)
            applied[tid] = test_class
        else:
            bp.set_status(manifest, tid, bp.PATCH_FAILED, note=f"repair patcher rc={rc}")
    return applied


def run_batches(
    state_dir: Path, repo: Path, *,
    batch_size: int, max_repair_rounds: int, max_batches: int | None,
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
            bp.ensure_target(manifest, t.get("targetId"), sut=t.get("sut", ""), batch_id=batch_id)
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
        req = bp.build_generation_request(run_id, batch_id, targets, batch_size=batch_size)
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
            items = bp.validate_generation_response(resp, targets, batch_id=batch_id)
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
                    rresp, set(failing), batch_id=batch_id, repair_round=rnd)
            except bp.BatchResponseError as exc:
                _print(f"[batch] response-repair-r{rnd} inválida: {exc}; corto repair.")
                break

            reapplied = _process_repair(ritems, manifest, state_dir=state_dir, repo=repo)
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
    args = ap.parse_args(argv)

    batch_size = args.batch_size if args.batch_size is not None else config.batch_size()
    max_repair_rounds = (args.max_repair_rounds if args.max_repair_rounds is not None
                         else config.max_repair_rounds())
    return run_batches(args.state_dir, args.repo, batch_size=batch_size,
                       max_repair_rounds=max_repair_rounds, max_batches=args.max_batches)


if __name__ == "__main__":
    sys.exit(main())
