"""repair_rules_compiler.py — pre-compile repair-rules/*.rules into metadata.

Reads every `*.rules` file under --rules-dir (default: repair-rules/) and
projects each rule into JSON metadata. Compiled regex objects are NOT
persisted — only the source pattern string and parsed action.

CLI
---
    python tools/python/repair_rules_compiler.py \
        --rules-dir repair-rules \
        --out <execution_folder>/state/_summaries/compiled-rules.json

Source rules are read from java-test-coverage-architecture/repair-rules/ (static definitions).
Output goes to the execution folder — never back into the architecture directory.

If `repair-rules/` does not exist:
    - default: WARN and emit an empty compiled-rules.json (rc=0)
    - --strict: FAIL with rc=2
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import _TimedRun, emit_tool_summary  # noqa: E402

# ── Parsing ──────────────────────────────────────────────────────────────────

# A rule line is `"pattern"  =>  action(args)` with optional surrounding ws.
_RULE_RE = re.compile(
    r'^\s*"(?P<pattern>(?:[^"\\]|\\.)*)"\s*=>\s*(?P<action>[A-Za-z_][A-Za-z0-9_]*)'
    r"\((?P<args>.*)\)\s*$"
)

# Known fix templates → error codes. Anything not listed maps to GENERIC.
_ACTION_TO_ERROR_CODE: dict[str, str] = {
    "addImport":                    "MISSING_IMPORT",
    "removeImport":                 "UNRESOLVABLE_IMPORT",
    "resolveUniqueImportFromWhitelist": "UNRESOLVED_TYPE",
    "removeUsageAndEscalate":       "NON_PUBLIC_TYPE",
    "addAnnotation":                "MISSING_ANNOTATION",
    "wrapWith":                     "MOCKITO_STRICT_STUBBING",
    "removeStub":                   "MOCKITO_UNNECESSARY_STUBBING",
    "useMockMaker":                 "MOCKITO_FINAL_CLASS",
    "normalizeMatchers":            "MOCKITO_MATCHER_MISUSE",
    "replaceCall":                  "REPLACE_CALL",
    "addMockBean":                  "SPRING_MISSING_MOCKBEAN",
    "useBuilder":                   "BUILDER_REQUIRED",
    # New deterministic quality fixes (post-audit 2026-05-28):
    "insertAaaComments":            "TQG_02_NO_AAA",
    "removeUnusedStub":             "TQG_06_UNUSED_STUB",
    "convertMockSutToInjectMocks":  "TQG_12_OVER_MOCK_SUT",  # SUT sub-kind from test_linter.py
    "escalateToLLM":                "ESCALATED",
}


@dataclass(frozen=True)
class CompiledRule:
    file: str
    line: int
    pattern: str
    action: str
    args: str
    error_code: str
    fix_template: str
    evidence_required: bool


def _evidence_required_for(action: str) -> bool:
    """Rules that mutate code MUST point at evidence; pure escalations do not."""
    return action not in {"escalateToLLM"}


def _parse_line(file: str, lineno: int, raw: str) -> CompiledRule | None:
    text = raw.strip()
    if not text or text.startswith("#"):
        return None
    m = _RULE_RE.match(text)
    if not m:
        return None
    action = m.group("action")
    return CompiledRule(
        file=file,
        line=lineno,
        pattern=m.group("pattern"),
        action=action,
        args=m.group("args").strip(),
        error_code=_ACTION_TO_ERROR_CODE.get(action, "GENERIC"),
        fix_template=f"{action}({m.group('args').strip()})",
        evidence_required=_evidence_required_for(action),
    )


def compile_rules(rules_dir: Path) -> list[CompiledRule]:
    """Parse every *.rules file under `rules_dir` into CompiledRule entries.

    Files are visited in deterministic sorted order; rules within a file keep
    source order so callers preserve precedence.
    """
    out: list[CompiledRule] = []
    if not rules_dir.exists():
        return out
    for rules_file in sorted(rules_dir.glob("*.rules")):
        rel = rules_file.name
        try:
            text = rules_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for idx, raw in enumerate(text.splitlines(), start=1):
            rule = _parse_line(rel, idx, raw)
            if rule is not None:
                out.append(rule)
    return out


# ── Output building ──────────────────────────────────────────────────────────

def _per_file_metadata(rules: Iterable[CompiledRule]) -> list[dict]:
    by_file: dict[str, list[dict]] = {}
    for r in rules:
        by_file.setdefault(r.file, []).append({
            "line":             r.line,
            "pattern":          r.pattern,
            "action":           r.action,
            "args":             r.args,
            "errorCode":        r.error_code,
            "fixTemplate":      r.fix_template,
            "evidenceRequired": r.evidence_required,
        })
    return [
        {"file": fname, "count": len(items), "rules": items}
        for fname, items in sorted(by_file.items())
    ]


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pre-compile repair-rules/*.rules into JSON metadata.",
    )
    ap.add_argument(
        "--rules-dir",
        default="repair-rules",
        help="Directory containing *.rules files (default: repair-rules).",
    )
    ap.add_argument(
        "--out",
        required=True,
        help="Destination JSON inside the execution folder (e.g. <execution_folder>/state/_summaries/compiled-rules.json).",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Fail (rc=2) when --rules-dir is missing instead of warning.",
    )
    args = ap.parse_args()

    rules_dir = Path(args.rules_dir).resolve()
    out_path = Path(args.out).resolve()

    if not rules_dir.exists():
        if args.strict:
            print(
                f"[FAIL] rules dir not found: {rules_dir}",
                file=sys.stderr,
            )
            return 2
        print(
            f"[WARN] rules dir not found: {rules_dir} — emitting empty manifest",
            file=sys.stderr,
        )
        compiled: list[CompiledRule] = []
    else:
        compiled = compile_rules(rules_dir)

    payload = {
        "schemaVersion": 1,
        "generatedAt":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rulesDir":      str(rules_dir),
        "totalRules":    len(compiled),
        "files":         _per_file_metadata(compiled),
    }
    _atomic_write_json(out_path, payload)
    print(
        f"[OK] compiled {len(compiled)} rule(s) from {rules_dir} -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    with _TimedRun("repair_rules_compiler") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
