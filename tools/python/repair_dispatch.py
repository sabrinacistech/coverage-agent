"""repair_dispatch.py — deterministic driver for repair-rules/*.rules.

Post-audit 2026-05-28: closes a gap discovered while validating quality.rules
end-to-end. The repair-agent.md prompt claims "the driver intentó match
determinístico antes de invocarte" — that driver did not exist in Python.
Quality rules with non-escalateToLLM actions were therefore unreachable.

This module loads `state/linter-violations.json` and `state/_summaries/
compiled-rules.json`, matches each violation by `kind`, substitutes
``${field}`` placeholders from the violation record into the action args,
and dispatches to ast_patcher.py for the deterministic actions. Anything
that fails to match or that resolves to escalateToLLM is left in the
escalated subset for the LLM repair-agent.

The contract documented in agents/repair-agent.md is now actually backed by
code: when this dispatcher exits, telemetry shows how many violations the
LLM had to handle versus how many were auto-repaired.

Usage:
    python tools/python/repair_dispatch.py \\
        --state state/ \\
        --test-file src/test/java/...FooTest.java \\
        --whitelist state/import-whitelist.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, atomic_write_json, load_json  # noqa: E402

# Actions that ast_patcher.py knows how to execute deterministically.
# Mirrors the choices in ast_patcher.main(); kept here so dispatch fails
# closed when a rule references something we cannot run.
_AST_PATCHER_ACTIONS: frozenset[str] = frozenset({
    "addImport",
    "removeImport",
    "insertAaaComments",
    "removeUnusedStub",
    "convertMockSutToInjectMocks",
})

_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")


def _interpolate(template: str, violation: dict) -> tuple[str, list[str]]:
    """Replace ``${field}`` placeholders in ``template`` with the matching
    field from ``violation``. Returns (resolved_string, missing_fields). If
    any placeholder cannot be resolved, the original placeholder is kept and
    the field name is recorded — the caller must treat that as a non-match.
    """
    missing: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        val = violation.get(key)
        if val is None or val == "":
            missing.append(key)
            return m.group(0)
        return str(val)

    return _PLACEHOLDER_RE.sub(_sub, template), missing


def _parse_action(fix_template: str) -> tuple[str, str]:
    """Split ``action(arg)`` into (action, arg). Tolerates whitespace and
    nested parens by taking everything between the first `(` and the last
    `)`.
    """
    open_idx = fix_template.find("(")
    close_idx = fix_template.rfind(")")
    if open_idx < 0 or close_idx < open_idx:
        return fix_template, ""
    return fix_template[:open_idx].strip(), fix_template[open_idx + 1:close_idx].strip()


def _index_rules_by_kind(compiled_rules: dict) -> dict[str, list[dict]]:
    """Group rules by their ``errorCode`` field. Each violation's ``kind``
    is the key the dispatcher looks up here; multiple rules may share a
    kind, so values are lists preserving file/line order.

    compile_rules emits a nested structure ``{files: [{rules: [...]}]}``;
    older callers may pass ``{rules: [...]}`` flat. Both are handled.
    """
    out: dict[str, list[dict]] = {}

    def _absorb(rule: dict, source_file: str | None) -> None:
        if not isinstance(rule, dict):
            return
        # Tolerate both snake_case and camelCase from older/newer compilers.
        kind = str(rule.get("errorCode") or rule.get("error_code") or "")
        if not kind:
            return
        # Normalise the rule dict so downstream code can rely on one shape.
        out.setdefault(kind, []).append({
            "errorCode": kind,
            "fixTemplate": rule.get("fixTemplate") or rule.get("fix_template") or "",
            "pattern": rule.get("pattern", ""),
            "action": rule.get("action", ""),
            "args": rule.get("args", ""),
            "file": rule.get("file") or source_file or "",
            "line": rule.get("line", 0),
        })

    # Nested form: {"files": [{"file": "...", "rules": [...]}]}
    for entry in compiled_rules.get("files", []) or []:
        if not isinstance(entry, dict):
            continue
        source_file = entry.get("file")
        for rule in entry.get("rules", []) or []:
            _absorb(rule, source_file)

    # Flat form (fallback): {"rules": [...]}
    for rule in compiled_rules.get("rules", []) or []:
        _absorb(rule, None)

    return out


_AST_PATCHER_TIMEOUT_S = 30


def _run_ast_patcher(
    test_file: Path,
    action: str,
    arg: str,
    whitelist: Path | None,
) -> tuple[bool, bool, str]:
    """Invoke ast_patcher.py for one action.

    Returns (success, changed, raw_output) where ``changed`` reflects whether
    the patcher reported a real file mutation. Both rc==0 and the parsed
    ``changed`` flag must be true for the violation to count as repaired;
    rc==0 with changed==false means the regex didn't match → escalate.
    """
    cmd: list[str] = [
        sys.executable,
        str(HERE / "ast_patcher.py"),
        "--file", str(test_file),
        "--action", action,
    ]
    if arg:
        cmd += ["--arg", arg]
    if whitelist is not None and action == "addImport":
        cmd += ["--whitelist", str(whitelist)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_AST_PATCHER_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        # Slow/hung patcher (e.g. AV-scanned FS on Windows): do not crash the
        # whole dispatch — report failure so the violation escalates to the LLM.
        return False, False, f"ast_patcher timed out after {_AST_PATCHER_TIMEOUT_S}s"
    raw = (proc.stdout or "") + (proc.stderr or "")
    changed = False
    try:
        payload = json.loads(proc.stdout or "{}")
        changed = bool(payload.get("changed", False))
    except json.JSONDecodeError:
        pass
    return proc.returncode == 0, changed, raw


def dispatch(
    state_dir: Path,
    test_file: Path | None,
    whitelist: Path | None,
) -> dict:
    """Apply deterministic rules and return a telemetry-shaped report."""
    violations_path = state_dir / "linter-violations.json"
    rules_path = state_dir / "_summaries" / "compiled-rules.json"

    violations_doc = load_json(violations_path) if violations_path.exists() else {"violations": []}
    rules_doc = load_json(rules_path) if rules_path.exists() else {"rules": []}

    violations = violations_doc.get("violations", []) or []
    rules_by_kind = _index_rules_by_kind(rules_doc)

    repaired: list[dict] = []
    escalated: list[dict] = []
    skipped: list[dict] = []

    for v in violations:
        if not isinstance(v, dict):
            continue
        kind = str(v.get("kind", ""))
        candidates = rules_by_kind.get(kind, [])
        if not candidates:
            escalated.append({**v, "_escalateReason": "no rule matched kind"})
            continue

        applied = False
        for rule in candidates:
            action_name, raw_args = _parse_action(rule.get("fixTemplate") or rule.get("fix_template") or "")
            if action_name == "escalateToLLM":
                escalated.append({**v, "_escalateReason": raw_args or "escalateToLLM(rule)"})
                applied = True
                break
            if action_name not in _AST_PATCHER_ACTIONS:
                escalated.append({**v, "_escalateReason": f"unknown action: {action_name}"})
                applied = True
                break

            resolved_arg, missing = _interpolate(raw_args, v)
            if missing:
                escalated.append({**v, "_escalateReason": f"unresolved placeholders: {missing}"})
                applied = True
                break

            if test_file is None:
                skipped.append({**v, "_skipReason": "no --test-file provided"})
                applied = True
                break
            if not test_file.exists():
                skipped.append({**v, "_skipReason": f"test file missing: {test_file}"})
                applied = True
                break

            ok, changed, out = _run_ast_patcher(test_file, action_name, resolved_arg, whitelist)
            if ok and changed:
                repaired.append({
                    **v,
                    "_action": action_name,
                    "_arg": resolved_arg,
                    "_ruleSource": f"{rule.get('file', '')}:{rule.get('line', 0)}",
                })
            elif ok and not changed:
                # rc==0 but no mutation → regex didn't match. Escalate so the
                # LLM repair-agent can handle the layout we couldn't.
                escalated.append({
                    **v,
                    "_escalateReason": (
                        f"ast_patcher action {action_name} produced no change "
                        f"(arg={resolved_arg!r}) — pattern did not match file"
                    ),
                })
            else:
                escalated.append({**v, "_escalateReason": f"ast_patcher failed: {out.strip()[:200]}"})
            applied = True
            break

        if not applied:
            escalated.append({**v, "_escalateReason": "no applicable rule"})

    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "testFile": str(test_file) if test_file else None,
        "counts": {
            "violations": len(violations),
            "repaired": len(repaired),
            "escalated": len(escalated),
            "skipped": len(skipped),
        },
        "repaired": repaired,
        "escalated": escalated,
        "skipped": skipped,
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Apply deterministic repair-rules to linter-violations.json and "
            "emit the subset that still needs the LLM repair-agent."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--state", required=True, help="State directory (e.g. state/)")
    ap.add_argument(
        "--test-file",
        default=None,
        help=(
            "Path to the Java test file that linter-violations.json refers "
            "to. Required to apply any deterministic action; omit it for a "
            "dry analysis of how many violations could be auto-repaired."
        ),
    )
    ap.add_argument(
        "--whitelist",
        default=None,
        help="Path to import-whitelist.json (only needed when addImport rules fire)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help=(
            "Destination JSON for the dispatch report (default: "
            "state/_summaries/repair-dispatch.json)"
        ),
    )
    args = ap.parse_args()

    state_dir = Path(args.state).resolve()
    if not state_dir.exists():
        print(f"[FAIL] state directory not found: {state_dir}", file=sys.stderr)
        return 2

    test_file = Path(args.test_file).resolve() if args.test_file else None
    whitelist = Path(args.whitelist).resolve() if args.whitelist else None

    report = dispatch(state_dir, test_file, whitelist)

    out_path = Path(args.out).resolve() if args.out else state_dir / "_summaries" / "repair-dispatch.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_path, report)

    counts = report["counts"]
    print(
        f"[OK] dispatch: {counts['repaired']} repaired, "
        f"{counts['escalated']} escalated, {counts['skipped']} skipped "
        f"(of {counts['violations']} violations) → {out_path}"
    )
    return 0


if __name__ == "__main__":
    with _TimedRun("repair_dispatch") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
