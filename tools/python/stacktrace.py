"""stacktrace.py — deterministic stack trace parser (Phase 2).

Implements the deterministic analysis policy rule:
  "Stack traces completos → parser determinístico; solo frame relevante + causa al LLM."

Input:  a raw JVM stack trace (stdin or --input file).
Output: JSON with the minimal, structured information the Repair Agent needs:
  {
    "exceptionClass": "java.lang.NullPointerException",
    "message": "Cannot invoke \"String.length()\" because str is null",
    "errorCode": "E_NPE",
    "relevantFrame": {
      "class": "com.acme.FooService",
      "method": "processName",
      "file": "FooService.java",
      "line": 42,
      "fqcn": "com.acme.FooService"
    },
    "causeChain": ["com.acme.FooService.processName(FooService.java:42)"],
    "testFrame": {
      "class": "com.acme.FooServiceTest",
      "method": "testProcessName_nullInput",
      "file": "FooServiceTest.java",
      "line": 18
    },
    "symbolFQN": "com.acme.FooService#processName",
    "fullFrameCount": 24,
    "filteredFrameCount": 2
  }

The LLM receives ONLY this JSON — never the raw stack trace.
This eliminates ~20-200 tokens of JVM framework noise per error.

Usage:
    python stacktrace.py --include "^com\\.acme\\." < stacktrace.txt
    python stacktrace.py --input stacktrace.txt --include "^com\\.acme\\." --out result.json
    python stacktrace.py --include "^com\\.acme\\." --state state/  # auto-detect filter from whitelist
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Patterns
# ─────────────────────────────────────────────────────────────────────────────

# "   at com.acme.FooService.processName(FooService.java:42)"
_FRAME_RE = re.compile(
    r"^\s+at\s+(?P<fqmethod>[\w\.\$<>]+)\((?P<file>[^:)]+)(?::(?P<line>\d+))?\)"
)
# "java.lang.NullPointerException: Cannot invoke ..."
_EXCEPTION_RE = re.compile(r"^(?P<cls>[\w\.\$]+(?:Exception|Error|Throwable|Fault)[\w\.\$]*)(?::\s*(?P<msg>.*))?$")
# "Caused by: ..."
_CAUSED_BY_RE = re.compile(r"^Caused by:\s+(?P<rest>.+)$")

# Exception class → errorCode mapping (expandable)
_EXCEPTION_CODES: dict[str, str] = {
    "NullPointerException": "E_NPE",
    "ClassCastException": "E_CLASS_CAST",
    "IllegalArgumentException": "E_ILLEGAL_ARG",
    "IllegalStateException": "E_ILLEGAL_STATE",
    "UnsupportedOperationException": "E_UNSUPPORTED_OP",
    "IndexOutOfBoundsException": "E_INDEX_OOB",
    "ArrayIndexOutOfBoundsException": "E_INDEX_OOB",
    "NoSuchMethodException": "E_NO_SUCH_METHOD",
    "NoSuchFieldException": "E_NO_SUCH_FIELD",
    "ClassNotFoundException": "E_CLASS_NOT_FOUND",
    "InstantiationException": "E_INSTANTIATION",
    "AssertionError": "E_ASSERTION",
    "ComparisonFailure": "E_ASSERTION",
    "AssertionFailedError": "E_ASSERTION",
    "MockitoException": "E_MOCK",
    "NotAMockException": "E_MOCK",
    "MissingMethodInvocationException": "E_MOCK",
    "WantedButNotInvoked": "E_MOCK_VERIFY",
    "TooManyActualInvocations": "E_MOCK_VERIFY",
    "UnnecessaryStubbingException": "E_MOCK_STUB",
    "BeanCreationException": "E_SPRING_CONTEXT",
    "NoSuchBeanDefinitionException": "E_SPRING_BEAN",
    "DataIntegrityViolationException": "E_DB",
    "StackOverflowError": "E_STACK_OVERFLOW",
    "OutOfMemoryError": "E_OOM",
}

# Known framework/infrastructure packages to skip in frame selection
_SKIP_PACKAGES = (
    "java.", "javax.", "jakarta.", "sun.", "com.sun.",
    "org.junit.", "junit.", "org.testng.",
    "org.mockito.", "net.bytebuddy.", "org.objenesis.",
    "org.springframework.test.", "org.springframework.boot.test.",
    "org.apache.maven.", "org.gradle.",
    "jdk.", "jdk.internal.", "jdk.proxy",
    "org.jacoco.",
    "org.opentest4j.",
    "kotlin.", "kotlinx.",
    "scala.",
    "com.intellij.", "org.eclipse.",
    "reactor.core.", "io.projectreactor.",
)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────

def _split_fqmethod(fqmethod: str) -> tuple[str, str]:
    """Split 'com.acme.Foo.bar' → ('com.acme.Foo', 'bar')."""
    # Handle '<init>' and '<clinit>'
    if "<" in fqmethod:
        last_dot = fqmethod.rfind(".", 0, fqmethod.index("<"))
    else:
        last_dot = fqmethod.rfind(".")
    if last_dot < 0:
        return ("", fqmethod)
    return fqmethod[:last_dot], fqmethod[last_dot + 1:]


def _exception_code(cls: str) -> str:
    simple = cls.rsplit(".", 1)[-1]
    for suffix, code in _EXCEPTION_CODES.items():
        if simple.endswith(suffix):
            return code
    return "E_UNKNOWN"


def _is_infrastructure_frame(fqmethod: str, skip_packages: tuple[str, ...]) -> bool:
    return any(fqmethod.startswith(pkg) for pkg in skip_packages)


def _parse_frames(lines: list[str]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for line in lines:
        m = _FRAME_RE.match(line)
        if not m:
            continue
        fqmethod = m.group("fqmethod")
        cls, method = _split_fqmethod(fqmethod)
        frames.append({
            "fqmethod": fqmethod,
            "class": cls,
            "method": method,
            "file": m.group("file"),
            "line": int(m.group("line")) if m.group("line") else None,
        })
    return frames


def _parse_exception_line(line: str) -> tuple[str, str] | None:
    m = _EXCEPTION_RE.match(line.strip())
    if m:
        return m.group("cls"), (m.group("msg") or "").strip()
    return None


def parse_stacktrace(
    raw: str,
    include_pattern: re.Pattern | None = None,
    skip_packages: tuple[str, ...] = _SKIP_PACKAGES,
) -> dict[str, Any]:
    """Parse a JVM stack trace into a minimal structured dict."""
    lines = raw.splitlines()
    exception_class = ""
    exception_message = ""
    cause_chain: list[str] = []
    all_frames: list[dict] = []
    current_exception_class = ""
    current_exception_msg = ""

    for line in lines:
        stripped = line.strip()
        # Caused by
        cb = _CAUSED_BY_RE.match(stripped)
        if cb:
            rest = cb.group("rest")
            parsed = _parse_exception_line(rest)
            if parsed:
                current_exception_class, current_exception_msg = parsed
                if not exception_class:
                    exception_class = current_exception_class
                    exception_message = current_exception_msg
            continue
        # Exception header (first non-indented line that looks like an exception)
        if not stripped.startswith("at ") and not stripped.startswith("..."):
            parsed = _parse_exception_line(stripped)
            if parsed:
                if not exception_class:
                    exception_class, exception_message = parsed
                    current_exception_class = exception_class
                continue
        # Stack frame
        frame_m = _FRAME_RE.match(line)
        if frame_m:
            fqmethod = frame_m.group("fqmethod")
            cls, method = _split_fqmethod(fqmethod)
            frame = {
                "fqmethod": fqmethod,
                "class": cls,
                "method": method,
                "file": frame_m.group("file"),
                "line": int(frame_m.group("line")) if frame_m.group("line") else None,
            }
            all_frames.append(frame)
            cause_chain.append(f"{cls}.{method}({frame['file']}:{frame['line']})")

    total_frames = len(all_frames)

    # ── Filter to relevant frames ─────────────────────────────────────────────
    def is_relevant(frame: dict) -> bool:
        cls = frame["class"]
        if _is_infrastructure_frame(frame["fqmethod"], skip_packages):
            return False
        if include_pattern and not include_pattern.search(cls):
            return False
        return True

    relevant_frames = [f for f in all_frames if is_relevant(f)]

    # ── Pick the most relevant frame (first in user code) ────────────────────
    relevant_frame: dict | None = relevant_frames[0] if relevant_frames else None
    if not relevant_frame and all_frames:
        # Fallback: first non-infrastructure frame
        for f in all_frames:
            if not _is_infrastructure_frame(f["fqmethod"], skip_packages):
                relevant_frame = f
                break

    # ── Identify test frame ───────────────────────────────────────────────────
    test_frame: dict | None = None
    for f in all_frames:
        cls = f["class"]
        if cls.endswith("Test") or cls.endswith("Tests") or "test" in cls.lower():
            if not _is_infrastructure_frame(f["fqmethod"], skip_packages):
                test_frame = f
                break

    # ── Build symbolFQN ───────────────────────────────────────────────────────
    symbol_fqn = ""
    if relevant_frame:
        symbol_fqn = f"{relevant_frame['class']}#{relevant_frame['method']}"

    # ── Build output ──────────────────────────────────────────────────────────
    result: dict[str, Any] = {
        "exceptionClass": exception_class,
        "message": exception_message,
        "errorCode": _exception_code(exception_class),
        "symbolFQN": symbol_fqn,
        "fullFrameCount": total_frames,
        "filteredFrameCount": len(relevant_frames),
        "causeChain": cause_chain[:5],  # top 5 frames only — no full trace to LLM
    }

    if relevant_frame:
        result["relevantFrame"] = {
            "class": relevant_frame["class"],
            "method": relevant_frame["method"],
            "file": relevant_frame["file"],
            "line": relevant_frame["line"],
            "fqcn": relevant_frame["class"],
        }

    if test_frame:
        result["testFrame"] = {
            "class": test_frame["class"],
            "method": test_frame["method"],
            "file": test_frame["file"],
            "line": test_frame["line"],
        }

    # Suggested repair rule hint (used by Repair Agent to look up repair-rules/*.rules)
    result["suggestedRule"] = _suggest_rule(result["errorCode"], exception_message)

    return result


def _suggest_rule(error_code: str, message: str) -> str | None:
    """Heuristic mapping from error code to repair-rules/*.rules category."""
    if error_code in ("E_NPE",):
        return "mockito.rules#NULL_RETURN"
    if error_code in ("E_MOCK", "E_MOCK_VERIFY", "E_MOCK_STUB"):
        return "mockito.rules"
    if error_code in ("E_ASSERTION",):
        return "junit.rules#ASSERTION"
    if error_code in ("E_SPRING_CONTEXT", "E_SPRING_BEAN"):
        return "spring.rules"
    if error_code == "E_INSTANTIATION":
        return "builders.rules#INSTANTIATION"
    if "cannot find symbol" in message or "does not exist" in message:
        return "imports.rules"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _load_include_from_whitelist(state_dir: Path) -> str | None:
    """Try to derive --include pattern from import-whitelist.json packages."""
    wl_path = state_dir / "import-whitelist.json"
    if not wl_path.exists():
        return None
    try:
        with wl_path.open(encoding="utf-8") as f:
            wl = json.load(f)
        source_pkgs = [
            p["name"] for p in wl.get("packages", [])
            if p.get("origin") in ("source", "generated")
        ]
        if not source_pkgs:
            return None
        # Build prefix pattern from common prefix of source packages
        prefixes = {pkg.split(".")[0] + "." + pkg.split(".")[1]
                    for pkg in source_pkgs if len(pkg.split(".")) >= 2}
        if prefixes:
            escaped = "|".join(re.escape(p) for p in sorted(prefixes))
            return f"^({escaped})"
    except Exception:
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Parse JVM stack trace → minimal JSON for Repair Agent (Phase 2)."
    )
    ap.add_argument("--input", default=None,
                    help="Path to stack trace file (default: read from stdin)")
    ap.add_argument("--out", default=None,
                    help="Output JSON path (default: print to stdout)")
    ap.add_argument("--include", default=None,
                    help="Regex to identify user code frames (e.g. '^com\\.acme\\.'). "
                         "If omitted, tries to derive from --state whitelist.")
    ap.add_argument("--state", default=None,
                    help="State directory; used to auto-detect --include from whitelist")
    args = ap.parse_args()

    # Read raw stack trace
    if args.input:
        raw = Path(args.input).read_text(encoding="utf-8", errors="ignore")
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        print("[FAIL] Empty stack trace input", file=sys.stderr)
        return 2

    # Resolve include pattern
    include_str = args.include
    if not include_str and args.state:
        include_str = _load_include_from_whitelist(Path(args.state))
        if include_str:
            print(f"[INFO] Auto-detected include pattern from whitelist: {include_str}",
                  file=sys.stderr)

    include_pattern = re.compile(include_str) if include_str else None

    result = parse_stacktrace(raw, include_pattern=include_pattern)

    out_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(out_json, encoding="utf-8")
        os.replace(tmp, out_path)
        print(f"[OK] {result['errorCode']} ({result['exceptionClass']}) → {args.out}",
              file=sys.stderr)
    else:
        print(out_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
