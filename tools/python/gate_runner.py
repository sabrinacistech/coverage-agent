"""gate_runner.py — deterministic gate evaluator for a candidate patch.

Implements the following gates today:

  G1 (IMPORT_WHITELIST)  — `patch.imports` must be a subset of the context
                           pack's `allowedImports`. Compact packs expose this
                           as `imp` (either a flat array or a prefix-compressed
                           {prefixes, leaves} object).
  G2 (SYMBOL_EVIDENCE)   — every patch method must cite ≥1 evidenceId, and each
                           cited id must exist in some state/symbol-contracts/
                           <fqcn>.json (constructors/methods/builders). A method
                           without evidence, or citing an unknown id, FAILs.
  G5 (STACK_PROFILE)     — context-pack stack must contain no "unknown" values
                           and `blocked` must not be true.
  G6 (TEST_LINT)         — when --test-file is supplied, invoke test_linter.py.
  G7 (FAILURE_MEMORY)    — when the patch is a repair (patchId starts with
                           `repair:` or repairs[] / --repair-attempt is given),
                           cross-check (errorCode, symbolFQN, fixId) hashes
                           against state/failure-memory.json. Blocks when the
                           same triplet already FAILED ≥2 cycles, or when the
                           same testCaseId has accumulated >3 attempts.

Gates not yet implemented are reported with status "NOT_IMPLEMENTED" together
with a reason; never a false PASS.

Output JSON:

  {
    "schemaVersion": 1,
    "status": "PASS|FAIL|NOT_IMPLEMENTED",
    "gates": {
      "G1": {"status": "...", "blockedReason"?, ...},
      ...,
      "G8": {"status": "NOT_IMPLEMENTED", "reason": "..."}
    },
    "blockedReason": "G1_IMPORT_NOT_WHITELISTED|G5_STACK_UNKNOWN|G6_LINTER_FAIL|null"
  }

Persisted to state/_summaries/gates.json.

Usage
-----
  python tools/python/gate_runner.py --state state \\
      --patch patch.json --context-pack pack.json [--test-file path]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun  # noqa: E402

_NOT_IMPLEMENTED: dict[str, str] = {
    "G4": "fixture/strategy validation not implemented yet",
}

# G7 thresholds — match the rules declared in agents/repair-agent.md.
_G7_MAX_FAILED_ATTEMPTS = 2   # same (errorCode, symbolFQN, fixId) hash
_G7_MAX_TESTCASE_ATTEMPTS = 3  # cumulative attempts for one testCaseId

# G8 thresholds — finiteness by construction. retry-policy.md / MASTER_PROMPT.md
# both pin these: two consecutive zero-delta cycles → halt; latest compile-fail
# rate above 50% → halt.
_G8_MAX_ZERO_DELTA_CYCLES = 2
_G8_MAX_COMPILE_FAIL_RATE = 0.5


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _patch_imports(patch: dict) -> list[str]:
    if isinstance(patch.get("imports"), list):
        return [str(s) for s in patch["imports"]]
    if isinstance(patch.get("allowedImports"), list):
        return [str(s) for s in patch["allowedImports"]]
    return []


def _expand_compact_imports(imp: object) -> list[str]:
    if isinstance(imp, list):
        return [str(s) for s in imp]
    if isinstance(imp, dict):
        prefixes = imp.get("prefixes", []) or []
        leaves = imp.get("leaves", {}) or {}
        result: list[str] = []
        for key, vals in leaves.items():
            try:
                idx = int(key)
                prefix = prefixes[idx]
            except (ValueError, IndexError, TypeError):
                prefix = key
            for leaf in vals:
                if prefix:
                    result.append(f"{prefix}.{leaf}" if leaf else prefix)
                else:
                    result.append(leaf)
        return result
    return []


def _context_pack_imports(pack: dict) -> list[str]:
    if isinstance(pack.get("allowedImports"), list):
        return [str(s) for s in pack["allowedImports"]]
    if "imp" in pack:
        return _expand_compact_imports(pack["imp"])
    return []


def _context_pack_stack(pack: dict) -> tuple[list[str], bool]:
    """Return (stack_value_strings, blocked_flag)."""
    blocked = bool(pack.get("blocked", False) or pack.get("blk", False))
    stack_values: list[str] = []
    if isinstance(pack.get("stack"), dict):
        for v in pack["stack"].values():
            if v is None:
                continue
            stack_values.append(str(v))
    elif isinstance(pack.get("stk"), list):
        for v in pack["stk"]:
            if v is None:
                continue
            stack_values.append(str(v))
    return stack_values, blocked


# ── gate implementations ──────────────────────────────────────────────────────

def gate_g1(patch: dict, pack: dict) -> dict:
    patch_imports = set(_patch_imports(patch))
    allowed = set(_context_pack_imports(pack))
    if not patch_imports:
        return {"status": "PASS", "detail": "patch declares no imports"}
    missing = sorted(patch_imports - allowed)
    if missing:
        return {
            "status": "FAIL",
            "blockedReason": "G1_IMPORT_NOT_WHITELISTED",
            "missing": missing,
        }
    return {"status": "PASS", "checked": len(patch_imports)}


def _collect_contract_evidence(state_dir: Path) -> set[str]:
    """Collect every evidenceId declared across state/symbol-contracts/*.json.

    Pulls ids from constructors[], methods[] and builders[] — each carries an
    `evidenceId` per symbol-contract.schema.json. Returns an empty set when the
    directory is absent; gate_g2 then treats any cited id as an orphan (the
    pre-stage guarantees this directory exists before Generation).
    """
    evidence: set[str] = set()
    contracts_dir = state_dir / "symbol-contracts"
    if not contracts_dir.is_dir():
        return evidence
    for path in sorted(contracts_dir.glob("*.json")):
        try:
            contract = _load_json(path)
        except Exception:
            continue
        if not isinstance(contract, dict):
            continue
        for bucket in ("constructors", "methods", "builders"):
            items = contract.get(bucket)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    eid = item.get("evidenceId")
                    if isinstance(eid, str) and eid:
                        evidence.add(eid)
    return evidence


def gate_g2(patch: dict, state_dir: Path) -> dict:
    """G2 — symbol-evidence gate (anti-hallucination).

    Every test method the patch emits must cite at least one evidenceId, and
    every cited id must exist in some state/symbol-contracts/<fqcn>.json. A
    method with no evidenceIds, or one citing an id absent from the contracts,
    is a hallucinated symbol → FAIL with G2_SYMBOL_WITHOUT_EVIDENCE.

    Patches in the BLOCKED shape (status == "BLOCKED") are SKIPPED: they emit no
    Java and carry no methods. A patch with no `methods` PASSes (nothing to verify).
    """
    if str(patch.get("status", "")).upper() == "BLOCKED":
        return {"status": "SKIPPED", "reason": "patch is BLOCKED — no methods to verify"}

    methods = patch.get("methods")
    if not isinstance(methods, list) or not methods:
        return {"status": "PASS", "reason": "patch declares no test methods", "evidenceChecked": 0}

    known = _collect_contract_evidence(state_dir)

    methods_without_evidence: list[str] = []
    orphan_ids: list[dict] = []
    checked = 0
    for m in methods:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "<anonymous>")
        ev_ids = m.get("evidenceIds")
        if not isinstance(ev_ids, list) or not ev_ids:
            methods_without_evidence.append(name)
            continue
        for eid in ev_ids:
            checked += 1
            if str(eid) not in known:
                orphan_ids.append({"method": name, "evidenceId": str(eid)})

    if methods_without_evidence or orphan_ids:
        return {
            "status": "FAIL",
            "blockedReason": "G2_SYMBOL_WITHOUT_EVIDENCE",
            "methodsWithoutEvidence": methods_without_evidence,
            "orphanEvidenceIds": orphan_ids,
            "contractsAvailable": len(known),
        }
    return {"status": "PASS", "evidenceChecked": checked, "contractsAvailable": len(known)}


def gate_g5(pack: dict) -> dict:
    stack_values, blocked = _context_pack_stack(pack)
    if blocked:
        return {
            "status": "FAIL",
            "blockedReason": "G5_STACK_UNKNOWN",
            "reason": "context-pack is blocked",
        }
    unknown = [v for v in stack_values if v.lower() == "unknown"]
    if unknown:
        return {
            "status": "FAIL",
            "blockedReason": "G5_STACK_UNKNOWN",
            "unknownValues": unknown,
        }
    return {"status": "PASS", "checked": len(stack_values)}


def _failure_hash(error_code: str, symbol_fqn: str, fix_id: str) -> str:
    """Canonical hash for the (errorCode, symbolFQN, fixId) triplet used by
    failure-memory.json. Matches the convention declared in MASTER_PROMPT.md
    "G7 Failure memory: hash(errorCode, symbolFQN, fixId)"."""
    h = hashlib.sha256()
    h.update(error_code.encode("utf-8"))
    h.update(b"\x00")
    h.update(symbol_fqn.encode("utf-8"))
    h.update(b"\x00")
    h.update(fix_id.encode("utf-8"))
    return h.hexdigest()


def _load_failure_memory(state_dir: Path) -> list[dict]:
    path = state_dir / "failure-memory.json"
    if not path.exists():
        return []
    try:
        data = _load_json(path)
    except Exception:
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _patch_repair_attempts(patch: dict) -> list[dict]:
    """Extract (errorCode, symbolFQN, fixId) triplets declared by the patch.

    A repair patch can declare them explicitly under `repairs[]` (preferred)
    or, for legacy patches, inside `repairOf` metadata. Items missing any of
    the three fields are skipped.
    """
    raw = patch.get("repairs")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        error_code = str(item.get("errorCode") or "").strip()
        symbol_fqn = str(item.get("symbolFQN") or item.get("symbol") or "").strip()
        fix_id = str(item.get("fixId") or "").strip()
        if not (error_code and symbol_fqn and fix_id):
            continue
        out.append({"errorCode": error_code, "symbolFQN": symbol_fqn, "fixId": fix_id})
    return out


def _parse_cli_attempts(raw: list[str] | None) -> list[dict]:
    """Parse --repair-attempt "errorCode|symbolFQN|fixId" strings."""
    if not raw:
        return []
    out: list[dict] = []
    for item in raw:
        parts = item.split("|")
        if len(parts) != 3:
            continue
        error_code, symbol_fqn, fix_id = (p.strip() for p in parts)
        if not (error_code and symbol_fqn and fix_id):
            continue
        out.append({"errorCode": error_code, "symbolFQN": symbol_fqn, "fixId": fix_id})
    return out


def gate_g7(
    patch: dict,
    state_dir: Path,
    cli_attempts: list[dict] | None = None,
) -> dict:
    """Anti-loop gate. Blocks a repair patch when failure-memory.json shows
    the same fix triplet has FAILED ≥ _G7_MAX_FAILED_ATTEMPTS cycles, or when
    the patch's testCaseId already has > _G7_MAX_TESTCASE_ATTEMPTS attempts.
    """
    patch_id = str(patch.get("patchId") or "")
    is_repair = patch_id.startswith("repair:") or bool(patch.get("repairOf"))
    attempts = _patch_repair_attempts(patch) + (cli_attempts or [])

    if not is_repair and not attempts:
        return {
            "status": "SKIPPED",
            "reason": "patch is not a repair (no patchId 'repair:' prefix, no repairs[], no --repair-attempt)",
        }

    if is_repair and not attempts:
        return {
            "status": "FAIL",
            "blockedReason": "G7_REPAIR_WITHOUT_TRIPLET",
            "reason": (
                "repair patch must declare repairs[] = "
                "[{errorCode, symbolFQN, fixId}, ...] or be invoked with --repair-attempt"
            ),
        }

    memory = _load_failure_memory(state_dir)
    if not memory:
        return {
            "status": "PASS",
            "reason": "no prior failure-memory.json entries",
            "attemptsChecked": len(attempts),
        }

    by_hash: dict[str, dict] = {}
    for entry in memory:
        if not isinstance(entry, dict):
            continue
        h = entry.get("hash")
        if isinstance(h, str):
            by_hash[h] = entry

    hits: list[dict] = []
    for triplet in attempts:
        h = _failure_hash(triplet["errorCode"], triplet["symbolFQN"], triplet["fixId"])
        entry = by_hash.get(h)
        if not entry:
            continue
        last = str(entry.get("lastResult", "")).upper()
        attempt_count = int(entry.get("attempts") or 0)
        if last == "FAILED" and attempt_count >= _G7_MAX_FAILED_ATTEMPTS:
            hits.append({
                "hash": h,
                "errorCode": triplet["errorCode"],
                "symbolFQN": triplet["symbolFQN"],
                "fixId": triplet["fixId"],
                "attempts": attempt_count,
                "lastResult": last,
            })

    test_case_id = str(patch.get("testCaseId") or patch.get("originalTestCaseId") or "")
    testcase_attempts = 0
    if test_case_id:
        for entry in memory:
            if not isinstance(entry, dict):
                continue
            if entry.get("testCaseId") == test_case_id:
                testcase_attempts += int(entry.get("attempts") or 0)
        if testcase_attempts > _G7_MAX_TESTCASE_ATTEMPTS:
            return {
                "status": "FAIL",
                "blockedReason": "G7_TESTCASE_OVER_BUDGET",
                "testCaseId": test_case_id,
                "attempts": testcase_attempts,
                "limit": _G7_MAX_TESTCASE_ATTEMPTS,
            }

    if hits:
        return {
            "status": "FAIL",
            "blockedReason": "G7_FAILURE_MEMORY_HIT",
            "hits": hits,
            "threshold": _G7_MAX_FAILED_ATTEMPTS,
        }

    return {
        "status": "PASS",
        "attemptsChecked": len(attempts),
        "testCaseAttempts": testcase_attempts,
    }


def _resolve_compact_pack(state_dir: Path, test_file: Path) -> Path | None:
    """Best-effort map of a test file to its compact context-pack by safe-FQCN.

    `src/test/java/com/acme/FooServiceTest.java` → `com.acme.FooService` →
    `state/context-packs-compact/com.acme.FooService.json` (dots are preserved
    by context_pack_builder.safe_fqcn). Used only as a fallback when the caller
    does not pass the pack path explicitly. Returns the candidate path (caller
    checks existence) or None when the simple name cannot be derived.
    """
    stem = test_file.stem
    for suffix in ("Tests", "Test", "IT"):
        if stem.endswith(suffix):
            simple = stem[: -len(suffix)]
            break
    else:
        simple = stem
    if not simple:
        return None
    parts = test_file.as_posix().split("/")
    pkg_parts: list[str] = []
    if "java" in parts:
        idx = len(parts) - 1 - parts[::-1].index("java")
        pkg_parts = parts[idx + 1 : -1]
    fqcn = ".".join([*pkg_parts, simple]) if pkg_parts else simple
    return state_dir / "context-packs-compact" / f"{fqcn}.json"


def gate_g6(
    state_dir: Path,
    test_file: Path | None,
    context_pack_path: Path | None = None,
) -> dict:
    if test_file is None:
        return {"status": "SKIPPED", "reason": "no --test-file supplied"}
    linter = HERE / "test_linter.py"
    if not linter.exists():
        return {"status": "NOT_IMPLEMENTED", "reason": "test_linter.py missing"}
    if not test_file.exists():
        return {
            "status": "FAIL",
            "blockedReason": "G6_LINTER_FAIL",
            "reason": f"test file not found: {test_file}",
        }
    # G6-quality (skills/11-quality/) corre por default desde test_linter.py.
    # No se pasa --no-quality-checks: queremos los 14 checks activos siempre.
    # Use the caller-supplied context-pack when available; otherwise fall back
    # to the compact pack derived by safe-FQCN (A2: never the verbose dir / the
    # broken stem.replace("Test", "") that missed every safe-FQCN filename).
    cp_path = context_pack_path or _resolve_compact_pack(state_dir, test_file)
    cmd = [
        sys.executable,
        str(linter),
        "--test-file", str(test_file),
        "--whitelist", str(state_dir / "import-whitelist.json"),
        "--contracts", str(state_dir / "symbol-contracts"),
        "--stack-profile", str(state_dir / "stack-profile.json"),
        "--index", str(state_dir / "index"),
    ]
    if cp_path is not None and cp_path.exists():
        cmd.extend(["--context-pack", str(cp_path)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        return {
            "status": "FAIL",
            "blockedReason": "G6_LINTER_FAIL",
            "reason": f"linter invocation failed: {exc}",
        }

    # Parse linter report and persist violations for downstream consumers
    # (repair-agent reads state/linter-violations.json and maps each entry
    # against repair-rules/quality.rules).
    violations: list[dict] = []
    try:
        report = json.loads(proc.stdout) if proc.stdout.strip() else {}
        violations = report.get("violations") or []
    except json.JSONDecodeError:
        # Linter crashed before producing JSON; keep violations empty and
        # fall back to the tail in the FAIL branch below.
        report = {}

    out_path = state_dir / "linter-violations.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "testFile": str(test_file),
        "violations": violations,
        "violationCount": len(violations),
    }
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
        return {
            "status": "FAIL",
            "blockedReason": "G6_LINTER_FAIL",
            "exitCode": proc.returncode,
            "violationCount": len(violations),
            "violationsPath": str(out_path),
            "tail": tail,
        }
    return {"status": "PASS", "violationCount": 0, "violationsPath": str(out_path)}


def gate_g8(state_dir: Path) -> dict:
    """G8 — finiteness gate. Halts the cycle when convergence stalls.

    Reads state/execution-state.json and blocks if either:
      - consecutiveZeroDeltaCycles >= _G8_MAX_ZERO_DELTA_CYCLES (no JaCoCo
        progress for two cycles in a row), or
      - the most recent entry in compileFailRateWindow exceeds
        _G8_MAX_COMPILE_FAIL_RATE (the cycle is producing more compile
        failures than passes — keep reparing and you just burn budget).

    Missing/empty execution-state is treated as PASS rather than NOT_IMPLEMENTED:
    a fresh repo has not produced enough cycles to observe a stall.
    """
    path = state_dir / "execution-state.json"
    if not path.exists():
        return {"status": "PASS", "detail": "execution-state.json missing — first cycle"}
    try:
        state = _load_json(path)
    except Exception as exc:
        return {
            "status": "FAIL",
            "blockedReason": "G8_STATE_UNREADABLE",
            "reason": f"cannot load execution-state.json: {exc}",
        }

    zero_delta = int(state.get("consecutiveZeroDeltaCycles", 0) or 0)
    if zero_delta >= _G8_MAX_ZERO_DELTA_CYCLES:
        return {
            "status": "FAIL",
            "blockedReason": "G8_NO_DELTA",
            "consecutiveZeroDeltaCycles": zero_delta,
            "threshold": _G8_MAX_ZERO_DELTA_CYCLES,
        }

    window = state.get("compileFailRateWindow") or []
    if isinstance(window, list) and window:
        try:
            latest = float(window[-1])
        except (TypeError, ValueError):
            latest = 0.0
        if latest > _G8_MAX_COMPILE_FAIL_RATE:
            return {
                "status": "FAIL",
                "blockedReason": "G8_COMPILE_FAIL_RATE",
                "latestRate": latest,
                "threshold": _G8_MAX_COMPILE_FAIL_RATE,
            }
    return {
        "status": "PASS",
        "consecutiveZeroDeltaCycles": zero_delta,
        "compileFailRateLatest": (float(window[-1]) if window else 0.0),
    }


def _try_auto_repair(state_dir: Path, test_file: Path) -> dict:
    """Invoke repair_dispatch.py and return a compact report.

    Bridges G6 (linter) with phase 10a (deterministic repair). When G6 fails,
    the violations land in state/linter-violations.json — exactly what
    repair_dispatch consumes. Anything the dispatcher cannot handle is
    surfaced as `escalated` so the caller can pass it to the LLM repair-agent.
    """
    dispatcher = HERE / "repair_dispatch.py"
    if not dispatcher.exists():
        return {"available": False, "reason": "repair_dispatch.py missing"}
    whitelist = state_dir / "import-whitelist.json"
    cmd = [
        sys.executable, str(dispatcher),
        "--state", str(state_dir),
        "--test-file", str(test_file),
    ]
    if whitelist.exists():
        cmd += ["--whitelist", str(whitelist)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out: dict = {"available": True, "exitCode": proc.returncode}
    report_path = state_dir / "_summaries" / "repair-dispatch.json"
    if report_path.exists():
        try:
            with report_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            counts = payload.get("counts", {})
            out["repaired"] = counts.get("repaired", 0)
            out["escalated"] = counts.get("escalated", 0)
            out["skipped"] = counts.get("skipped", 0)
            out["reportPath"] = str(report_path)
        except (OSError, json.JSONDecodeError) as exc:
            out["reportError"] = f"{exc.__class__.__name__}: {exc}"
    return out


# ── orchestration (shared by CLI and test_patch_applier.py) ─────────────────────

def evaluate_gates(
    patch: dict,
    pack: dict,
    state_dir: Path,
    test_file: Path | None = None,
    cli_attempts: list[dict] | None = None,
    auto_repair: bool = False,
    context_pack_path: Path | None = None,
) -> dict:
    """Run gates G1..G8 over a candidate patch and return the report dict.

    Single source of the gate aggregation logic, shared by this module's CLI and
    by `test_patch_applier.py` (which folds gate evaluation into the only code
    path that physically writes Java — making the gates impossible to bypass).
    Persists the report to `<state>/_summaries/gates.json` as a side effect.

    The report `status` is "FAIL" if any blocking gate (G1/G2/G5/G6/G7/G8) fails,
    otherwise "PASS" when at least one gate passed, else "NOT_IMPLEMENTED".
    """
    gates: dict[str, dict] = {}
    gates["G1"] = gate_g1(patch, pack)
    gates["G2"] = gate_g2(patch, state_dir)
    gates["G4"] = {"status": "NOT_IMPLEMENTED", "reason": _NOT_IMPLEMENTED["G4"]}
    gates["G5"] = gate_g5(pack)
    gates["G6"] = gate_g6(state_dir, test_file, context_pack_path)
    if auto_repair and gates["G6"].get("status") == "FAIL" and test_file is not None:
        dispatch_report = _try_auto_repair(state_dir, test_file)
        gates["G6"]["autoRepair"] = dispatch_report
        # Re-run G6 if dispatch actually changed the file.
        if dispatch_report.get("repaired"):
            gates["G6"] = gate_g6(state_dir, test_file, context_pack_path)
            gates["G6"]["autoRepair"] = dispatch_report
    gates["G7"] = gate_g7(patch, state_dir, cli_attempts)
    gates["G8"] = gate_g8(state_dir)

    blocked_reason: str | None = None
    for key in ("G1", "G2", "G5", "G6", "G7", "G8"):
        g = gates.get(key, {})
        if g.get("status") == "FAIL":
            blocked_reason = g.get("blockedReason") or f"{key}_FAIL"
            break

    if blocked_reason:
        status = "FAIL"
    elif any(g.get("status") == "PASS" for g in gates.values()):
        status = "PASS"
    else:
        status = "NOT_IMPLEMENTED"

    report = {
        "schemaVersion": 1,
        "status": status,
        "gates": gates,
        "blockedReason": blocked_reason,
    }

    summaries = state_dir / "_summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    out_path = summaries / "gates.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    return report


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run deterministic gates G1..G8 over a candidate patch.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--state", default="state", help="State directory (default: state).")
    ap.add_argument("--patch", required=True, help="Path to the patch JSON.")
    ap.add_argument(
        "--context-pack",
        required=True,
        dest="context_pack",
        help="Path to the context-pack JSON (full or compact).",
    )
    ap.add_argument("--test-file", default=None, help="Path to the Java test file for G6.")
    ap.add_argument(
        "--auto-repair",
        action="store_true",
        help=(
            "When G6 fails, invoke repair_dispatch.py to apply deterministic "
            "rules (10a) before reporting failure. Only violations the "
            "dispatcher escalates remain in G6's blockedReason — everything "
            "else is auto-fixed and G6 re-runs."
        ),
    )
    ap.add_argument(
        "--repair-attempt",
        action="append",
        default=None,
        metavar="errorCode|symbolFQN|fixId",
        help=(
            "G7: declare a (errorCode, symbolFQN, fixId) triplet for this "
            "repair attempt. May be supplied multiple times. Alternative to "
            "embedding repairs[] inside the patch JSON."
        ),
    )
    args = ap.parse_args()

    state_dir = Path(args.state).resolve()

    try:
        patch = _load_json(Path(args.patch))
    except Exception as exc:
        print(f"[FAIL] cannot load patch: {exc}", file=sys.stderr)
        return 2
    try:
        pack = _load_json(Path(args.context_pack))
    except Exception as exc:
        print(f"[FAIL] cannot load context-pack: {exc}", file=sys.stderr)
        return 2

    test_file = Path(args.test_file).resolve() if args.test_file else None

    cli_attempts = _parse_cli_attempts(args.repair_attempt)

    report = evaluate_gates(
        patch,
        pack,
        state_dir,
        test_file=test_file,
        cli_attempts=cli_attempts,
        auto_repair=args.auto_repair,
        context_pack_path=Path(args.context_pack).resolve(),
    )

    print(json.dumps(report, indent=2, ensure_ascii=False))

    return 0 if report["status"] != "FAIL" else 1


if __name__ == "__main__":
    with _TimedRun("gate_runner") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
