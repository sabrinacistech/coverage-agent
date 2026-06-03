"""test_m7_version_resolution_and_g5.py — classpath version resolution + G5 relax.

Two coupled fixes so a Spring Boot project (frameworks managed transitively by
the BOM, no explicit <version>) is no longer blocked:

  Detector: stack_profile_detector resolves junit/mockito/assertj versions from
            the resolved classpath (target/cp.txt) when the POM does not pin
            them — instead of emitting "unknown".

  G5:       gate_g5 blocks only on an unknown *framework* (test/mock/assert),
            namespace or java — NOT on an unknown framework *version*.

Run: `python tools/python/tests/test_m7_version_resolution_and_g5.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from gate_runner import gate_g5  # noqa: E402
from stack_profile_detector import _ModuleProfile, _classpath_versions  # noqa: E402


def _write_cp(mod_dir: Path, jars: list[str]) -> None:
    (mod_dir / "target").mkdir(parents=True, exist_ok=True)
    (mod_dir / "target" / "cp.txt").write_text(os.pathsep.join(jars), encoding="utf-8")


# ── Detector: classpath version resolution ────────────────────────────────────

def case_classpath_versions_parsed() -> None:
    with tempfile.TemporaryDirectory() as td:
        mod = Path(td)
        m2 = mod / ".m2"
        _write_cp(mod, [
            str(m2 / "org/junit/jupiter/junit-jupiter/5.10.5/junit-jupiter-5.10.5.jar"),
            str(m2 / "org/mockito/mockito-core/5.11.0/mockito-core-5.11.0.jar"),
            str(m2 / "org/assertj/assertj-core/3.25.3/assertj-core-3.25.3.jar"),
            str(mod / "target/cluster-status-service-0.0.1-SNAPSHOT.jar"),  # reactor jar → ignored
        ])
        m = _classpath_versions(mod)
        if m.get("junit-jupiter") != "5.10.5":
            raise AssertionError(f"junit-jupiter version wrong: {m}")
        if m.get("mockito-core") != "5.11.0":
            raise AssertionError(f"mockito-core version wrong: {m}")
        if m.get("assertj-core") != "3.25.3":
            raise AssertionError(f"assertj-core version wrong: {m}")
        if "cluster-status-service-0.0.1-SNAPSHOT" in m:
            raise AssertionError("reactor jar (target/, non-m2 layout) must be ignored")


def case_classpath_absent_is_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        if _classpath_versions(Path(td)) != {}:
            raise AssertionError("missing cp.txt must yield {} (graceful, stays 'unknown')")


def case_fill_only_fills_none_and_from_cp() -> None:
    cp_map = {"junit-jupiter": "5.10.5", "mockito-core": "5.11.0", "assertj-core": "3.25.3"}
    # None versions get filled
    p = _ModuleProfile("m")
    p.fill_versions_from_classpath(cp_map)
    if (p.junit_version, p.mockito_version, p.assertj_version) != ("5.10.5", "5.11.0", "3.25.3"):
        raise AssertionError(f"None versions not filled from classpath: {p.__dict__}")
    # explicit POM version must NOT be overwritten
    p2 = _ModuleProfile("m")
    p2.junit_version = "5.9.0"
    p2.fill_versions_from_classpath(cp_map)
    if p2.junit_version != "5.9.0":
        raise AssertionError("explicit POM version must win over classpath")
    # empty cp map → no change
    p3 = _ModuleProfile("m")
    p3.fill_versions_from_classpath({})
    if p3.junit_version is not None:
        raise AssertionError("empty cp map must leave versions None")


# ── G5: version-unknown must not block; framework-unknown must block ──────────

def case_g5_passes_when_only_versions_unknown_compact() -> None:
    # compact stk: [java, testFw, mockFw, assertFw, springEnabled, ns, testVer, mockVer, sbVer]
    pack = {"stk": ["21", "junit5", "mockito", "assertj", True, "jakarta",
                    "unknown", "unknown", "3.3.5"]}
    g5 = gate_g5(pack)
    if g5.get("status") != "PASS":
        raise AssertionError(f"G5 must PASS when only versions are unknown: {g5}")


def case_g5_fails_when_framework_unknown_compact() -> None:
    pack = {"stk": ["21", "unknown", "mockito", "assertj", True, "jakarta",
                    "5.10.5", "5.11.0", "3.3.5"]}
    g5 = gate_g5(pack)
    if g5.get("status") != "FAIL" or g5.get("blockedReason") != "G5_STACK_UNKNOWN":
        raise AssertionError(f"G5 must FAIL when a framework is unknown: {g5}")


def case_g5_verbose_dict_version_exempt() -> None:
    pack = {"stack": {"javaVersion": "21", "testFramework": "junit5",
                      "mockFramework": "mockito", "assertFramework": "assertj",
                      "namespaceStyle": "jakarta",
                      "testVersion": "unknown", "mockVersion": "unknown"}}
    if gate_g5(pack).get("status") != "PASS":
        raise AssertionError("verbose dict: unknown *Version keys must be exempt")
    pack_bad = {"stack": {"javaVersion": "21", "testFramework": "unknown",
                          "mockFramework": "mockito", "assertFramework": "assertj",
                          "namespaceStyle": "jakarta"}}
    if gate_g5(pack_bad).get("status") != "FAIL":
        raise AssertionError("verbose dict: unknown framework must still block")


def main() -> int:
    cases = [
        ("classpath-versions-parsed",            case_classpath_versions_parsed),
        ("classpath-absent-is-empty",            case_classpath_absent_is_empty),
        ("fill-only-none-and-from-cp",           case_fill_only_fills_none_and_from_cp),
        ("g5-pass-when-only-versions-unknown",   case_g5_passes_when_only_versions_unknown_compact),
        ("g5-fail-when-framework-unknown",       case_g5_fails_when_framework_unknown_compact),
        ("g5-verbose-dict-version-exempt",       case_g5_verbose_dict_version_exempt),
    ]
    failed = 0
    for name, fn in cases:
        try:
            fn()
            print(f"OK   {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    if failed:
        print(f"\n{failed} test(s) failed")
        return 1
    print("\nAll M7 cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
