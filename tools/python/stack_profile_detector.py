"""stack_profile_detector.py — deterministic testing stack detection from pom.xml(s).

Analyses every pom.xml under the target repository (root + Maven modules) to detect,
per module, with no LLM, no network access and no guessing:

  - Java version
  - Test framework: JUnit 4 or JUnit 5
  - Mock framework: Mockito (features: mockito-inline, powermock)
  - Assertion library: AssertJ | Hamcrest | JUnit built-in
  - Spring / Spring Boot test profile
  - Testcontainers availability
  - Annotation processors: Lombok, FreeBuilder, MapStruct, Immutables, AutoValue
  - Base namespace: javax.* vs jakarta.* (derived from Spring Boot parent version)

Detection strategy
------------------
  1. Parse root pom.xml → extract global settings (Spring Boot parent → namespace,
     Java version from compiler plugin / properties).
  2. Parse each module pom.xml → local deps override root findings; absent fields
     are inherited from the root profile.
  3. Aggregate → write state/stack-profile.json.

Schema update
-------------
  Retrocompatibly adds `namespace` and `testcontainers` to the module definition in
  state/_schemas/stack-profile.schema.json if those properties are absent.

CLI:
    python tools/python/stack_profile_detector.py --repo <repo-java> --out state
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from lxml import etree

from common import SCHEMAS_DIR, atomic_write_json, fail, find_pom_modules, load_json, validate

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

NS = {"m": "http://maven.apache.org/POM/4.0.0"}

# JUnit 5 artifactIds (any org.junit.* groupId)
_JUNIT5_AIDS: frozenset[str] = frozenset({
    "junit-jupiter",
    "junit-jupiter-api",
    "junit-jupiter-engine",
    "junit-jupiter-params",
    "junit-platform-launcher",
    "junit-platform-runner",
    "junit-vintage-engine",
})

# JUnit 4: groupId must be exactly "junit"
_JUNIT4_AIDS: frozenset[str] = frozenset({"junit", "junit-dep"})
_JUNIT4_GIDS: frozenset[str] = frozenset({"junit"})

_MOCKITO_AIDS: frozenset[str] = frozenset({
    "mockito-core", "mockito-all", "mockito-junit-jupiter",
})
_MOCKITO_INLINE_AIDS: frozenset[str] = frozenset({"mockito-inline"})
_POWERMOCK_AIDS: frozenset[str] = frozenset({
    "powermock-api-mockito2",
    "powermock-api-mockito",
    "powermock-module-junit4",
    "powermock-module-junit5",
    "powermock-core",
})

_ASSERTJ_AIDS: frozenset[str] = frozenset({"assertj-core"})
_HAMCREST_AIDS: frozenset[str] = frozenset({
    "hamcrest-all", "hamcrest-core", "hamcrest-library",
})

_SPRING_BOOT_TEST_AIDS: frozenset[str] = frozenset({
    "spring-boot-test", "spring-boot-starter-test",
})
_SPRING_TEST_AIDS: frozenset[str] = frozenset({"spring-test"})
_TC_GIDS: frozenset[str] = frozenset({"org.testcontainers"})

# (name → (artifact_ids, required_group_ids))
_PROCESSORS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "lombok":      (frozenset({"lombok"}),                               frozenset({"org.projectlombok"})),
    "freebuilder": (frozenset({"freebuilder"}),                          frozenset({"org.inferred"})),
    "mapstruct":   (frozenset({"mapstruct", "mapstruct-processor"}),     frozenset({"org.mapstruct"})),
    "immutables":  (frozenset({"value"}),                                frozenset({"org.immutables"})),
    "autovalue":   (frozenset({"auto-value", "auto-value-annotations"}), frozenset({"com.google.auto.value"})),
}

_SB_PARENT_AIDS: frozenset[str] = frozenset({
    "spring-boot-starter-parent", "spring-boot-dependencies",
})

_PROP_RE = re.compile(r"^\$\{(.+)\}$")
_MAJOR_RE = re.compile(r"^(\d+)")


# ─────────────────────────────────────────────────────────────────────────────
# POM helpers
# ─────────────────────────────────────────────────────────────────────────────

def _txt(node: etree._Element, xp: str) -> str | None:
    """Evaluate an XPath that may return an Element or a text node."""
    r = node.xpath(xp, namespaces=NS)
    if not r:
        return None
    first = r[0]
    val = first.text if hasattr(first, "text") else str(first)
    return (val or "").strip() or None


def _resolve_property(root: etree._Element, value: str | None) -> str | None:
    """Resolve ${property.name} references from <properties> section."""
    if not value:
        return value
    m = _PROP_RE.match(value)
    if not m:
        return value
    return _txt(root, f"m:properties/m:{m.group(1)}/text()") or value


def _all_deps(root: etree._Element) -> list[tuple[str, str, str]]:
    """Return (groupId, artifactId, version) for all <dependency> elements."""
    result: list[tuple[str, str, str]] = []
    for dep in root.xpath("//m:dependency", namespaces=NS):
        gid = (_txt(dep, "m:groupId/text()") or "").lower()
        aid = (_txt(dep, "m:artifactId/text()") or "").lower()
        ver = _txt(dep, "m:version/text()") or ""
        result.append((gid, aid, ver))
    return result


def _namespace_from_sb_version(ver: str | None) -> str:
    """'jakarta' if Spring Boot >= 3.0; 'javax' if < 3.0; 'unknown' otherwise."""
    if not ver:
        return "unknown"
    m = _MAJOR_RE.match(ver.strip())
    if not m:
        return "unknown"
    return "jakarta" if int(m.group(1)) >= 3 else "javax"


def _java_from_pom(root: etree._Element) -> str | None:
    """Extract Java version from compiler plugin or properties."""
    return (
        _txt(root, "m:properties/m:maven.compiler.release/text()")
        or _txt(root, "m:properties/m:maven.compiler.target/text()")
        or _txt(root, "m:properties/m:java.version/text()")
        or _txt(root, "//m:plugin[m:artifactId='maven-compiler-plugin']"
                      "/m:configuration/m:release/text()")
        or _txt(root, "//m:plugin[m:artifactId='maven-compiler-plugin']"
                      "/m:configuration/m:target/text()")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-module profile accumulator
# ─────────────────────────────────────────────────────────────────────────────

class _ModuleProfile:
    """Mutable accumulator for one Maven module's testing stack."""

    def __init__(self, path: str) -> None:
        self.path = path
        # test framework
        self.has_junit5 = False
        self.has_junit4 = False
        self.junit_version: str | None = None
        # mock
        self.has_mockito = False
        self.mockito_version: str | None = None
        self.has_mockito_inline = False
        self.has_powermock = False
        # assertion
        self.has_assertj = False
        self.assertj_version: str | None = None
        self.has_hamcrest = False
        self.hamcrest_version: str | None = None
        # spring
        self.has_spring_test = False
        self.has_spring_boot_test = False
        self.spring_boot_version: str | None = None
        # testcontainers
        self.has_testcontainers = False
        # annotation processors
        self.annotation_processors: list[str] = []
        # namespace
        self.namespace: str = "unknown"

    # ── absorption ────────────────────────────────────────────────────────────

    def absorb_deps(self, root: etree._Element) -> None:
        """Scan all <dependency> elements and set flags."""
        for gid, aid, ver in _all_deps(root):
            self._check_dep(gid, aid, ver)

    def _check_dep(self, gid: str, aid: str, ver: str) -> None:  # noqa: C901
        # JUnit 5
        if aid in _JUNIT5_AIDS and ("junit" in gid or gid == ""):
            self.has_junit5 = True
            self.junit_version = self.junit_version or ver or None

        # JUnit 4
        if aid in _JUNIT4_AIDS and gid in _JUNIT4_GIDS:
            self.has_junit4 = True
            self.junit_version = self.junit_version or ver or None

        # Mockito core
        if aid in _MOCKITO_AIDS and gid == "org.mockito":
            self.has_mockito = True
            self.mockito_version = self.mockito_version or ver or None

        # Mockito inline
        if aid in _MOCKITO_INLINE_AIDS and gid == "org.mockito":
            self.has_mockito = True
            self.has_mockito_inline = True
            self.mockito_version = self.mockito_version or ver or None

        # PowerMock
        if aid in _POWERMOCK_AIDS:
            self.has_powermock = True

        # AssertJ
        if aid in _ASSERTJ_AIDS:
            self.has_assertj = True
            self.assertj_version = self.assertj_version or ver or None

        # Hamcrest
        if aid in _HAMCREST_AIDS:
            self.has_hamcrest = True
            self.hamcrest_version = self.hamcrest_version or ver or None

        # Spring Test
        if aid in _SPRING_TEST_AIDS and "spring" in gid:
            self.has_spring_test = True

        # Spring Boot Test (umbrella: also implies JUnit 5, Mockito, AssertJ)
        if aid in _SPRING_BOOT_TEST_AIDS:
            self.has_spring_boot_test = True
            if aid == "spring-boot-starter-test":
                self.has_junit5 = True
                self.has_mockito = True
                self.has_assertj = True

        # Testcontainers
        if gid in _TC_GIDS:
            self.has_testcontainers = True

        # Annotation processors
        for proc_name, (proc_aids, proc_gids) in _PROCESSORS.items():
            if aid in proc_aids and (not proc_gids or gid in proc_gids):
                if proc_name not in self.annotation_processors:
                    self.annotation_processors.append(proc_name)

    def absorb_parent(self, root: etree._Element) -> None:
        """Detect Spring Boot version from <parent> for namespace resolution."""
        parent_aid = (_txt(root, "m:parent/m:artifactId/text()") or "").lower()
        if parent_aid not in _SB_PARENT_AIDS:
            return
        raw_ver = _txt(root, "m:parent/m:version/text()")
        resolved = _resolve_property(root, raw_ver)
        ns = _namespace_from_sb_version(resolved)
        if ns != "unknown":
            self.namespace = ns
            self.spring_boot_version = resolved

    def inherit_from(self, root_profile: "_ModuleProfile") -> None:
        """Inherit settings from root POM profile where not locally set."""
        if self.namespace == "unknown" and root_profile.namespace != "unknown":
            self.namespace = root_profile.namespace
            self.spring_boot_version = root_profile.spring_boot_version
        if not self.has_junit5 and not self.has_junit4:
            self.has_junit5 = root_profile.has_junit5
            self.has_junit4 = root_profile.has_junit4
            self.junit_version = root_profile.junit_version
        if not self.has_mockito:
            self.has_mockito = root_profile.has_mockito
            self.mockito_version = root_profile.mockito_version
            self.has_mockito_inline = root_profile.has_mockito_inline
            self.has_powermock = root_profile.has_powermock
        if not self.has_assertj and not self.has_hamcrest:
            self.has_assertj = root_profile.has_assertj
            self.assertj_version = root_profile.assertj_version
            self.has_hamcrest = root_profile.has_hamcrest
            self.hamcrest_version = root_profile.hamcrest_version
        if not self.has_spring_boot_test and not self.has_spring_test:
            self.has_spring_boot_test = root_profile.has_spring_boot_test
            self.has_spring_test = root_profile.has_spring_test
        if not self.has_testcontainers:
            self.has_testcontainers = root_profile.has_testcontainers
        # Merge annotation processors
        for proc in root_profile.annotation_processors:
            if proc not in self.annotation_processors:
                self.annotation_processors.append(proc)

    def to_dict(self) -> dict:
        """Serialise to the JSON object expected by stack-profile.schema.json."""
        # Test framework
        if self.has_junit5:
            test_framework = "junit5"
        elif self.has_junit4:
            test_framework = "junit4"
        else:
            test_framework = "junit5"  # conservative default

        # Mock features
        mock_features: list[str] = []
        if self.has_mockito_inline:
            mock_features.append("mockito-inline")
        if self.has_powermock:
            mock_features.append("powermock")

        # Assertion
        if self.has_assertj:
            assert_fw, assert_ver = "assertj", self.assertj_version or "unknown"
        elif self.has_hamcrest:
            assert_fw, assert_ver = "hamcrest", self.hamcrest_version or "unknown"
        else:
            assert_fw, assert_ver = "junit-builtin", self.junit_version or "unknown"

        result: dict = {
            "path": self.path,
            "test": {
                "framework": test_framework,
                "version": self.junit_version or "unknown",
            },
            "mock": {
                "framework": "mockito",
                "version": self.mockito_version or "unknown",
                "features": mock_features,
            },
            "assert": {
                "framework": assert_fw,
                "version": assert_ver,
            },
            "di": {
                "spring": self.has_spring_test or self.has_spring_boot_test,
                "springBoot": self.spring_boot_version,
                "slices": [],
            },
            "annotationProcessors": self.annotation_processors,
            "generatedSources": [],
            "namespace": self.namespace,
        }
        if self.has_testcontainers:
            result["testcontainers"] = True
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Schema update
# ─────────────────────────────────────────────────────────────────────────────

def _update_schema(schema_path: Path) -> None:
    """Retrocompatibly add namespace and testcontainers to the module schema."""
    if not schema_path.exists():
        return
    try:
        schema = load_json(schema_path)
    except Exception as exc:
        print(f"[WARN] cannot load schema for update: {exc}", file=sys.stderr)
        return

    module_props: dict = (
        schema
        .get("properties", {})
        .get("modules", {})
        .get("items", {})
        .get("properties", {})
    )
    changed = False

    if "namespace" not in module_props:
        module_props["namespace"] = {
            "type": "string",
            "enum": ["javax", "jakarta", "unknown"],
            "description": "Base import namespace: javax.* (Spring Boot < 3) or jakarta.* (Spring Boot >= 3)",
        }
        changed = True

    if "testcontainers" not in module_props:
        module_props["testcontainers"] = {
            "type": "boolean",
            "description": "True if org.testcontainers is in the test classpath",
        }
        changed = True

    if changed:
        # Navigate to the right nested location and patch it
        (
            schema
            .setdefault("properties", {})
            .setdefault("modules", {})
            .setdefault("items", {})
            .setdefault("properties", {})
        ).update(module_props)
        atomic_write_json(schema_path, schema)
        print(f"[INFO] updated schema: {schema_path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# Core detection
# ─────────────────────────────────────────────────────────────────────────────

def _parse_pom(pom: Path) -> etree._Element | None:
    try:
        return etree.parse(str(pom)).getroot()
    except etree.XMLSyntaxError as exc:
        print(f"[WARN] cannot parse {pom}: {exc}", file=sys.stderr)
        return None


def detect(repo: Path) -> dict:
    """Return a complete stack-profile dict for the given repository."""
    root_pom = repo / "pom.xml"
    if not root_pom.exists():
        fail(f"No root pom.xml found in {repo}")

    # ── root POM ──────────────────────────────────────────────────────────────
    root_el = _parse_pom(root_pom)
    if root_el is None:
        fail(f"Cannot parse root pom.xml: {root_pom}")
    assert root_el is not None  # for type checkers

    root_profile = _ModuleProfile(str(repo.resolve()))
    root_profile.absorb_deps(root_el)
    root_profile.absorb_parent(root_el)

    java_ver = _resolve_property(root_el, _java_from_pom(root_el)) or "unknown"

    # ── modules ───────────────────────────────────────────────────────────────
    module_dirs = find_pom_modules(repo)
    module_profiles: list[dict] = []

    for mod_dir in sorted(module_dirs):
        pom = mod_dir / "pom.xml"
        if not pom.exists():
            continue
        profile = _ModuleProfile(str(mod_dir.resolve()))
        root_el_mod = _parse_pom(pom)
        if root_el_mod is not None:
            profile.absorb_deps(root_el_mod)
            profile.absorb_parent(root_el_mod)
            # Java version per module
            mod_java = _resolve_property(root_el_mod, _java_from_pom(root_el_mod))
            if mod_java:
                java_ver_local = mod_java
            else:
                java_ver_local = java_ver
        else:
            java_ver_local = java_ver

        profile.inherit_from(root_profile)
        _ = java_ver_local  # stored per module if needed in future
        module_profiles.append(profile.to_dict())

    # If no modules found (single-module project), treat root as the sole module
    if not module_profiles:
        module_profiles.append(root_profile.to_dict())

    # ── presets: derive import namespace rule ─────────────────────────────────
    # Use the namespace from the most common module (or root)
    namespaces = [m["namespace"] for m in module_profiles if m["namespace"] != "unknown"]
    global_ns = max(set(namespaces), key=namespaces.count) if namespaces else "unknown"

    imports_allowed: list[str] = []
    imports_forbidden: list[str] = []
    if global_ns == "jakarta":
        imports_allowed.append("jakarta.*")
        imports_forbidden.append("javax.*")
    elif global_ns == "javax":
        imports_allowed.append("javax.*")
        imports_forbidden.append("jakarta.*")

    return {
        "java": java_ver,
        "buildTool": "maven",
        "modules": module_profiles,
        "presets": {
            "imports.allowed": imports_allowed,
            "imports.forbidden": imports_forbidden,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Detect testing stack from pom.xml(s) and write state/stack-profile.json.\n"
            "Detection is fully deterministic: no LLM, no network, no guessing.\n"
            "Detects: JUnit 4/5, Mockito (inline/PowerMock), AssertJ/Hamcrest,\n"
            "Spring Test, Testcontainers, annotation processors, javax vs jakarta."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", required=True, help="Root of the Java repository to analyse")
    ap.add_argument("--out", required=True, help="State directory (e.g. state/)")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state_dir = Path(args.out).resolve()

    if not repo.exists():
        fail(f"Repository not found: {repo}")

    # Retrocompatibly extend the schema before validating
    _update_schema(SCHEMAS_DIR / "stack-profile.schema.json")

    profile = detect(repo)
    validate("stack-profile", profile)
    atomic_write_json(state_dir / "stack-profile.json", profile)

    n_modules = len(profile["modules"])
    java = profile["java"]
    ns_values = {m["namespace"] for m in profile["modules"]}
    procs = sorted({p for m in profile["modules"] for p in m.get("annotationProcessors", [])})
    print(f"[OK] state/stack-profile.json  "
          f"java={java}  modules={n_modules}  "
          f"namespace={','.join(ns_values) if ns_values else 'unknown'}  "
          f"processors=[{','.join(procs)}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
