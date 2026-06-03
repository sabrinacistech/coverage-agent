"""test_m4_baseline_and_encoding.py — regression for the M4 audit fixes.

M4a (narrow_test_runner): the Maven subprocess decodes stdout as UTF-8 with
    errors="replace", so a UTF-8 byte from Maven (emoji/box-drawing) can never
    raise UnicodeDecodeError under the Windows cp1252 default and kill the cycle.

M4b (run_pipeline.snapshot_baseline): the pre-generation JaCoCo report is
    snapshotted to state/jacoco-baseline.xml — the canonical `--before` image
    for per-cycle delta computation — write-if-absent so re-runs never move the
    baseline forward.

Run: `python tools/python/tests/test_m4_baseline_and_encoding.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from run_pipeline import snapshot_baseline  # noqa: E402

_NARROW_RUNNER = HERE.parent / "narrow_test_runner.py"


# ── M4b: baseline snapshot ────────────────────────────────────────────────────

def case_snapshot_writes_when_absent() -> None:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src = tdp / "jacoco.xml"
        src.write_bytes(b"<report>baseline</report>")
        out = tdp / "state"
        result = snapshot_baseline(src, out)
        baseline = out / "jacoco-baseline.xml"
        if result != baseline:
            raise AssertionError(f"expected returned path {baseline}, got {result}")
        if not baseline.exists():
            raise AssertionError("baseline file was not created")
        if baseline.read_bytes() != b"<report>baseline</report>":
            raise AssertionError("baseline content does not match source")


def case_snapshot_idempotent_does_not_overwrite() -> None:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src = tdp / "jacoco.xml"
        src.write_bytes(b"<report>new</report>")
        out = tdp / "state"
        out.mkdir()
        baseline = out / "jacoco-baseline.xml"
        baseline.write_bytes(b"<report>original</report>")   # pre-existing baseline
        result = snapshot_baseline(src, out)
        if result is not None:
            raise AssertionError("idempotent call should return None (no write)")
        if baseline.read_bytes() != b"<report>original</report>":
            raise AssertionError("baseline must NOT be overwritten when it already exists")


def case_snapshot_force_overwrites() -> None:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src = tdp / "jacoco.xml"
        src.write_bytes(b"<report>new</report>")
        out = tdp / "state"
        out.mkdir()
        (out / "jacoco-baseline.xml").write_bytes(b"<report>original</report>")
        result = snapshot_baseline(src, out, force=True)
        if result is None or result.read_bytes() != b"<report>new</report>":
            raise AssertionError("force=True must recapture the baseline")


def case_snapshot_missing_source_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        out = tdp / "state"
        result = snapshot_baseline(tdp / "does-not-exist.xml", out)
        if result is not None:
            raise AssertionError("missing source must be a no-op returning None")
        if (out / "jacoco-baseline.xml").exists():
            raise AssertionError("no baseline should be written for a missing source")


# ── M4a: narrow_test_runner UTF-8 decode tripwire ─────────────────────────────

def case_narrow_runner_decodes_utf8() -> None:
    # Tripwire: the Popen that reads Maven stdout must pin UTF-8 + errors=replace.
    # A behavioural test would require a real Maven; this guards the regression
    # that bit the audited run (cp1252 UnicodeDecodeError mid-build).
    src = _NARROW_RUNNER.read_text(encoding="utf-8")
    if 'encoding="utf-8"' not in src:
        raise AssertionError("narrow_test_runner must pin encoding=\"utf-8\" on the Maven Popen")
    if 'errors="replace"' not in src:
        raise AssertionError("narrow_test_runner must pass errors=\"replace\" on the Maven Popen")


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    cases = [
        ("snapshot-writes-when-absent",        case_snapshot_writes_when_absent),
        ("snapshot-idempotent-no-overwrite",   case_snapshot_idempotent_does_not_overwrite),
        ("snapshot-force-overwrites",          case_snapshot_force_overwrites),
        ("snapshot-missing-source-noop",       case_snapshot_missing_source_is_noop),
        ("narrow-runner-decodes-utf8",         case_narrow_runner_decodes_utf8),
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
    print("\nAll M4 cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
