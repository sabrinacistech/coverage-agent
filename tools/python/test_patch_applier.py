"""test_patch_applier.py — inject LLM-produced JSON patches into Java test files.

Applies structured JSON patch descriptors (from Body Agent or Repair Agent) onto
physical Java test files under authorized test directories only.

HARD CONSTRAINTS enforced here (not by the LLM):
  - NEVER touches src/main/java/** (PermissionError raised before any write).
  - Only writes to authorized test roots (src/test/java, src/integrationTest/java, etc.).
  - Template-initialized files are seeded from templates/<name>.java[.tpl].
  - Method-name collision detection prevents duplicate @Test methods.
  - Every injected method receives an // evidence: comment from evidenceIds[].
  - state/generated-tests.json is updated atomically after each patch.
  - GATES BY CONSTRUCTION (only bypassable with --no-gates AND env
    TPA_ALLOW_NO_GATES=1, used by the patcher's own tests): because this is the
    only code path that writes Java, the deterministic gate suite is folded in
    here so a runtime caller cannot skip it. Before writing, gate_runner.evaluate_gates enforces G1/G2/G5/G7
    plus convergence (G8) and the execution-state budget (exit 2 if exceeded,
    exit 3 if a gate blocks). After rendering, G6 (linter) lints the written file
    and rolls the write back on failure.

Usage:
  python test_patch_applier.py \\
    --patch         state/_patches/FooServiceTest.patch.json \\
    --repo          /path/to/java-repo \\
    --state         state \\
    --templates     templates \\
    --context-pack  state/context-packs/<fqcn>.json \\
    --whitelist     state/import-whitelist.json \\
    --out           state/generated-tests.json \\
    [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any

# Disabling the folded-in gates requires BOTH --no-gates AND this env var. A CLI
# flag alone can never turn off enforcement in runtime — only the patcher's own
# unit tests set TPA_ALLOW_NO_GATES=1. Keeps the by-construction guarantee honest.
_ALLOW_NO_GATES_ENV = "TPA_ALLOW_NO_GATES"

from common import (  # noqa: F401
    _TimedRun,
    atomic_write_json,
    emit_tool_summary,
    has_raw_newline_inside_java_string,
    load_json,
    validate,
)
import framework_imports  # shared symbol→import catalog (FIX side); test_linter is the GATE side

# ── Safety constants ─────────────────────────────────────────────────────────
_FORBIDDEN_SEGMENTS = ("src/main/java", "src\\main\\java")
_AUTHORIZED_TEST_ROOTS = (
    "src/test/java",
    "src/integrationTest/java",
    "src/integration-test/java",
    "src/testFixtures/java",
)

# ── Regex helpers ─────────────────────────────────────────────────────────────
_IMPORT_LINE_RE = re.compile(
    r"^\s*import\s+(static\s+)?[\w\.]+(?:\.\*)?\s*;", re.MULTILINE
)
_PACKAGE_RE = re.compile(r"^\s*package\s+[\w\.]+\s*;", re.MULTILINE)
_LAST_IMPORT_RE = re.compile(r"^import\s+[\w\.]+(?:\.\*)?\s*;", re.MULTILINE)
_FIELD_INJECT_RE = re.compile(r"^\s*@InjectMocks\b", re.MULTILINE)
_CLASS_OPEN_RE = re.compile(r"\bclass\s+\w+[^{]*\{", re.DOTALL)
_LAST_CLOSING_BRACE_RE = re.compile(r"^}", re.MULTILINE)
_METHOD_NAME_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final|synchronized|abstract)\s+)*"
    r"(?:void|[\w<>\[\]]+)\s+(\w+)\s*\(",
    re.MULTILINE,
)
_FIELD_NAME_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:private|protected|public)\s+[\w<>\[\], ]+\s+(\w+)\s*[;=]",
    re.MULTILINE,
)
_COLLAB_BLOCK_RE = re.compile(
    r"[ \t]*//[ \t]*\$\{COLLABORATORS\}[^\n]*(?:\n[ \t]*//[^\n]*)*",
    re.MULTILINE,
)
_BODY_PLACEHOLDER_RE = re.compile(
    r"[ \t]*//[ \t]*\$\{TEST_BODY\}[^\n]*",
    re.MULTILINE,
)


# ── Body safety: forbidden Java structures inside methods[].body ─────────────
# A test method body may NOT contain top-level import/package statements or
# nested class/interface/enum declarations. Enforced at render time.
_BODY_FORBIDDEN = (
    re.compile(r"(?m)^\s*import\s+"),
    re.compile(r"(?m)^\s*package\s+"),
    re.compile(r"(?m)^\s*public\s+class\b"),
    re.compile(r"(?m)^\s*class\s+\w+"),
    re.compile(r"(?m)^\s*interface\s+\w+"),
    re.compile(r"(?m)^\s*enum\s+\w+"),
)


def _normalize_sut(sut: Any) -> str | None:
    """Accept sut as either a plain FQCN string or a structured {fqcn: ...} object.

    Centralizes the normalization so equality checks and rendering use the same key
    regardless of which shape the agent / context-pack chose.
    """
    if sut is None:
        return None
    if isinstance(sut, str):
        return sut
    if isinstance(sut, dict):
        f = sut.get("fqcn")
        return f if isinstance(f, str) else None
    return None


def sanitize_java_body(text: str) -> str:
    """Normalize a method body for safe Java source writing.

    Two failure modes handled via a string-literal-aware state machine:

      A) Real control characters inside a Java string literal — the LLM wrote
         argument content with actual 0x0A/0x0D/0x09 bytes instead of Java
         escape sequences, causing an "unclosed string literal" compile error
         (on Windows, Python text-mode write further expands 0x0A → CRLF).
         Fix: inside `"..."`, convert real newline/CR/tab to Java escapes.

      B) Over-escaped statement separators — agent emitted backslash+n as line
         break between statements (single-escape from json.loads, or double-
         escape Windows pipe artefact).
         Fix: outside `"..."`, convert (\\\\n or bare \\n when no real newlines
         exist) to a real 0x0A.
    """
    if not text:
        return text
    if '"' not in text and '\\' not in text and '\r' not in text:
        return text  # nothing to fix

    has_real_newline = "\n" in text

    result: list[str] = []
    in_string = False
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if in_string:
            if ch == "\\":
                # Consume backslash + next char verbatim: preserves valid Java
                # escape sequences and prevents \" from closing the string.
                result.append(ch)
                if i + 1 < n:
                    result.append(text[i + 1])
                    i += 2
                    continue
            elif ch == '"':
                in_string = False
                result.append(ch)
            elif ch == "\n":
                result.append("\\n")   # real newline inside literal → Java escape
            elif ch == "\r":
                result.append("\\r")   # real CR inside literal → Java escape
            elif ch == "\t":
                result.append("\\t")   # real tab inside literal → Java escape
            else:
                result.append(ch)
        else:
            if ch == '"':
                in_string = True
                result.append(ch)
            elif ch == "\\" and i + 1 < n:
                nxt = text[i + 1]
                if nxt == "\\" and i + 2 < n:
                    # Double-backslash artefact (Windows pipe over-escaping).
                    esc = text[i + 2]
                    if esc == "n":
                        result.append("\n")
                        i += 3
                        continue
                    elif esc == "t":
                        result.append("\t")
                        i += 3
                        continue
                    elif esc == "r":
                        i += 3           # drop \\r outside string literals
                        continue
                    elif esc == '"':
                        result.append('"')
                        i += 3
                        continue
                elif nxt == "n" and not has_real_newline:
                    # Single-escape fallback: no real newlines anywhere in the body,
                    # so backslash+n is a statement separator, not a Java escape.
                    result.append("\n")
                    i += 2
                    continue
                elif nxt == "t" and not has_real_newline:
                    result.append("\t")
                    i += 2
                    continue
                result.append(ch)
            else:
                result.append(ch)
        i += 1

    return "".join(result)


def _validate_body(body: str) -> None:
    if not body:
        return
    for pat in _BODY_FORBIDDEN:
        m = pat.search(body)
        if m:
            raise PermissionError(
                f"FORBIDDEN_JAVA_STRUCTURE_IN_BODY: {m.group(0).strip()!r}"
            )


# ── Import perimeter helpers ──────────────────────────────────────────────────

def _authorized_imports_from_whitelist(wl: dict) -> set[str]:
    """Extract authorized identifiers from an import-whitelist.json.

    Supports the schema-conformant shape (``packages: [{name, origin}]`` /
    ``classes: [{fqcn, origin}]``) AND legacy/free-form string arrays.
    Items that are neither strings nor dicts with the expected key are skipped.
    Never raises — accepts mixed input.
    """
    out: set[str] = set()
    for item in (wl.get("packages") or []):
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str):
                out.add(name)
    for item in (wl.get("classes") or []):
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, dict):
            fqcn = item.get("fqcn")
            if isinstance(fqcn, str):
                out.add(fqcn)
    return out


def _import_in_authorized_set(imp: str, authorized: set[str]) -> bool:
    """Return True if *imp* (a patch.allowedImports entry) is covered by *authorized*.

    *authorized* may contain: full FQCNs, package names, or "static X.Y.Z" entries.
    Matching rules (in order):
      1. Exact match (including "static X.Y.Z" entries from context-pack).
      2. Strip leading "static " and retry.
      3. Package prefix: "org.junit.jupiter.api" covers "org.junit.jupiter.api.Test".
      4. Wildcard: "org.junit.*" is covered if "org.junit" is in authorized.
    """
    if imp in authorized:
        return True
    bare = imp[len("static "):] if imp.startswith("static ") else imp
    if bare in authorized:
        return True
    if bare.endswith(".*"):
        return bare[:-2] in authorized
    if "." in bare:
        return bare.rsplit(".", 1)[0] in authorized
    return False


# ── Safety checks ─────────────────────────────────────────────────────────────

def _assert_not_production(path: Path, repo: Path) -> None:
    try:
        rel = path.relative_to(repo).as_posix()
    except ValueError:
        rel = str(path)
    for seg in _FORBIDDEN_SEGMENTS:
        if seg in rel:
            raise PermissionError(
                f"[BLOCKED] test_patch_applier must NEVER touch production code: {rel}"
            )


def _is_authorized_test_path(path: Path, repo: Path) -> bool:
    try:
        rel = path.relative_to(repo).as_posix()
    except ValueError:
        return False
    return any(rel.startswith(root) for root in _AUTHORIZED_TEST_ROOTS)


# ── Path resolution ────────────────────────────────────────────────────────────

def _resolve_test_file(patch: dict, repo: Path) -> Path:
    test_class: str = patch["testClass"]          # e.g. com.acme.FooServiceTest
    # Hardening: testClass SHOULD be the FQCN, but if an unqualified name slips
    # through (e.g. "FooServiceTest"), the file would land at src/test/java root
    # with a mismatched `package` declaration (JDT: "declared package … does not
    # match expected package", and same-package types fail to resolve). Qualify it
    # with testPackage so the file always lands in its package directory.
    if "." not in test_class:
        pkg = (patch.get("testPackage") or "").strip()
        if pkg:
            test_class = f"{pkg}.{test_class}"
    pkg_path = test_class.replace(".", "/") + ".java"
    target_dir: str = patch.get("targetDir") or "src/test/java"
    module: str = patch.get("targetModule") or ""
    if module:
        base = repo / module / target_dir / pkg_path
    else:
        base = repo / target_dir / pkg_path
    return base.resolve()


# ── Template loading ──────────────────────────────────────────────────────────

def _load_template(name: str, templates_dir: Path) -> str:
    for candidate in (
        templates_dir / f"{name}.java",
        templates_dir / f"{name}.java.tpl",
        templates_dir / "junit5-mockito.java",
    ):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"Template '{name}' not found in {templates_dir}. "
        "Expected file: <name>.java or <name>.java.tpl"
    )


# ── Code-generation helpers ───────────────────────────────────────────────────

def _indent_body(body: str) -> str:
    if not body.strip():
        return ""
    lines = textwrap.dedent(body).splitlines()
    out = []
    for line in lines:
        out.append(("        " + line.rstrip()) if line.strip() else "")
    return "\n".join(out)


def _render_method(m: dict) -> str:
    body_raw = sanitize_java_body((m.get("body") or "")).strip()
    _validate_body(body_raw)
    anns = m.get("annotations") or ["@Test"]
    ann_lines = "\n".join(f"    {a}" for a in anns)
    ev_ids = m.get("evidenceIds") or []
    if ev_ids and "// evidence:" not in body_raw:
        body_raw = body_raw + f"\n// evidence: {', '.join(ev_ids)}"
    indented = _indent_body(body_raw)
    return f"{ann_lines}\n    void {m['name']}() {{\n{indented}\n    }}"


def _field_declaration(f: dict) -> str:
    ann = f.get("annotation") or "@Mock"
    return f"    {ann}\n    private {f['type']} {f['name']};"


def _assert_imports_for(assert_fw: str | None) -> str:
    """Seed import block for ``${ASSERT_IMPORTS}`` matching the project's assert lib.

    junit-builtin returns '' on purpose: the reverse-import resolver
    (_ensure_required_imports) adds the specific
    ``org.junit.jupiter.api.Assertions.*`` statics on use, avoiding unused-import
    churn. AssertJ stays the default for unknown/none so the historical template
    behaviour is preserved for the common Spring Boot starter-test stack.
    """
    if assert_fw == "hamcrest":
        return (
            "import static org.hamcrest.MatcherAssert.assertThat;\n"
            "import static org.hamcrest.Matchers.is;"
        )
    if assert_fw == "junit-builtin":
        return ""
    return (
        "import static org.assertj.core.api.Assertions.assertThat;\n"
        "import static org.assertj.core.api.Assertions.assertThatThrownBy;"
    )


def _assert_not_null_for(assert_fw: str | None, expr: str = "sut") -> str:
    """Render a ``<expr> is not null`` assertion in the project's assert dialect."""
    if assert_fw == "hamcrest":
        return f"assertThat({expr}, org.hamcrest.Matchers.notNullValue());"
    if assert_fw == "junit-builtin":
        return f"assertNotNull({expr});"
    return f"assertThat({expr}).isNotNull();"


def _render_from_template(tpl: str, patch: dict, stack: dict | None = None) -> str:
    sut_fqn: str = patch["sut"]
    sut_simple = sut_fqn.rsplit(".", 1)[-1]
    pkg = patch.get("testPackage") or sut_fqn.rsplit(".", 1)[0]
    assert_fw = (stack or {}).get("assertFramework") or "assertj"

    fields = patch.get("fields") or []
    methods = patch.get("methods") or []

    collab_block = (
        "\n\n".join(_field_declaration(f) for f in fields)
        if fields
        else "    // no collaborators"
    )
    body_block = (
        "\n\n".join(_render_method(m) for m in methods)
        if methods
        else "    // no test methods generated"
    )

    result = tpl
    result = result.replace("${PACKAGE}", pkg)
    result = result.replace("${SUT_SIMPLE}", sut_simple)
    result = result.replace("${SUT_FQN}", sut_fqn)
    # Assert dialect is chosen from the detected stack, never hardcoded. These are
    # literal str.replace() (no regex) so escapes inside the values are untouched.
    result = result.replace("${ASSERT_IMPORTS}", _assert_imports_for(assert_fw))
    result = result.replace("${ASSERT_NOT_NULL}", _assert_not_null_for(assert_fw))
    # CRITICAL: use FUNCTION replacements, never string replacements. re.sub treats
    # a string repl specially — it expands backreferences (\1, \g<n>) AND escape
    # sequences (\n, \t, \r, \f...). The generated Java carries valid escapes
    # inside string literals (e.g. "a\nb\tc"); a string repl would turn those back
    # into REAL control characters, producing an "unclosed string literal" compile
    # error (caught downstream as INVALID_JAVA_STRING_LITERAL) and could even raise
    # on a stray "\g"/"\1". A lambda repl is inserted verbatim — no escape/group
    # processing — so the rendered Java is byte-for-byte what _render_method emitted.
    result = _COLLAB_BLOCK_RE.sub(lambda _m: collab_block, result)
    result = _BODY_PLACEHOLDER_RE.sub(lambda _m: "\n\n" + body_block, result)

    # M2: drop the Mockito scaffolding when there is nothing to inject. With no
    # @Mock collaborators AND a SUT instance that the test body never touches
    # (e.g. a static utility like LogSanitizer), @ExtendWith(MockitoExtension)
    # + @InjectMocks are dead weight that trip SonarQube smells. We only strip
    # them in this case: when the body DOES reference `sut`, keeping @InjectMocks
    # lets Mockito instantiate it (compile-safe regardless of the constructor),
    # so we never risk an uninstantiated SUT. The orphaned imports left behind
    # are removed downstream by _prune_unused_imports.
    sut_referenced = bool(re.search(r"\bsut\b", body_block))
    if not fields and not sut_referenced:
        result = re.sub(
            r"[ \t]*@ExtendWith\(MockitoExtension\.class\)[ \t]*\n",
            "",
            result,
        )
        result = re.sub(
            r"[ \t]*@InjectMocks[ \t]*\n[ \t]*private[ \t]+"
            + re.escape(sut_simple)
            + r"[ \t]+sut;[ \t]*\n",
            "",
            result,
        )
    return result


# ── Extraction helpers ────────────────────────────────────────────────────────

def _existing_imports(text: str) -> set[str]:
    return {m.group(0).strip() for m in _IMPORT_LINE_RE.finditer(text)}


def _existing_method_names(text: str) -> set[str]:
    return {m.group(1) for m in _METHOD_NAME_RE.finditer(text)}


def _existing_field_names(text: str) -> set[str]:
    return {m.group(1) for m in _FIELD_NAME_RE.finditer(text)}


# ── Injection into existing files ─────────────────────────────────────────────

def _insert_import_lines(text: str, lines: list[str]) -> str:
    """Insert ready-made `import ...;` lines after the last existing import.

    Shared insertion point for both the regular (`_inject_imports`) and the
    static (`_inject_static_imports`) injectors so they place imports identically.
    """
    if not lines:
        return text
    matches = list(_LAST_IMPORT_RE.finditer(text))
    if matches:
        pos = matches[-1].end()
        return text[:pos] + "\n" + "\n".join(lines) + text[pos:]
    pkg_m = _PACKAGE_RE.search(text)
    if pkg_m:
        pos = pkg_m.end()
        return text[:pos] + "\n\n" + "\n".join(lines) + text[pos:]
    return "\n".join(lines) + "\n\n" + text


def _inject_imports(text: str, new_imports: list[str]) -> str:
    existing = _existing_imports(text)
    to_add = []
    for imp in dict.fromkeys(new_imports):  # de-dup, preserve order
        stmt = f"import {imp};"
        if stmt not in existing and f"import static {imp};" not in existing:
            to_add.append(stmt)
    return _insert_import_lines(text, to_add)


def _inject_static_imports(text: str, new_imports: list[str]) -> str:
    """Inject `import static <owner>.<member>;` lines (skip those already present)."""
    existing = _existing_imports(text)
    to_add = []
    for imp in dict.fromkeys(new_imports):  # de-dup, preserve order
        stmt = f"import static {imp};"
        if stmt not in existing:
            to_add.append(stmt)
    return _insert_import_lines(text, to_add)


def _inject_fields(text: str, fields: list[dict], existing_names: set[str]) -> str:
    to_add = [f for f in fields if f["name"] not in existing_names]
    if not to_add:
        return text
    block = "\n\n".join(_field_declaration(f) for f in to_add)
    inj_m = _FIELD_INJECT_RE.search(text)
    if inj_m:
        pos = inj_m.start()
        return text[:pos] + block + "\n\n    " + text[pos:]
    cls_m = _CLASS_OPEN_RE.search(text)
    if cls_m:
        pos = cls_m.end()
        return text[:pos] + "\n\n" + block + text[pos:]
    return text


def _inject_methods(text: str, methods: list[dict], existing_names: set[str]) -> str:
    to_add = [m for m in methods if m["name"] not in existing_names]
    if not to_add:
        return text
    blocks = "\n\n".join(_render_method(m) for m in to_add)
    last_brace = None
    for match in _LAST_CLOSING_BRACE_RE.finditer(text):
        last_brace = match
    if last_brace:
        pos = last_brace.start()
        return text[:pos] + "\n" + blocks + "\n\n" + text[pos:]
    return text + "\n\n" + blocks + "\n}\n"


# ── Unused-import pruning (Clean Code: SonarQube "Unused imports") ────────────

_IMPORT_CAPTURE_RE = re.compile(
    r"^[ \t]*import[ \t]+(static[ \t]+)?([\w.]+(?:\.\*)?)[ \t]*;[ \t]*$",
    re.MULTILINE,
)


def _strip_comments_and_strings(java: str) -> str:
    """Blank out comments and string/char literals so they don't count as symbol
    usage. A symbol named only in a comment (e.g. the BDD ``// when`` AAA marker)
    or inside a string literal is NEVER a real use of an imported symbol — counting
    it would wrongly keep an unused import (e.g. ``org.mockito.Mockito.when``) and
    re-introduce the SonarQube "Unused imports" violation M2 exists to prevent.
    """
    java = re.sub(r"/\*.*?\*/", " ", java, flags=re.DOTALL)   # block comments
    java = re.sub(r"//[^\n]*", " ", java)                      # line comments
    java = re.sub(r'"(?:\\.|[^"\\\n])*"', " ", java)           # string literals
    java = re.sub(r"'(?:\\.|[^'\\\n])*'", " ", java)           # char literals
    return java


def _prune_unused_imports(text: str) -> str:
    """Remove import lines whose imported symbol is never referenced.

    The deterministic template (templates/junit5-mockito.java) emits a fixed,
    maximal import block (Mockito when/verify/never/times/any, assertThatThrownBy,
    @Mock/@InjectMocks, ...). A simple getter/DTO test uses only a few of them,
    leaving the rest as SonarQube "Unused imports" violations that block the
    OpenShift quality gate. Because the LLM only fills ${TEST_BODY} it cannot
    prune them — so we do it deterministically here, on the only code path that
    writes Java.

    Conservative by construction: a symbol counts as "used" if its simple name
    appears as a whole word anywhere outside the import block (comments and
    string literals excluded — see _strip_comments_and_strings). This biases
    toward KEEPING an import — a false "used" leaves a harmless extra import,
    whereas a false "unused" would break compilation. Wildcard imports (``.*``)
    are always kept.
    """
    matches = list(_IMPORT_CAPTURE_RE.finditer(text))
    if not matches:
        return text

    # Usage scan target: the file with every import statement removed (so an
    # import's own path never counts as a usage of itself) AND with comments /
    # string literals blanked (so a `// when` AAA marker never keeps the unused
    # Mockito `when` import).
    usage_text = _strip_comments_and_strings(_IMPORT_CAPTURE_RE.sub("", text))

    spans_to_drop: list[tuple[int, int]] = []
    for m in matches:
        path = m.group(2)
        if path.endswith(".*"):
            continue  # wildcard — cannot prove unused
        symbol = path.rsplit(".", 1)[-1]
        # The import is only "used" if its simple name appears as a BARE identifier.
        # A qualified reference (e.g. `Mockito.when(...)`) does NOT consume the
        # import `import static org.mockito.Mockito.when;` — the `(?<!\.)` lookbehind
        # rejects a leading dot, so qualified uses no longer keep an unused import
        # (the SonarQube "Unused imports" false-positive this method must avoid).
        if re.search(rf"(?<![\w.]){re.escape(symbol)}\b", usage_text):
            continue  # referenced as a bare name — keep
        start, end = m.start(), m.end()
        if end < len(text) and text[end] == "\n":
            end += 1
        spans_to_drop.append((start, end))

    if not spans_to_drop:
        return text

    out: list[str] = []
    cursor = 0
    for start, end in spans_to_drop:
        out.append(text[cursor:start])
        cursor = end
    out.append(text[cursor:])
    pruned = "".join(out)

    # Collapse any 3+ consecutive newlines left behind to a single blank line.
    return re.sub(r"\n{3,}", "\n\n", pruned)


# ── Reverse import resolution (symbol used → import required) ──────────────────
# The LLM declares patch.allowedImports, but a body that references a framework
# symbol it forgot to declare — classically `Assertions.assertEquals(...)` with no
# `import org.junit.jupiter.api.Assertions;` — fails to compile with
# "cannot find symbol: variable Assertions". G1 only checks declared→whitelisted,
# never used→declared, so the gap slips past every gate straight to Maven.
#
# This deterministic pass closes it: it resolves (via the shared framework_imports
# catalog — also used by test_linter's reverse-G1 gate, so the fixer and the gate
# never drift) the imports the well-known JUnit / Mockito / AssertJ / Hamcrest
# symbols in the body require, and injects the missing ones. It ONLY ever adds
# framework imports — never project types — so it can never mask a real
# hallucination (those still fail G1/G2).


def _ensure_required_imports(text: str, stack: dict | None) -> str:
    """Inject the framework imports that symbols used in the body actually need.

    Idempotent and additive: already-present imports are skipped (handled by the
    injectors), and only the curated framework symbol set is ever resolved.
    """
    test_fw = (stack or {}).get("testFramework") or "junit5"
    assert_fw = (stack or {}).get("assertFramework") or "assertj"
    type_imports, static_imports = framework_imports.resolve_imports(
        text, test_fw, assert_fw
    )
    text = _inject_imports(text, type_imports)
    text = _inject_static_imports(text, static_imports)
    return text


def _resolve_authorized_type_imports(text: str, authorized_imports: list[str] | None) -> str:
    """Inject non-static imports for AUTHORIZED project/dependency types whose
    simple name is used in the body but left unimported by the LLM.

    Framework symbols are handled by _ensure_required_imports; this closes the
    *project-type* half of "cannot find symbol" (e.g. a body that references
    `ClusterConfigProperties` / `ClusterProjection` from another package without
    importing them). It is the exact mirror of _prune_unused_imports: a type
    counts as used when its simple name appears as a bare identifier (comments,
    strings and import lines stripped). Only context-pack ``allowedImports`` FQCNs
    are eligible — never an invented symbol — and only when a simple name maps to
    exactly ONE authorized FQCN (ambiguous names are left to the LLM/compiler).
    Same-package and java.lang types need no import and are skipped.
    """
    if not authorized_imports:
        return text

    pkg_match = re.search(r"package\s+([\w.]+)\s*;", text)
    same_pkg = pkg_match.group(1) if pkg_match else ""

    # Simple names already imported (non-static, non-wildcard) — never re-add.
    already: set[str] = set()
    for m in _IMPORT_CAPTURE_RE.finditer(text):
        if m.group(1):  # static
            continue
        path = m.group(2)
        if path.endswith(".*"):
            continue
        already.add(path.rsplit(".", 1)[-1])

    by_simple: dict[str, set[str]] = {}
    for fq in authorized_imports:
        if "." not in fq:
            continue
        by_simple.setdefault(fq.rsplit(".", 1)[-1], set()).add(fq)

    scan = _strip_comments_and_strings(_IMPORT_CAPTURE_RE.sub("", text))
    to_add: list[str] = []
    for simple, fqcns in by_simple.items():
        if simple in already or len(fqcns) != 1:
            continue
        fq = next(iter(fqcns))
        pkg = fq.rsplit(".", 1)[0]
        if pkg == "java.lang" or pkg == same_pkg:
            continue
        if re.search(rf"(?<![\w.]){re.escape(simple)}\b", scan):
            to_add.append(fq)

    return _inject_imports(text, to_add)


def _stack_view(context_pack: dict | None) -> dict | None:
    """Extract {testFramework, assertFramework} from a verbose or compact pack."""
    if not context_pack:
        return None
    st = context_pack.get("stack")
    if isinstance(st, dict):
        return {
            "testFramework": st.get("testFramework"),
            "assertFramework": st.get("assertFramework"),
        }
    # compact stk: [java, testFw, mockFw, assertFw, springEnabled, ns, ...]
    stk = context_pack.get("stk")
    if isinstance(stk, list) and len(stk) >= 4:
        return {"testFramework": stk[1], "assertFramework": stk[3]}
    return None


# ── Core apply function ───────────────────────────────────────────────────────

def apply_patch(
    patch: dict,
    repo: Path,
    templates_dir: Path,
    dry_run: bool = False,
    stack: dict | None = None,
    authorized_imports: list[str] | None = None,
) -> dict:
    patch_id: str = patch.get("patchId") or f"patch:{uuid.uuid4().hex[:12]}"
    sut = _normalize_sut(patch.get("sut"))
    if not sut:
        raise ValueError("patch.sut missing or unparseable (need string or {fqcn})")
    patch["sut"] = sut  # canonicalize for downstream renderers / report
    test_class: str = patch["testClass"]
    fields: list[dict] = patch.get("fields") or []
    methods: list[dict] = patch.get("methods") or []
    allowed_imports: list[str] = patch.get("allowedImports") or []

    test_path = _resolve_test_file(patch, repo)
    _assert_not_production(test_path, repo)

    if not _is_authorized_test_path(test_path, repo):
        rel = test_path.relative_to(repo).as_posix() if test_path.is_relative_to(repo) else str(test_path)
        raise PermissionError(
            f"[BLOCKED] Target path is not an authorized test directory: {rel}\n"
            f"Authorized roots: {_AUTHORIZED_TEST_ROOTS}"
        )

    injected_methods: list[str] = []
    skipped_methods: list[str] = []
    action: str

    if test_path.exists():
        current = test_path.read_text(encoding="utf-8")
        ex_methods = _existing_method_names(current)
        ex_fields = _existing_field_names(current)

        skipped_methods = [m["name"] for m in methods if m["name"] in ex_methods]
        injected_methods = [m["name"] for m in methods if m["name"] not in ex_methods]

        new_text = current
        new_text = _inject_imports(new_text, allowed_imports)
        new_text = _inject_fields(new_text, fields, ex_fields)
        new_text = _inject_methods(new_text, methods, ex_methods)
        action = "PATCHED"
    else:
        template_name: str = patch.get("template") or "junit5-mockito"
        tpl_src = _load_template(template_name, templates_dir)
        new_text = _render_from_template(tpl_src, patch, stack=stack)
        new_text = _inject_imports(new_text, allowed_imports)
        injected_methods = [m["name"] for m in methods]
        action = "INITIALIZED"

    # M2: strip imports left unused by the fixed template block (and by any
    # FQCN-inlined symbols) so the file passes SonarQube "Unused imports", which
    # otherwise blocks the OpenShift deploy gate. Single insertion point: this is
    # the only code path that writes Java.
    new_text = _prune_unused_imports(new_text)

    # Reverse import resolution: add framework imports that symbols used in the
    # body need but the LLM forgot to declare (e.g. `Assertions.assertEquals`
    # without its import). Runs AFTER pruning so these additions are never
    # stripped, and only adds the curated JUnit/Mockito/AssertJ set — turning the
    # most common "cannot find symbol" compile failure into an impossibility.
    new_text = _ensure_required_imports(new_text, stack)
    # Same idea for the project/dependency TYPES the body references (collaborators,
    # fixtures) that the LLM left unimported — backfilled from the context-pack's
    # authorized import set so they cannot trip "cannot find symbol" either.
    new_text = _resolve_authorized_type_imports(new_text, authorized_imports)

    # Last-resort backstop before touching disk: a raw newline/CR inside a Java
    # string literal is an "unclosed string literal" compile error. sanitize_java_
    # body() already converts real control chars to escapes during render, so this
    # normally never fires — but if that pass ever regresses or a future code path
    # bypasses it, we want to fail loudly here (no broken Java written, clear
    # signal to regenerate) instead of paying a full javac cycle to discover it.
    # Treated as a generation defect, not an application bug. See string-literals
    # .rules (repair) and skills/07-generation Java String Literal Safety.
    if test_path.suffix == ".java" and has_raw_newline_inside_java_string(new_text):
        raise ValueError(
            "INVALID_JAVA_STRING_LITERAL: rendered test contains a raw newline "
            "inside a Java string literal (would not compile). Regenerate the "
            "affected literal using escaped sequences (\\n, \\r, \\t, \\\\, \\\")."
        )

    if not dry_run:
        test_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = test_path.with_suffix(".java.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(test_path)

    return {
        "patchId": patch_id,
        "action": action,
        "testClass": test_class,
        "sut": sut,
        "file": str(test_path),
        "injectedMethods": injected_methods,
        "skippedMethods": skipped_methods,
        "status": "PROPOSED" if not dry_run else "DRY_RUN",
    }


# ── Report updater ────────────────────────────────────────────────────────────

def _update_report(
    out_path: Path,
    result: dict,
    patch: dict,
    dry_run: bool,
) -> None:
    if out_path.exists():
        report = load_json(out_path)
        if not isinstance(report, dict):
            report = {"schemaVersion": 1, "tests": []}
    else:
        report = {"schemaVersion": 1, "tests": []}
    report.setdefault("schemaVersion", 1)

    cycle = patch.get("cycle", 1)
    all_evidence: list[str] = [
        eid
        for m in (patch.get("methods") or [])
        for eid in (m.get("evidenceIds") or [])
    ]
    new_entry: dict = {
        "testClass": result["testClass"],
        "sut": result["sut"],
        "status": result["status"],
        "patchId": result["patchId"],
        "evidenceIds": all_evidence,
    }
    report["cycle"] = cycle
    tests = report.get("tests")
    if not isinstance(tests, list):
        tests = []
        report["tests"] = tests
    existing = next(
        (
            t for t in tests
            if isinstance(t, dict) and t.get("testClass") == result["testClass"]
        ),
        None,
    )
    if existing:
        existing.update(new_entry)
    else:
        tests.append(new_entry)

    if not dry_run:
        validate("generated-tests", report)
        atomic_write_json(out_path, report)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Apply a JSON patch descriptor from Body/Repair Agent onto a Java test file. "
            "Initializes from template if the file does not exist. "
            "NEVER modifies src/main/java."
        )
    )
    ap.add_argument(
        "--patch",
        required=True,
        metavar="PATH",
        help="JSON patch file produced by Body Agent or Repair Agent.",
    )
    ap.add_argument(
        "--repo",
        required=True,
        metavar="DIR",
        help="Root directory of the Java repository being tested.",
    )
    ap.add_argument(
        "--state",
        default="state",
        metavar="DIR",
        help="State directory (default: state/).",
    )
    ap.add_argument(
        "--templates",
        default=None,
        metavar="DIR",
        help=(
            "Templates directory. Defaults to <architecture-root>/templates/. "
            "Template files: <name>.java or <name>.java.tpl"
        ),
    )
    ap.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Output path for generated-tests.json (default: <state>/generated-tests.json).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Simulate the patch without writing any Java files. "
            "The generated-tests.json report is also NOT updated."
        ),
    )
    ap.add_argument(
        "--context-pack",
        default=None,
        metavar="PATH",
        help=(
            "Optional: path to state/context-packs/<fqcn>.json. "
            "When provided, (1) validates patch.allowedImports against "
            "contextPack.allowedImports and (2) asserts patch.sut == contextPack.sut. "
            "Any import absent from the authorized set causes exit 3."
        ),
    )
    ap.add_argument(
        "--whitelist",
        default=None,
        metavar="PATH",
        help=(
            "Optional: path to state/import-whitelist.json. "
            "When provided, validates patch.allowedImports against the whitelist's "
            "packages[] and classes[] entries. "
            "Any import absent from the authorized set causes exit 3."
        ),
    )
    ap.add_argument(
        "--repair-attempt",
        action="append",
        default=None,
        metavar="errorCode|symbolFQN|fixId",
        help=(
            "G7: declare a (errorCode, symbolFQN, fixId) anti-loop triplet for "
            "this repair attempt. May be supplied multiple times. The orchestrator "
            "owns this metadata (derived from the compile-error index); without it "
            "a patch flagged as a repair (patchId 'repair:' / repairOf) is blocked "
            "with G7_REPAIR_WITHOUT_TRIPLET."
        ),
    )
    ap.add_argument(
        "--no-gates",
        action="store_true",
        help=(
            "Disable the folded-in deterministic gate suite (G1/G2/G5/G6/G7/G8) "
            "and the budget backstop. Only takes effect when the environment "
            f"variable {_ALLOW_NO_GATES_ENV}=1 is also set (unit tests of the "
            "patcher only) — a CLI flag alone can NEVER disable enforcement in "
            "runtime: the gates are the by-construction anti-hallucination guarantee."
        ),
    )
    args = ap.parse_args()

    # --no-gates is honored only with the env opt-in. Otherwise it is ignored and
    # gates stay ON (fail-safe), so a stray runtime flag cannot weaken enforcement.
    gates_disabled = args.no_gates and os.environ.get(_ALLOW_NO_GATES_ENV) == "1"
    if args.no_gates and not gates_disabled:
        print(
            f"[WARN] --no-gates ignored: set {_ALLOW_NO_GATES_ENV}=1 to disable "
            "gates (tests only). Enforcing gates.",
            file=sys.stderr,
        )

    repo = Path(args.repo).resolve()
    state_dir = Path(args.state) if Path(args.state).is_absolute() else Path.cwd() / args.state

    if args.templates:
        templates_dir = Path(args.templates).resolve()
    else:
        templates_dir = (Path(__file__).resolve().parents[2] / "templates").resolve()

    out_path = (
        Path(args.out).resolve()
        if args.out
        else (state_dir / "generated-tests.json").resolve()
    )

    patch_path = Path(args.patch)
    if not patch_path.exists():
        print(f"[FAIL] Patch file not found: {patch_path}", file=sys.stderr)
        return 2

    try:
        patch = load_json(patch_path)
    except Exception as exc:
        print(f"[FAIL] Cannot parse patch JSON: {exc}", file=sys.stderr)
        return 2

    required_keys = {"sut", "testClass"}
    missing = required_keys - patch.keys()
    if missing:
        print(
            f"[FAIL] Patch JSON missing required keys: {missing}",
            file=sys.stderr,
        )
        return 2

    # ── Perimeter interception middleware (runs before any I/O write) ─────────
    context_pack: dict | None = None
    if args.context_pack:
        try:
            context_pack = load_json(Path(args.context_pack).resolve())
        except Exception as exc:
            print(f"[FAIL] Cannot load context-pack: {exc}", file=sys.stderr)
            return 2
        # Structural SUT identity check — normalize both sides so the comparison
        # works whether the agent emitted "com.acme.X" or {"fqcn":"com.acme.X"}.
        cp_sut = _normalize_sut(context_pack.get("sut"))
        patch_sut = _normalize_sut(patch.get("sut"))
        if cp_sut != patch_sut:
            print(
                f"[BLOCKED] patch.sut '{patch_sut}' does not match "
                f"contextPack.sut '{cp_sut}'",
                file=sys.stderr,
            )
            return 3

    # Build authorized import set (union of context-pack + whitelist sources)
    authorized_imports: set[str] | None = None
    if context_pack is not None or args.whitelist:
        authorized_imports = set()
        if context_pack is not None:
            authorized_imports.update(context_pack.get("allowedImports") or [])
        if args.whitelist:
            try:
                wl = load_json(Path(args.whitelist).resolve())
            except Exception as exc:
                print(f"[FAIL] Cannot load whitelist: {exc}", file=sys.stderr)
                return 2
            authorized_imports.update(_authorized_imports_from_whitelist(wl))

    # Validate every declared import against the authorized perimeter
    if authorized_imports is not None:
        for imp in (patch.get("allowedImports") or []):
            if not _import_in_authorized_set(imp, authorized_imports):
                print(
                    f"[BLOCKED] import not allowed by context-pack/whitelist: {imp}",
                    file=sys.stderr,
                )
                return 3
    # ── End perimeter middleware ───────────────────────────────────────────────

    # Gates ON but no authorized perimeter ⇒ G1 (import whitelist) and G5 (stack)
    # would run against an empty pack and PASS vacuously (audit H-1c). The
    # context-pack / whitelist is the by-construction anti-hallucination perimeter,
    # not an optional convenience — refuse rather than write unverified imports.
    if not gates_disabled and authorized_imports is None:
        print(
            "[BLOCKED] G1_NO_PERIMETER: gates are enforced but neither "
            "--context-pack nor --whitelist was supplied; the authorized-import "
            "perimeter is mandatory (G1/G5 would otherwise be vacuous). Pass one, "
            f"or set {_ALLOW_NO_GATES_ENV}=1 with --no-gates for patcher unit tests.",
            file=sys.stderr,
        )
        return 3

    # ── Gate + budget enforcement BY CONSTRUCTION (M2) ─────────────────────────
    # This is the only code path that writes Java, so the gate suite is folded
    # in here: a patch that fails the anti-hallucination gates (G1/G2/G5/G7) or
    # exceeds the budget (G8 / maxCycles) never reaches disk. G6 (linter) needs
    # the rendered file and runs post-write below.
    if not gates_disabled:
        from budget_enforcer import EXIT_EXCEEDED, check as _budget_check  # local
        from gate_runner import _parse_cli_attempts, evaluate_gates  # local import (same dir)

        cli_attempts = _parse_cli_attempts(args.repair_attempt)

        exec_state = state_dir / "execution-state.json"
        if exec_state.exists():
            try:
                brc, bpayload = _budget_check(exec_state)
            except Exception as exc:  # malformed state must not silently pass
                print(f"[BLOCKED] BUDGET_STATE_UNREADABLE: {exc}", file=sys.stderr)
                return 2
            if brc == EXIT_EXCEEDED:
                print(
                    f"[BLOCKED] BUDGET_EXCEEDED ({bpayload.get('reason')}): "
                    f"cycle={bpayload.get('cycle')} maxCycles={bpayload.get('maxCycles')}",
                    file=sys.stderr,
                )
                return 2

        gate_report = evaluate_gates(
            patch,
            context_pack or {},
            state_dir,
            test_file=None,  # G6 runs post-write (needs the rendered file)
            cli_attempts=cli_attempts,
            context_pack_path=(
                Path(args.context_pack).resolve() if args.context_pack else None
            ),
        )
        if gate_report.get("status") == "FAIL":
            br = gate_report.get("blockedReason") or "GATE_FAIL"
            rc = 2 if br.startswith("G8") else 3
            print(f"[BLOCKED] gate {br}", file=sys.stderr)
            # Emit the failing gate's detail (e.g. G2 orphanEvidenceIds /
            # methodsWithoutEvidence) so the captured output carries the
            # symbol-level reason into the repair payload, not just the code.
            # Plain-string format on purpose: this module does not import json.
            detail_parts: list[str] = []
            for gname, g in (gate_report.get("gates") or {}).items():
                if not isinstance(g, dict) or g.get("status") != "FAIL":
                    continue
                seg = f"{gname}={g.get('blockedReason') or 'FAIL'}"
                orphans = g.get("orphanEvidenceIds") or []
                if orphans:
                    seg += "; orphanEvidenceIds=" + ", ".join(
                        f"{o.get('method')}:{o.get('evidenceId')}"
                        for o in orphans if isinstance(o, dict))
                mwe = g.get("methodsWithoutEvidence") or []
                if mwe:
                    seg += "; methodsWithoutEvidence=" + ", ".join(str(x) for x in mwe)
                detail_parts.append(seg)
            if detail_parts:
                print("[BLOCKED-DETAIL] " + " | ".join(detail_parts), file=sys.stderr)
            return rc
    # ── End gate + budget enforcement ──────────────────────────────────────────

    # Capture prior content so a post-write G6 failure can be rolled back
    # (None when the target test file does not yet exist).
    prior_text: str | None = None
    if not gates_disabled and not args.dry_run:
        try:
            _pre_path = _resolve_test_file(patch, repo)
            if _pre_path.exists():
                prior_text = _pre_path.read_text(encoding="utf-8")
        except Exception:
            prior_text = None

    try:
        result = apply_patch(
            patch,
            repo,
            templates_dir,
            dry_run=args.dry_run,
            stack=_stack_view(context_pack),
            authorized_imports=(context_pack or {}).get("allowedImports"),
        )
    except PermissionError as exc:
        print(f"[BLOCKED] {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        # Repairable generation defect (e.g. INVALID_JAVA_STRING_LITERAL). No
        # file was written. Exit 2 (FAIL, not BLOCKED) so the run-and-fix cycle
        # regenerates with a clear, actionable message instead of crashing.
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[FAIL] Unexpected error: {exc}", file=sys.stderr)
        return 1

    # ── G6 (static linter) post-write: lint what we rendered, AUTO-REPAIR via
    #    deterministic rules, then roll back only if it still fails ─────────────
    # M6 — reconnect repair_dispatch into the only code path that writes Java.
    # It was built but never invoked from here (audit H-2), so a fixable lint
    # violation went straight to a rollback instead of a deterministic repair.
    # evaluate_gates(auto_repair=True) owns the repair flow (gate_g6 → repair_
    # dispatch → re-lint); we reuse it rather than duplicating, and gate only on
    # the post-write G6 result (G1/G2/G5/G7/G8 already passed pre-write).
    if not gates_disabled and not args.dry_run:
        from gate_runner import evaluate_gates as _post_eval  # local import (same dir)

        written = Path(result["file"])
        post = _post_eval(
            patch,
            context_pack or {},
            state_dir,
            test_file=written,
            auto_repair=True,
            context_pack_path=(
                Path(args.context_pack).resolve() if args.context_pack else None
            ),
        )
        g6 = post.get("gates", {}).get("G6", {})
        if g6.get("status") == "FAIL":
            try:
                if prior_text is None:
                    written.unlink(missing_ok=True)
                else:
                    rb_tmp = written.with_suffix(written.suffix + ".tmp")
                    rb_tmp.write_text(prior_text, encoding="utf-8")
                    rb_tmp.replace(written)
            except OSError as exc:
                print(f"[WARN] G6 rollback failed: {exc}", file=sys.stderr)
            ar = g6.get("autoRepair") or {}
            print(
                f"[BLOCKED] gate G6_LINTER_FAIL "
                f"(violations={g6.get('violationCount')}; "
                f"autoRepair repaired={ar.get('repaired', 0)} "
                f"escalated={ar.get('escalated', 0)}; write rolled back)",
                file=sys.stderr,
            )
            # Emit the concrete linter violations (kind + message + line) so the
            # repair payload can carry WHAT to fix, not just "G6_LINTER_FAIL".
            vp = g6.get("violationsPath")
            violations: list = []
            try:
                if vp and Path(vp).exists():
                    violations = (json.loads(Path(vp).read_text(encoding="utf-8"))
                                  .get("violations") or [])
            except Exception:
                violations = []
            if violations:
                detail = " | ".join(
                    f"{v.get('kind') or v.get('rule') or 'LINT'}"
                    f"@L{v.get('line', '?')}: {v.get('message', '')}".strip()
                    for v in violations[:6] if isinstance(v, dict))
                if detail:
                    print("[BLOCKED-DETAIL] " + detail, file=sys.stderr)
            return 3

    _update_report(out_path, result, patch, dry_run=args.dry_run)

    prefix = "[DRY-RUN] " if args.dry_run else ""
    print(
        f"{prefix}[{result['action']}] {result['testClass']}\n"
        f"  file:     {result['file']}\n"
        f"  injected: {result['injectedMethods']}\n"
        f"  skipped:  {result['skippedMethods']} (signature collision)\n"
        f"  patchId:  {result['patchId']}"
    )
    return 0


if __name__ == "__main__":
    with _TimedRun("test_patch_applier") as _tr:
        _rc = main()
        if _rc != 0:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
