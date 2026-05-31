"""test_body_validation.py — _validate_body() blocks forbidden Java structures.

Runs the patcher as a subprocess against a temporary repo. The patch carries a
method body containing a top-level `import` statement. Expected outcome:

  * exit code == 3 (PermissionError path in main()).
  * Target test file is NOT written to disk.
  * "FORBIDDEN_JAVA_STRUCTURE_IN_BODY" appears in stdout or stderr.

Exits non-zero on any failure.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent.parent  # java-test-coverage-architecture/
PATCHER = HERE.parent / "test_patch_applier.py"
TEMPLATES_DIR = PROJECT_ROOT / "templates"


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = tmp / "repo"
        state = tmp / "state"
        (repo / "src" / "test" / "java" / "com" / "acme").mkdir(parents=True)
        state.mkdir()

        patch = {
            "patchId": "test-forbidden-body",
            "schemaVersion": 1,
            "cycle": 1,
            "sut": "com.acme.FooService",
            "testClass": "com.acme.FooServiceTest",
            "testPackage": "com.acme",
            "template": "junit5-mockito",
            "targetDir": "src/test/java",
            "allowedImports": [],
            "fields": [],
            "methods": [
                {
                    "name": "shouldFailValidation",
                    "annotations": ["@Test"],
                    "body": "import com.evil.X;\n// when\nsut.foo();",
                    "evidenceIds": ["evt:1"],
                }
            ],
        }
        patch_path = state / "FooServiceTest.patch.json"
        patch_path.write_text(json.dumps(patch), encoding="utf-8")

        target_file = (
            repo / "src" / "test" / "java" / "com" / "acme" / "FooServiceTest.java"
        )

        proc = subprocess.run(
            [
                sys.executable,
                str(PATCHER),
                "--patch", str(patch_path),
                "--repo", str(repo),
                "--state", str(state),
                "--templates", str(TEMPLATES_DIR),
                "--out", str(state / "generated-tests.json"),
                # Isolate the body-validation path; gate enforcement is covered
                # by the patcher's own integration test (test_patcher_gates.py).
                "--no-gates",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "TPA_ALLOW_NO_GATES": "1"},
        )

        problems: list[str] = []
        if proc.returncode != 3:
            problems.append(
                f"exit code: expected 3, got {proc.returncode}\n"
                f"  stdout: {proc.stdout!r}\n  stderr: {proc.stderr!r}"
            )
        if target_file.exists():
            problems.append(f"target file should not exist: {target_file}")
        combined = (proc.stdout or "") + (proc.stderr or "")
        if "FORBIDDEN_JAVA_STRUCTURE_IN_BODY" not in combined:
            problems.append(
                "expected 'FORBIDDEN_JAVA_STRUCTURE_IN_BODY' in stdout/stderr; "
                f"got stdout={proc.stdout!r} stderr={proc.stderr!r}"
            )

        if problems:
            print("FAIL test_body_validation:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("OK   test_body_validation: exit=3, no file written, marker present")
        return 0


if __name__ == "__main__":
    sys.exit(main())
