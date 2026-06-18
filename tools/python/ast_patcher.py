"""ast_patcher.py — conservative text patcher for deterministic repair rules.

This is intentionally small. It only performs safe edits that do not require
semantic guessing. Unsupported actions fail closed and must be handled by the
Repair Agent fallback policy.

Actions:
  - addImport <fqcn>          — insert ``import <fqcn>;`` if whitelisted.
  - removeImport <fqcn>       — drop the matching ``import`` line.
  - insertAaaComments         — insert ``// given`` / ``// when`` / ``// then``
                                separators inside each ``@Test`` method body
                                that lacks them (G6 quality / TQG_02_NO_AAA).
  - removeUnusedStub <method> — drop stub lines that reference the named
                                mock method but are never invoked
                                (TQG_06_UNUSED_STUB).
  - convertMockSutToInjectMocks <fqcn>
                              — change ``@Mock`` to ``@InjectMocks`` for a
                                field whose declared type is exactly the SUT
                                (TQG_12_OVER_MOCK / E_MOCK_SUT).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from common import atomic_write_text, load_json
from framework_imports import (  # shared assert-framework source of truth
    AssertionFramework,
    OWNER_ASSERTJ,
    OWNER_JUNIT5_ASSERT,
    assertions_owner_for,
    assertions_owner_methods,
)

IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w\.]+(?:\.\*)?)\s*;\n?", re.MULTILINE)
PACKAGE_RE = re.compile(r"^\s*package\s+[\w\.]+\s*;\s*\n", re.MULTILINE)

# Match `@Test ... <returnType> <methodName>(<params>) { <body> }` conservatively.
# We capture the body and reinject AAA separators when none of // given,
# // when, // then are present.
_TEST_METHOD_RE = re.compile(
    r"(@Test[^\n]*\n(?:\s*@[^\n]*\n)*\s*(?:public\s+|protected\s+|private\s+)?"
    r"(?:static\s+)?\w[\w<>,\[\] ]*\s+(\w+)\s*\([^)]*\)\s*"
    r"(?:throws\s+[\w, ]+)?\s*\{)([\s\S]*?)(\n[ \t]*\})",
    re.MULTILINE,
)

_AAA_PRESENT = re.compile(r"//\s*(given|when|then)\b", re.IGNORECASE)


def is_import_allowed(fqcn: str, whitelist: dict) -> bool:
    target = fqcn.replace("static ", "")
    classes = {c.get("fqcn") for c in whitelist.get("classes", [])}
    packages = {p.get("name") for p in whitelist.get("packages", [])}
    if target in classes:
        return True
    owner = target.rsplit(".", 1)[0]
    return owner in packages


def add_import(text: str, imp: str, whitelist: dict) -> tuple[str, str | None]:
    if not is_import_allowed(imp, whitelist):
        return text, f"IMPORT_NOT_WHITELISTED: {imp}"
    line = f"import {imp};\n" if not imp.startswith("static ") else f"import static {imp[len('static '):]};\n"
    if line.strip() in {m.group(0).strip() for m in IMPORT_RE.finditer(text)}:
        return text, None
    imports = list(IMPORT_RE.finditer(text))
    if imports:
        pos = imports[-1].end()
        return text[:pos] + line + text[pos:], None
    pkg = PACKAGE_RE.search(text)
    if pkg:
        return text[:pkg.end()] + "\n" + line + text[pkg.end():], None
    return line + text, None


def remove_import(text: str, imp: str) -> str:
    escaped = re.escape(imp)
    return re.sub(rf"^\s*import\s+(?:static\s+)?{escaped}\s*;\s*\n", "", text, flags=re.MULTILINE)


# ── De-duplication by SimpleName (assert-framework exclusivity) ───────────────
# A generated test may end up declaring BOTH `import org.junit.jupiter.api.
# Assertions;` and `import org.assertj.core.api.Assertions;` (e.g. the LLM listed
# both in allowedImports). They share the SimpleName `Assertions`, so any
# `Assertions.assertEquals(...)` becomes an ambiguous reference and the file no
# longer compiles. This pass runs as the last text transform before disk: it keeps
# the import that matches the configured AssertionFramework and drops the loser,
# rewriting the loser's qualified call sites to its FQN so they still resolve.

# Qualified `Assertions.<method>` usage. Mirrors framework_imports' detector: the
# `(?<![\w.])` lookbehind makes the rewrite idempotent — once a call is FQN'd to
# `org.junit.jupiter.api.Assertions.x`, the inner `Assertions` is preceded by a
# dot and no longer matches.
_QUALIFIED_ASSERTIONS_RE = re.compile(r"(?<![\w.])Assertions\s*\.\s*(\w+)")
# The two `Assertions` owners that can collide on the simple name. JUnit 4 asserts
# through the differently-named `Assert`, so the collision is always jupiter↔assertj.
_ASSERTIONS_OWNERS: tuple[str, ...] = (OWNER_ASSERTJ, OWNER_JUNIT5_ASSERT)


def _mask_noise(text: str) -> str:
    """Length-preserving blanking of comments and string/char literals.

    Returns a string the SAME length as ``text`` with comment and literal regions
    replaced by spaces, so regex match offsets computed on the mask line up exactly
    with the original — letting us rewrite `Assertions.x` in real code while never
    touching one that appears inside a comment or a string literal.
    """
    def _blank(m: re.Match[str]) -> str:
        return " " * (m.end() - m.start())

    text = re.sub(r"/\*.*?\*/", _blank, text, flags=re.DOTALL)   # block comments
    text = re.sub(r"//[^\n]*", _blank, text)                      # line comments
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', _blank, text)           # string literals
    text = re.sub(r"'(?:\\.|[^'\\\n])*'", _blank, text)           # char literals
    return text


def _has_plain_import(text: str, fqcn: str) -> bool:
    return bool(re.search(rf"(?m)^[ \t]*import[ \t]+{re.escape(fqcn)}[ \t]*;", text))


def _ensure_plain_import(text: str, fqcn: str) -> str:
    """Insert ``import <fqcn>;`` after the last import (idempotent)."""
    if _has_plain_import(text, fqcn):
        return text
    line = f"import {fqcn};\n"
    imports = list(IMPORT_RE.finditer(text))
    if imports:
        pos = imports[-1].end()
        return text[:pos] + line + text[pos:]
    pkg = PACKAGE_RE.search(text)
    if pkg:
        return text[:pkg.end()] + "\n" + line + text[pkg.end():]
    return line + text


def _collapse_duplicate_imports(text: str) -> str:
    """Drop exact-duplicate ``import`` lines, keeping the first occurrence."""
    seen: set[str] = set()
    out: list[str] = []
    last = 0
    for m in IMPORT_RE.finditer(text):
        stmt = m.group(0).strip()
        if stmt in seen:
            out.append(text[last:m.start()])
            last = m.end()
        else:
            seen.add(stmt)
    out.append(text[last:])
    return "".join(out)


def _rewrite_qualified_to_fqn(
    text: str, owner_fqcn: str, methods: frozenset[str]
) -> str:
    """Rewrite code-region `Assertions.<m>` → `<owner_fqcn>.<m>` for ``m`` in the
    given dialect's ``methods`` set. Comments / string literals are left intact."""
    if not methods:
        return text
    mask = _mask_noise(text)
    out: list[str] = []
    last = 0
    for m in _QUALIFIED_ASSERTIONS_RE.finditer(mask):
        if m.group(1) not in methods:
            continue
        start, end = m.start(), m.end()
        # Same span in the real text; swap only the leading `Assertions` token,
        # preserving any whitespace around the dot and the method name.
        rewritten = re.sub(r"Assertions", owner_fqcn, text[start:end], count=1)
        out.append(text[last:start])
        out.append(rewritten)
        last = end
    out.append(text[last:])
    return "".join(out)


def _dedup_imports_by_simple_name(
    text: str, assert_fw: "str | AssertionFramework | None" = None
) -> str:
    """Resolve a ``Assertions`` simple-name collision before writing to disk.

    Precedence is the configured :class:`AssertionFramework`: AssertJ stacks keep
    ``org.assertj.core.api.Assertions``; JUnit-builtin / Hamcrest stacks keep
    ``org.junit.jupiter.api.Assertions``. The winning import is the only one left;
    every qualified call belonging to the *losing* dialect is rewritten to that
    dialect's FQN so it still compiles. No collision (≤1 owner involved) → the text
    is returned unchanged, and a second pass is a no-op (idempotent).
    """
    text = _collapse_duplicate_imports(text)

    present = [owner for owner in _ASSERTIONS_OWNERS if _has_plain_import(text, owner)]

    # Which dialects does the body actually call through the bare `Assertions` type?
    mask = _mask_noise(text)
    used: set[str] = set()
    for m in _QUALIFIED_ASSERTIONS_RE.finditer(mask):
        method = m.group(1)
        if method in assertions_owner_methods(OWNER_ASSERTJ):
            used.add(OWNER_ASSERTJ)
        elif method in assertions_owner_methods(OWNER_JUNIT5_ASSERT):
            used.add(OWNER_JUNIT5_ASSERT)

    candidates = set(present) | used
    if len(candidates) < 2:
        return text  # no collision possible — nothing to resolve

    winner = assertions_owner_for(assert_fw)  # AssertJ vs JUnit, by config
    if winner not in candidates:
        # Configured winner isn't even in play (e.g. AssertJ-config but only JUnit
        # present+used): keep the single coherent dialect rather than forcing one.
        winner = next(iter(candidates)) if len(candidates) == 1 else (
            OWNER_JUNIT5_ASSERT if OWNER_JUNIT5_ASSERT in candidates else OWNER_ASSERTJ
        )

    for loser in candidates - {winner}:
        text = _rewrite_qualified_to_fqn(text, loser, assertions_owner_methods(loser))
        text = remove_import(text, loser)
    text = _ensure_plain_import(text, winner)
    return text


# ── New deterministic actions ────────────────────────────────────────────────

def insert_aaa_comments(text: str) -> tuple[str, int]:
    """Insert `// given` / `// when` / `// then` separators into @Test method
    bodies that lack them. Heuristic split:

      - `// given`  → first non-blank line of the body;
      - `// when`   → the line that calls a SUT method (best effort: the
                       first statement after the first blank-line gap, or
                       the line that contains ``=`` with a call);
      - `// then`   → the line that begins with ``assert`` / ``verify`` /
                       ``Assertions.``.

    Falls back to prepending only ``// given`` when the heuristic can't
    classify the structure (still a strict improvement: TQG_02 only requires
    the separators to exist as a hint, not a perfect split).
    """
    changed = 0

    def _rewrite(match: re.Match[str]) -> str:
        nonlocal changed
        head, _name, body, tail = match.group(1), match.group(2), match.group(3), match.group(4)
        if _AAA_PRESENT.search(body):
            return match.group(0)
        # Compute indentation from the first non-empty line.
        lines = body.split("\n")
        first_non_empty = next((ln for ln in lines if ln.strip()), "")
        indent_match = re.match(r"^[ \t]*", first_non_empty)
        indent = indent_match.group(0) if indent_match else "        "

        # Locate index of first assert/verify line for the // then marker.
        then_idx: int | None = None
        for i, ln in enumerate(lines):
            stripped = ln.lstrip()
            if stripped.startswith(("assert", "verify", "Assertions.", "assertThat", "assertThrows")):
                then_idx = i
                break

        # Locate index of likely // when line (first statement with `=` and
        # `(`, or first line after a blank gap, that isn't an assert).
        when_idx: int | None = None
        for i, ln in enumerate(lines):
            stripped = ln.lstrip()
            if then_idx is not None and i >= then_idx:
                break
            if "=" in stripped and "(" in stripped and not stripped.startswith(("assert", "verify")):
                when_idx = i
                break
        if when_idx is None and then_idx is not None and then_idx > 0:
            # First non-blank line before the assert that isn't a setup stub.
            for i in range(then_idx - 1, -1, -1):
                stripped = lines[i].lstrip()
                if stripped and not stripped.startswith(("when(", "doReturn", "doThrow", "given(", "//")):
                    when_idx = i
                    break

        # Build new body lines with markers inserted.
        out: list[str] = []
        first_non_empty_seen = False
        for i, ln in enumerate(lines):
            if not first_non_empty_seen and ln.strip():
                out.append(f"{indent}// given")
                first_non_empty_seen = True
            if when_idx is not None and i == when_idx:
                out.append(f"{indent}// when")
            if then_idx is not None and i == then_idx:
                out.append(f"{indent}// then")
            out.append(ln)
        if not first_non_empty_seen:
            return match.group(0)  # empty body — leave alone

        changed += 1
        return head + "\n".join(out) + tail

    new_text = _TEST_METHOD_RE.sub(_rewrite, text)
    return new_text, changed


def remove_unused_stub(text: str, method: str) -> tuple[str, int]:
    """Drop ``when(<x>.<method>(...)).thenReturn|thenThrow|thenAnswer(...)``
    and the equivalent ``doReturn(...).when(<x>).<method>(...)`` lines.

    The ``method`` argument is the bare method name; the receiver and args
    are wildcarded. Removed lines are reported via the int return value.
    """
    if not method or not re.match(r"^\w+$", method):
        return text, 0
    me = re.escape(method)
    patterns = [
        rf"^\s*when\([^;]*\.{me}\s*\([^;]*\)\s*\)\s*\.(?:thenReturn|thenThrow|thenAnswer)\([^;]*\)\s*;\s*\n",
        rf"^\s*do(?:Return|Throw|Answer|Nothing)\([^;]*\)\s*\.when\([^;]*\)\s*\.{me}\s*\([^;]*\)\s*;\s*\n",
        rf"^\s*given\([^;]*\.{me}\s*\([^;]*\)\s*\)\s*\.willReturn\([^;]*\)\s*;\s*\n",
    ]
    removed = 0
    new_text = text
    for pat in patterns:
        compiled = re.compile(pat, re.MULTILINE | re.DOTALL)
        new_text, n = compiled.subn("", new_text)
        removed += n
    return new_text, removed


_SUT_FIELD_NAME_RE_TEMPLATE = (
    r"@InjectMocks\b[^\n]*\n?\s*"
    r"(?:@[\w.]+(?:\([^)]*\))?\s*\n?\s*)*"
    r"(?:(?:private|protected|public|final|static)\s+)*"
    r"{se}\s+(\w+)\s*[;=]"
)


def convert_mock_sut_to_inject_mocks(text: str, sut_simple_name: str) -> tuple[str, int]:
    """Convert SUT-mocking into proper Mockito @InjectMocks wiring.

    Three deterministic transformations, performed in order:

      1. Flip ``@Mock`` → ``@InjectMocks`` on fields whose declared type is
         exactly ``sut_simple_name`` (both same-line and multi-line layouts).
      2. Drop local declarations of the form
         ``<SUT> <localVar> = mock(<SUT>.class);`` — once a field carries
         ``@InjectMocks``, the local mock is redundant *and* shadows the SUT
         injection point Mockito set up.
      3. Rewrite references to those local variables to point at the
         ``@InjectMocks`` field. Skipped when no SUT field exists, since we'd
         leave dangling identifiers; the LLM repair-agent picks that up via
         the residual ``TQG_12_OVER_MOCK_SUT`` violation.

    Idempotent: already-converted fields and already-removed local mocks
    pass through unchanged. Returns the total number of mutations applied.
    """
    if not sut_simple_name or not re.match(r"^\w+$", sut_simple_name):
        return text, 0
    se = re.escape(sut_simple_name)
    converted = 0

    # 1a. Same-line: `@Mock <maybe modifiers> SUT <name>;`
    same_line = re.compile(
        rf"(^\s*)@Mock(\s+)((?:(?:private|protected|public|final|static)\s+)*){se}\b",
        re.MULTILINE,
    )
    text, n = same_line.subn(
        lambda m: f"{m.group(1)}@InjectMocks{m.group(2)}{m.group(3)}{sut_simple_name}", text
    )
    converted += n

    # 1b. Multi-line: `@Mock` on its own line, then the field declaration.
    multi_line = re.compile(
        rf"(^\s*)@Mock(\s*)\n(\s*(?:@[\w.]+\s*\n\s*)*)((?:(?:private|protected|public|final|static)\s+)*{se}\b)",
        re.MULTILINE,
    )
    text, n = multi_line.subn(
        lambda m: f"{m.group(1)}@InjectMocks{m.group(2)}\n{m.group(3)}{m.group(4)}", text
    )
    converted += n

    # 2. Identify the @InjectMocks SUT field name (post-step-1) so we know
    #    where to redirect local references. Without it we leave local mocks
    #    alone — removing them would dangle every downstream identifier.
    sut_field_re = re.compile(_SUT_FIELD_NAME_RE_TEMPLATE.format(se=se))
    sut_field_match = sut_field_re.search(text)
    sut_field = sut_field_match.group(1) if sut_field_match else None

    # 3. Drop local `<SUT> <var> = mock(<SUT>.class);` declarations and
    #    capture the var names so we can rewrite references.
    local_decl = re.compile(
        rf"^[ \t]*(?:final\s+)?{se}\s+(\w+)\s*=\s*mock\s*\(\s*{se}\.class\s*\)\s*;[ \t]*\n",
        re.MULTILINE,
    )
    local_vars: list[str] = [m.group(1) for m in local_decl.finditer(text)]

    if local_vars and sut_field:
        text = local_decl.sub("", text)
        for var in local_vars:
            # Whole-word replace; never touch the SUT field name even if it
            # collides (it shouldn't, but be defensive).
            if var == sut_field:
                continue
            text = re.sub(rf"\b{re.escape(var)}\b", sut_field, text)
        converted += len(local_vars)

    return text, converted


# ── CLI ──────────────────────────────────────────────────────────────────────

_ACTIONS_WITHOUT_ARG = frozenset({"insertAaaComments"})
_ACTIONS_WITH_ARG = frozenset({
    "addImport", "removeImport", "removeUnusedStub", "convertMockSutToInjectMocks",
})
_ALL_ACTIONS = sorted(_ACTIONS_WITHOUT_ARG | _ACTIONS_WITH_ARG)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--action", required=True, choices=_ALL_ACTIONS)
    ap.add_argument("--arg", default=None,
                    help="Required for addImport/removeImport/removeUnusedStub/convertMockSutToInjectMocks")
    ap.add_argument("--whitelist", default=None,
                    help="Path to import-whitelist.json (required for addImport)")
    ap.add_argument("--assert-fw", default=None, dest="assert_fw",
                    help=(
                        "Configured assert framework (assertj|hamcrest|junit-builtin). "
                        "Breaks an `Assertions` simple-name collision on addImport by "
                        "AssertionFramework precedence before writing."
                    ))
    args = ap.parse_args()

    if args.action in _ACTIONS_WITH_ARG and not args.arg:
        ap.error(f"--arg is required for action={args.action}")

    path = Path(args.file)
    text = path.read_text(encoding="utf-8", errors="ignore")

    if args.action == "addImport":
        if not args.whitelist:
            ap.error("--whitelist is required for action=addImport")
        whitelist = load_json(Path(args.whitelist))
        new_text, err = add_import(text, args.arg, whitelist)
        if err:
            print(json.dumps({"status": "BLOCKED", "reason": err}, indent=2))
            return 1
        # addImport is the action that can introduce a second `Assertions` import;
        # resolve any resulting simple-name collision deterministically before disk.
        new_text = _dedup_imports_by_simple_name(new_text, args.assert_fw)
        report: dict = {"status": "OK", "changed": new_text != text}
    elif args.action == "removeImport":
        new_text = remove_import(text, args.arg)
        report = {"status": "OK", "changed": new_text != text}
    elif args.action == "insertAaaComments":
        new_text, n = insert_aaa_comments(text)
        report = {"status": "OK", "changed": new_text != text, "methodsPatched": n}
    elif args.action == "removeUnusedStub":
        new_text, n = remove_unused_stub(text, args.arg)
        report = {"status": "OK", "changed": new_text != text, "stubsRemoved": n}
    elif args.action == "convertMockSutToInjectMocks":
        new_text, n = convert_mock_sut_to_inject_mocks(text, args.arg)
        report = {"status": "OK", "changed": new_text != text, "fieldsConverted": n}
    else:  # pragma: no cover — argparse choices guard this
        ap.error(f"unknown action: {args.action}")
        return 2

    if new_text != text:
        # Atomic (tmp + replace): a crash/AV interruption mid-write must never
        # leave a half-written Java test on disk (audit H-3).
        atomic_write_text(path, new_text)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
