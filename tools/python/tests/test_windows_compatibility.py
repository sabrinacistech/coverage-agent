"""test_windows_compatibility.py — regression tests for the Windows fixes.

Runs as a plain script: ``python tools/python/tests/test_windows_compatibility.py``.
No external runner required. Exits non-zero on any failure.

Each case targets a specific bug that was fixed in this refactor pass, so any
future change that regresses one of them surfaces here immediately:

  A. classpath_resolver uses mvn_executable() (not bare "mvn")
  B. common.run() decodes child output as UTF-8 (non-ASCII passes through)
  C. sys.stdout is reconfigured to utf-8 when common is imported
  D. resolve_target_dirs handles monolithic repos (target/classes at root)
  E. bytecode_scanner.TYPE_DECL_RE skips `Compiled from "Foo.java"` lines
  F. archetype-profile.schema.json accepts changelog: null
  G. fixture_catalog_builder Strategy 5 emits mock+degraded, not "none"
  H. long_path() prefixes \\?\\ on Windows and is a no-op on POSIX
  I. normalize_params() is idempotent and coerces legacy string params
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS))

import common  # noqa: E402
from common import (  # noqa: E402
    IS_WINDOWS,
    long_path,
    mvn_executable,
    normalize_params,
    resolve_target_dirs,
    run,
    validate,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        _FAILURES.append(f"{label}: FAIL {detail}".rstrip())
        print(f"  [FAIL] {label} {detail}")
    else:
        print(f"  [ OK ] {label}")


# ── Cases ─────────────────────────────────────────────────────────────────────

def case_A_classpath_uses_mvn_executable() -> None:
    print("\nA. classpath_resolver uses mvn_executable()")
    import classpath_resolver as cr

    src = inspect.getsource(cr.resolve_module)
    _check("mvn_executable() referenced", "mvn_executable()" in src)

    # No bare ["mvn", ...] literal in non-comment lines.
    code_lines = [
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    ]
    bare = [line for line in code_lines if '["mvn",' in line.replace(" ", "")]
    _check("no bare [\"mvn\", ...] literal remains", not bare, f"hits={bare}")


def case_B_run_decodes_utf8() -> None:
    print("\nB. common.run() decodes child output as UTF-8")
    # The child writes raw utf-8 bytes to stdout.buffer so this isolates the
    # parent's *decoding* behavior from the child's own text-layer encoding
    # (which defaults to cp1252 in Python on Windows and would otherwise
    # short-circuit the test before run() ever sees the bytes).
    payload = "héllo — ñ ✓"
    child = (
        "import sys; "
        f"sys.stdout.buffer.write({payload.encode('utf-8')!r})"
    )
    cp = run([sys.executable, "-c", child])
    _check("returncode == 0", cp.returncode == 0,
           f"rc={cp.returncode} stderr={cp.stderr!r}")
    _check("stdout decoded as utf-8", payload in cp.stdout,
           f"stdout={cp.stdout!r}")


def case_C_stdout_reconfigured_to_utf8() -> None:
    print("\nC. stdout is reconfigured to utf-8 after importing common")
    enc = getattr(sys.stdout, "encoding", "") or ""
    _check("stdout encoding is utf-8", enc.lower().replace("-", "") == "utf8",
           f"encoding={enc!r}")


def case_D_resolve_target_dirs_monolithic() -> None:
    print("\nD. resolve_target_dirs handles monolithic repos")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        # Pretend this is a monolithic project: pom.xml + target/classes at root.
        (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
        classes = repo / "target" / "classes"
        classes.mkdir(parents=True)

        # No --module specified → should locate the root target/classes.
        dirs = resolve_target_dirs(repo, None)
        _check("monolithic root detected", classes in [d.resolve() for d in dirs],
               f"dirs={dirs}")

        # --module "." also yields the same.
        dirs_dot = resolve_target_dirs(repo, ".")
        _check("--module '.' resolves to root", classes in [d.resolve() for d in dirs_dot],
               f"dirs={dirs_dot}")


def case_E_bytecode_skips_compiled_from() -> None:
    print("\nE. bytecode_scanner skips 'Compiled from' line")
    import bytecode_scanner as bs

    sample = (
        'Compiled from "Foo.java"\n'
        "public class com.acme.Foo {\n"
        "  public com.acme.Foo();\n"
        "    descriptor: ()V\n"
        "}\n"
    )
    matches = [
        (idx, line)
        for idx, line in enumerate(sample.splitlines())
        if bs.TYPE_DECL_RE.search(line)
    ]
    _check("at least one declaration matched", bool(matches), f"matches={matches}")
    first_idx, first_line = matches[0]
    _check("'Compiled from' is NOT the first match",
           "Compiled from" not in first_line, f"first_line={first_line!r}")
    m = bs.TYPE_DECL_RE.search(first_line)
    _check("FQCN extracted as com.acme.Foo",
           m and m.group(2) == "com.acme.Foo",
           f"got={m and m.group(2)!r}")


def case_F_archetype_schema_allows_null_changelog() -> None:
    print("\nF. archetype-profile schema accepts changelog: null")
    sample = {
        "schemaVersion": 1,
        "modules": [
            {
                "path": "/tmp/m",
                "archetype": "java-21",
                "implies": {"java": "21"},
                "changelog": None,
                "rulesApplied": [],
                "discrepancies": [],
            }
        ],
    }
    try:
        validate("archetype-profile", sample)
        ok = True
        err = ""
    except Exception as exc:
        ok = False
        err = str(exc)
    _check("validate('archetype-profile') with null changelog", ok, err)


def case_G_fixture_builder_emits_degraded_mock() -> None:
    print("\nG. fixture_catalog_builder Strategy 5 → mock + degraded")
    import fixture_catalog_builder as fcb

    # Class with no public ctor, no builders, no static factory, not interface/abstract.
    contract = {
        "fqcn": "com.acme.SealedThing",
        "kind": "class",
        "constructors": [],
        "builders": [],
        "methods": [],
    }
    fixture = fcb._build_fixture(contract, sut_type="component", mockito_ok=True)
    _check("strategy is 'mock'", fixture["strategy"] == "mock",
           f"strategy={fixture.get('strategy')!r}")
    _check("degraded flag is True", fixture.get("degraded") is True,
           f"degraded={fixture.get('degraded')!r}")
    _check("cycleSafe is False", fixture.get("cycleSafe") is False,
           f"cycleSafe={fixture.get('cycleSafe')!r}")

    catalog = {"schemaVersion": 1, "fixtures": [fixture]}
    try:
        validate("fixture-catalog", catalog)
        ok = True
        err = ""
    except Exception as exc:
        ok = False
        err = str(exc)
    _check("fixture passes schema validation", ok, err)

    # A legit interface mock must NOT carry degraded=true.
    iface = {"fqcn": "com.acme.Repo", "kind": "interface", "constructors": [],
             "builders": [], "methods": []}
    legit = fcb._build_fixture(iface, "service", True)
    _check("legit interface mock has no 'degraded'", "degraded" not in legit,
           f"keys={sorted(legit.keys())}")


def case_H_long_path_behavior() -> None:
    print("\nH. long_path() platform behavior")
    sample = Path(".") / "tools" / "python" / "common.py"
    out = long_path(sample)
    if IS_WINDOWS:
        _check("Windows: starts with \\\\?\\", out.startswith("\\\\?\\"),
               f"out={out!r}")
    else:
        # On POSIX it's a no-op (returns the string as-is).
        _check("POSIX: identity", out == str(sample),
               f"out={out!r}")


def case_I_normalize_params_shapes() -> None:
    print("\nI. normalize_params() is idempotent and coerces strings")
    # Strings → dicts
    out1 = normalize_params(["String", "int"])
    _check("strings become [{type}, ...]",
           out1 == [{"type": "String"}, {"type": "int"}],
           f"got={out1}")

    # Dicts pass through unchanged
    src = [{"type": "java.lang.String", "name": "msg"}]
    out2 = normalize_params(src)
    _check("dicts unchanged (idempotent)", out2 == src, f"got={out2}")

    # Mixed input is handled
    out3 = normalize_params(["String", {"type": "int", "name": "n"}, None])
    _check("mixed input handled deterministically",
           len(out3) == 3 and out3[0] == {"type": "String"}
           and out3[1] == {"type": "int", "name": "n"}
           and out3[2] == {"type": "java.lang.Object"},
           f"got={out3}")

    # Empty / falsy
    _check("empty list → []", normalize_params([]) == [])
    _check("None → []", normalize_params(None) == [])


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    cases = [
        case_A_classpath_uses_mvn_executable,
        case_B_run_decodes_utf8,
        case_C_stdout_reconfigured_to_utf8,
        case_D_resolve_target_dirs_monolithic,
        case_E_bytecode_skips_compiled_from,
        case_F_archetype_schema_allows_null_changelog,
        case_G_fixture_builder_emits_degraded_mock,
        case_H_long_path_behavior,
        case_I_normalize_params_shapes,
    ]
    print(f"Running {len(cases)} regression case(s)...")
    for case in cases:
        try:
            case()
        except Exception as exc:
            _FAILURES.append(f"{case.__name__}: raised {type(exc).__name__}: {exc}")
            print(f"  [FAIL] {case.__name__}: {type(exc).__name__}: {exc}")

    print()
    if _FAILURES:
        print(f"[FAIL] {len(_FAILURES)} failure(s):")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print(f"[OK] all {len(cases)} cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
