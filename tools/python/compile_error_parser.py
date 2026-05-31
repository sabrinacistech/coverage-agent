"""compile_error_parser.py — parse compiler build logs into state/compile-error-index.json.

Supports three input formats via --format:
  maven   — standard Maven/javac [ERROR] lines
  jdt     — Eclipse JDT multi-block format (VS Code Java extension)
  vscode  — VS Code Problems panel single-line format
  auto    — auto-detect from log content (default)

All formats are normalised to the same token vocabulary and produce output
conforming to compile-error-index.schema.json.

Error token map (unified across all formats):
  E_IMPORT_UNRESOLVED      — import/class cannot be resolved
  E_PACKAGE_UNRESOLVED     — package does not exist
  E_INTERFACE_INSTANTIATION — cannot instantiate interface/abstract
  E_CONSTRUCTOR_UNRESOLVED — constructor not found / cannot be applied
  E_METHOD_UNRESOLVED      — method not found / undefined
  E_TYPE_MISMATCH          — incompatible types
  E_GENERIC_INFERENCE      — inference variable / generic type error
  E_VARARGS                — non-varargs call
  E_OVERRIDE               — method does not override
  E_ACCESS                 — private/package access violation
  E_OTHER                  — unclassified

Usage:
  python compile_error_parser.py \\
    --log   build-output.log \\
    --out   state/compile-error-index.json \\
    --run   run-0042 \\
    --format auto
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from common import atomic_write_json, validate

# ── Unified error classification patterns ─────────────────────────────────────
# Applied to the message text regardless of input format.
# Order matters: more specific patterns first.

_UNIFIED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # JDT phrasing
    ("E_IMPORT_UNRESOLVED",       re.compile(r"The import\s+(\S+)\s+cannot be resolved")),
    ("E_IMPORT_UNRESOLVED",       re.compile(r"(\S+)\s+cannot be resolved to a type")),
    ("E_IMPORT_UNRESOLVED",       re.compile(r"(\S+)\s+cannot be resolved")),
    ("E_INTERFACE_INSTANTIATION", re.compile(r"Cannot instantiate the type\s+(\S+)")),
    ("E_METHOD_UNRESOLVED",       re.compile(r"The method\s+(\w+)\(.*?\)\s+is undefined for the type")),
    ("E_CONSTRUCTOR_UNRESOLVED",  re.compile(r"The constructor\s+(\S+)\(.*?\)\s+is undefined")),
    ("E_TYPE_MISMATCH",           re.compile(r"Type mismatch:\s+cannot convert from\s+(\S+)\s+to\s+(\S+)")),
    # Maven/javac phrasing
    ("E_PACKAGE_UNRESOLVED",      re.compile(r"package\s+([\w\.]+)\s+does not exist")),
    ("E_INTERFACE_INSTANTIATION", re.compile(r"(\S+)\s+is abstract;\s+cannot be instantiated")),
    ("E_CONSTRUCTOR_UNRESOLVED",  re.compile(r"constructor\s+(\S+)\s+in class\s+\S+\s+cannot be applied")),
    ("E_METHOD_UNRESOLVED",       re.compile(r"cannot find symbol\s+method\s+(\w+)\(")),
    ("E_IMPORT_UNRESOLVED",       re.compile(r"cannot find symbol\s+class\s+(\w+)")),
    ("E_TYPE_MISMATCH",           re.compile(r"incompatible types:\s+(\S+)\s+cannot be converted to\s+(\S+)")),
    ("E_GENERIC_INFERENCE",       re.compile(r"incompatible types:\s+inference variable")),
    ("E_VARARGS",                 re.compile(r"non-varargs call of varargs method")),
    ("E_OVERRIDE",                re.compile(r"method does not override")),
    ("E_ACCESS",                  re.compile(r"(\S+)\s+has\s+(?:private|package)\s+access")),
]


def _classify(msg: str) -> tuple[str, str]:
    """Return (error_code, captured_symbol_or_empty)."""
    for code, rx in _UNIFIED_PATTERNS:
        m = rx.search(msg)
        if m:
            return code, m.group(1) if m.lastindex and m.lastindex >= 1 else ""
    return "E_OTHER", ""


# ── Format detection ──────────────────────────────────────────────────────────

_MAVEN_HEADER_RE = re.compile(r"^\[ERROR\]\s+\S+:\[\d+,\d+\]", re.MULTILINE)
_JDT_BLOCK_RE = re.compile(r"\d+\.\s+ERROR in\s+\S+")
_VSCODE_PROBLEM_RE = re.compile(r"^\S+\(\d+,\s*\d+\):\s+error:", re.MULTILINE)


def detect_format(text: str) -> str:
    """Auto-detect log format. Returns 'maven', 'jdt', or 'vscode'."""
    if _MAVEN_HEADER_RE.search(text):
        return "maven"
    if _JDT_BLOCK_RE.search(text):
        return "jdt"
    if _VSCODE_PROBLEM_RE.search(text):
        return "vscode"
    return "maven"  # conservative fallback


# ── Maven parser ──────────────────────────────────────────────────────────────

_MAVEN_ERROR_LINE = re.compile(
    r"^\[ERROR\]\s+(?P<file>[^:]+):\[(?P<line>\d+),(?P<col>\d+)\]\s+(?P<msg>.+)$"
)


def _parse_maven(text: str, run_id: str) -> dict:
    errors: list[dict] = []
    eid = 0
    for raw in text.splitlines():
        m = _MAVEN_ERROR_LINE.match(raw)
        if not m:
            continue
        code, sym = _classify(m.group("msg"))
        eid += 1
        errors.append({
            "id": f"err:{eid:04d}",
            "code": code,
            "file": m.group("file").strip(),
            "line": int(m.group("line")),
            "col": int(m.group("col")),
            "message": m.group("msg").strip(),
            "symbolFQN": sym,
            "raw": raw.rstrip(),
        })
    return {"schemaVersion": 1, "runId": run_id, "errors": errors}


# ── JDT multi-block parser ────────────────────────────────────────────────────

_JDT_BLOCK_HEADER = re.compile(
    r"(\d+)\.\s+ERROR in\s+(.+?)\s+\(at line\s+(\d+)\)"
)
# Message lines in a JDT block are tab-indented; code lines are also tab-indented
# (they contain Java tokens). We identify the *error message* as the last
# tab-indented line that is NOT caret/squiggle and NOT raw Java code.
_JDT_TAB_LINE = re.compile(r"^\t(.+)$", re.MULTILINE)
_CARET_LINE_RE = re.compile(r"^[\^~\s]+$")


def _parse_jdt(text: str, run_id: str) -> dict:
    errors: list[dict] = []
    eid = 0
    # JDT output separates blocks with lines of dashes
    blocks = re.split(r"-{5,}", text)
    for block in blocks:
        header_m = _JDT_BLOCK_HEADER.search(block)
        if not header_m:
            continue
        file_path = header_m.group(2).strip()
        line_num = int(header_m.group(3))
        # Collect all tab-indented lines after the header
        tab_lines = [
            m.group(1).strip()
            for m in _JDT_TAB_LINE.finditer(block[header_m.end():])
        ]
        # The error message is the last non-caret, non-blank tab-indented line
        msg_candidates = [
            ln for ln in tab_lines
            if ln and not _CARET_LINE_RE.match(ln)
        ]
        msg = msg_candidates[-1] if msg_candidates else "unknown error"
        code, sym = _classify(msg)
        eid += 1
        errors.append({
            "id": f"err:{eid:04d}",
            "code": code,
            "file": file_path,
            "line": line_num,
            "col": 1,
            "message": msg,
            "symbolFQN": sym,
            "raw": header_m.group(0),
        })
    return {"schemaVersion": 1, "runId": run_id, "errors": errors}


# ── VS Code Problems-panel parser ─────────────────────────────────────────────
# Format: <file>(<line>, <col>): error: <message>

_VSCODE_LINE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),\s*(?P<col>\d+)\):\s+error:\s+(?P<msg>.+)$",
    re.MULTILINE,
)


def _parse_vscode(text: str, run_id: str) -> dict:
    errors: list[dict] = []
    eid = 0
    for m in _VSCODE_LINE.finditer(text):
        msg = m.group("msg").strip()
        code, sym = _classify(msg)
        eid += 1
        errors.append({
            "id": f"err:{eid:04d}",
            "code": code,
            "file": m.group("file").strip(),
            "line": int(m.group("line")),
            "col": int(m.group("col")),
            "message": msg,
            "symbolFQN": sym,
            "raw": m.group(0).rstrip(),
        })
    return {"schemaVersion": 1, "runId": run_id, "errors": errors}


# ── Dispatch ──────────────────────────────────────────────────────────────────

_PARSERS = {
    "maven":  _parse_maven,
    "jdt":    _parse_jdt,
    "vscode": _parse_vscode,
}


_MAX_ERRORS = 200  # schema maxItems — keep the JSON bounded for the repair-agent.


def parse(log_path: Path, run_id: str, fmt: str = "auto") -> dict:
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    resolved_fmt = detect_format(text) if fmt == "auto" else fmt
    parser = _PARSERS.get(resolved_fmt)
    if parser is None:
        raise ValueError(
            f"Unknown format '{resolved_fmt}'. "
            f"Valid values: {list(_PARSERS)} + 'auto'"
        )
    result = parser(text, run_id)
    errs = result.get("errors", [])
    if len(errs) > _MAX_ERRORS:
        result["errors"] = errs[:_MAX_ERRORS]
        result["truncated"] = {"total": len(errs), "kept": _MAX_ERRORS}
    result["format"] = resolved_fmt  # informational; not in schema required fields
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Parse a compiler build log into state/compile-error-index.json. "
            "Normalises Maven, Eclipse JDT, and VS Code error formats to a "
            "unified token vocabulary."
        )
    )
    ap.add_argument(
        "--log",
        required=True,
        metavar="PATH",
        help="Path to the compiler/build log file.",
    )
    ap.add_argument(
        "--out",
        required=True,
        metavar="PATH",
        help="Output path (e.g. state/compile-error-index.json).",
    )
    ap.add_argument(
        "--run",
        default="run-0",
        metavar="ID",
        help="Run identifier written into the output JSON (default: run-0).",
    )
    ap.add_argument(
        "--format",
        default="auto",
        choices=["maven", "jdt", "vscode", "auto"],
        metavar="FORMAT",
        help=(
            "Input log format. "
            "maven  — standard Maven/javac [ERROR] lines. "
            "jdt    — Eclipse JDT multi-block format (VS Code Java extension). "
            "vscode — VS Code Problems panel single-line format. "
            "auto   — auto-detect from log content (default)."
        ),
    )
    args = ap.parse_args()

    log = Path(args.log)
    if not log.exists():
        print(f"[FAIL] Log file not found: {log}", file=sys.stderr)
        return 2

    try:
        out = parse(log, args.run, fmt=args.format)
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    validate("compile-error-index", out)
    atomic_write_json(Path(args.out), out)

    detected = out.get("format", args.format)
    print(
        f"[OK] {len(out['errors'])} error(s) parsed "
        f"(format={detected}) → {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
