"""classpath_resolver.py — resolve test classpath and produce import-whitelist.json.

Strategy: run `mvn dependency:build-classpath -DincludeScope=test` per module,
collect jars, list FQCNs and packages from each jar via `zipfile`, plus JDK packages
from JAVA_HOME, plus local source/generated packages.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from common import (
    atomic_write_json,
    find_pom_modules,
    long_path,
    mvn_executable,
    run,
    validate,
)


def _list_classes_in_jar(jar: Path) -> tuple[set[str], set[str]]:
    packages: set[str] = set()
    fqcns: set[str] = set()
    try:
        with zipfile.ZipFile(jar) as zf:
            for n in zf.namelist():
                if n.endswith(".class") and "$" not in n:
                    fq = n[:-6].replace("/", ".")
                    fqcns.add(fq)
                    if "." in fq:
                        packages.add(fq.rsplit(".", 1)[0])
    except (zipfile.BadZipFile, FileNotFoundError):
        pass
    return packages, fqcns


def _jdk_packages() -> set[str]:
    """Collect package names from JAVA_HOME (jrt-fs for 9+, rt.jar for 8)."""
    pkgs: set[str] = set()
    java_home = os.environ.get("JAVA_HOME")
    if not java_home:
        return pkgs
    jh = Path(java_home)
    # Java 8
    rt = jh / "jre" / "lib" / "rt.jar"
    if rt.exists():
        p, _ = _list_classes_in_jar(rt)
        pkgs |= p
        return pkgs
    # Java 9+: use `jrt:/` via `java --list-modules` + `jmod list`
    try:
        out = run([str(jh / "bin" / "java"), "--list-modules"]).stdout
        modules = [line.split("@")[0].strip() for line in out.splitlines() if line.strip()]
        for m in modules:
            try:
                # jmod might not exist in JRE; ignore failures
                jmod = jh / "bin" / "jmod"
                if not jmod.exists():
                    continue
                r = run([str(jmod), "list", str(jh / "jmods" / f"{m}.jmod")])
                for line in r.stdout.splitlines():
                    if line.startswith("classes/") and line.endswith(".class") and "$" not in line:
                        fq = line[len("classes/"):-6].replace("/", ".")
                        if "module-info" in fq:
                            continue
                        if "." in fq:
                            pkgs.add(fq.rsplit(".", 1)[0])
            except subprocess.CalledProcessError:
                continue
    except Exception:
        pass
    return pkgs


def _walk_source_packages(roots: list[Path]) -> set[str]:
    pkgs: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        # long_path() opts into Windows long-path API for generated-sources trees
        # that easily exceed MAX_PATH (260 chars) under target/.
        for p, dirs, files in os.walk(long_path(root)):
            dirs.sort()
            for fn in sorted(files):
                if not fn.endswith(".java"):
                    continue
                fp = Path(p) / fn
                try:
                    with open(long_path(fp), "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("package "):
                                pkgs.add(line[len("package "):].rstrip(";").strip())
                                break
                except Exception:
                    pass
    return pkgs


def resolve_module(mod_dir: Path) -> dict:
    cp_file = mod_dir / "target" / "cp.txt"
    cp_file.parent.mkdir(parents=True, exist_ok=True)
    # Use mvn_executable() so Windows resolves mvn.cmd correctly; bare "mvn"
    # would fail subprocess lookup because PATHEXT is not honored without a shell.
    cmd = [
        mvn_executable(), "-q", "-pl", ".", "dependency:build-classpath",
        "-DincludeScope=test", f"-Dmdep.outputFile={cp_file}",
    ]
    rc = run(cmd, cwd=mod_dir, timeout=900).returncode
    classes_out: list[dict] = []
    packages_out: dict[str, str] = {}  # name -> origin
    if cp_file.exists():
        cp_text = cp_file.read_text(encoding="utf-8").strip()
        jars = [Path(p.strip()) for p in cp_text.split(os.pathsep) if p.strip()]
        for jar in jars:
            pkgs, fqcns = _list_classes_in_jar(jar)
            for p in pkgs:
                packages_out.setdefault(p, "dep")
            for fq in fqcns:
                classes_out.append({"fqcn": fq, "origin": "dep", "jar": jar.name})
    # JDK
    for p in _jdk_packages():
        packages_out.setdefault(p, "jdk")
    # Source + generated
    for p in _walk_source_packages(
        [mod_dir / "src" / "main" / "java", mod_dir / "src" / "test" / "java"]
    ):
        packages_out.setdefault(p, "source")
    for p in _walk_source_packages([mod_dir / "target" / "generated-sources"]):
        packages_out.setdefault(p, "generated")

    # Sort classes by FQCN for reproducible output (post-audit 2026-05-28).
    # The jar iteration order Maven gives us is not stable across runs, which
    # broke the input-hash cache for every downstream step. Consumers build a
    # set from this list, so order is purely cosmetic — sorting is free.
    classes_sorted = sorted(classes_out, key=lambda c: (c.get("fqcn", ""), c.get("jar", "")))

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "module": mod_dir.name,
        "packages": [{"name": k, "origin": v} for k, v in sorted(packages_out.items())],
        "classes": classes_sorted[:50000],  # cap to avoid huge files
        "_meta": {"resolveExit": rc},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--module", default=None, help="Restrict to one module by name")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state_dir = Path(args.out).resolve()
    out_dir = state_dir / "import-whitelist"
    out_dir.mkdir(parents=True, exist_ok=True)

    modules = find_pom_modules(repo, contract=state_dir / "build-tool-contract.json")
    if args.module and args.module not in (".", ""):
        modules = [m for m in modules if m.name == args.module]
    if not modules:
        print("[FAIL] no Maven modules", file=sys.stderr)
        return 2

    primary = None
    for m in modules:
        wl = resolve_module(m)
        try:
            # Strip _meta before validating
            for_validation = {k: v for k, v in wl.items() if not k.startswith("_")}
            validate("import-whitelist", for_validation)
        except Exception as e:
            print(f"[WARN] schema validation failed for {m.name}: {e}", file=sys.stderr)
        atomic_write_json(out_dir / f"{m.name}.json", wl)
        if primary is None:
            primary = wl

    if primary is not None:
        atomic_write_json(state_dir / "import-whitelist.json", primary)
    print(f"[OK] {state_dir/'import-whitelist.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
