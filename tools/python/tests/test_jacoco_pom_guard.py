"""test_jacoco_pom_guard.py — the deterministic POM-edit gate (audit F2).

Covers the decision table (docs/archetype-policy.md) and the idempotent,
namespace-correct insertion of the canonical jacoco-maven-plugin block:

  decide():
    - already-present  → none
    - java-21 inherited → forbidden
    - java-8 manual, missing → add
    - non-BGBA absent, missing → add
  add_jacoco_plugin():
    - inserts the canonical block under <build><plugins> in the POM namespace
    - is idempotent (no duplicate plugin / no second write)
    - creates <build>/<plugins> when absent
  build_decisions() + apply path:
    - forbidden module is NOT edited and apply returns RC_FORBIDDEN
    - add module IS edited; the POM ends up with exactly one jacoco plugin

Run: `python tools/python/tests/test_jacoco_pom_guard.py`  (also collected by pytest)
"""
from __future__ import annotations

import sys
from pathlib import Path

from lxml import etree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import jacoco_pom_guard as guard  # noqa: E402
from common import atomic_write_json, atomic_write_text  # noqa: E402

NS = {"m": "http://maven.apache.org/POM/4.0.0"}

_POM_NO_BUILD = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>svc</artifactId>
  <version>1.0.0</version>
</project>
"""

_POM_WITH_BUILD_PLUGINS = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>svc</artifactId>
  <version>1.0.0</version>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
      </plugin>
    </plugins>
  </build>
</project>
"""

_POM_WITH_JACOCO = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>svc</artifactId>
  <version>1.0.0</version>
  <build>
    <plugins>
      <plugin>
        <groupId>org.jacoco</groupId>
        <artifactId>jacoco-maven-plugin</artifactId>
        <version>0.8.13</version>
      </plugin>
    </plugins>
  </build>
</project>
"""


def _jacoco_count(pom_path: Path) -> int:
    root = etree.parse(str(pom_path)).getroot()
    return len(root.xpath("//m:plugin[m:artifactId='jacoco-maven-plugin']", namespaces=NS))


# ── decide() ──────────────────────────────────────────────────────────────────

def case_decide_already_present() -> None:
    d = guard.decide("java-8", "manual", jacoco_in_pom=True)
    assert d["action"] == "none", d


def case_decide_inherited_forbidden() -> None:
    d = guard.decide("java-21", "inherited", jacoco_in_pom=False)
    assert d["action"] == "forbidden", d
    # also forbidden if archetype mislabeled but implies says inherited
    d2 = guard.decide("unknown", "inherited", jacoco_in_pom=False)
    assert d2["action"] == "forbidden", d2


def case_decide_java8_add() -> None:
    d = guard.decide("java-8", "manual", jacoco_in_pom=False)
    assert d["action"] == "add", d


def case_decide_nonbgba_absent_add() -> None:
    d = guard.decide("unknown", "absent", jacoco_in_pom=False)
    assert d["action"] == "add", d


# ── canonical block extraction ─────────────────────────────────────────────────

def case_canonical_block_loads() -> None:
    block = guard.load_canonical_block()
    assert "jacoco-maven-plugin" in block
    assert "0.8.13" in block
    assert "0.80" in block  # the deploy-gate branch check
    # must parse into the POM namespace without error
    el = guard._plugin_element_in_pom_ns(block)
    assert el.tag.endswith("}plugin")


# ── add_jacoco_plugin(): insertion + idempotency ───────────────────────────────

def case_add_into_existing_plugins_then_idempotent(tmp: Path) -> None:
    pom = tmp / "pom.xml"
    atomic_write_text(pom, _POM_WITH_BUILD_PLUGINS)
    block = guard.load_canonical_block()

    wrote = guard.add_jacoco_plugin(pom, block)
    assert wrote is True
    assert _jacoco_count(pom) == 1
    # surefire still there → we appended, not replaced
    root = etree.parse(str(pom)).getroot()
    assert root.xpath("//m:plugin[m:artifactId='maven-surefire-plugin']", namespaces=NS)
    # no stray empty namespace on the inserted element
    assert 'xmlns=""' not in pom.read_text(encoding="utf-8")

    # second call is a no-op
    wrote2 = guard.add_jacoco_plugin(pom, block)
    assert wrote2 is False
    assert _jacoco_count(pom) == 1


def case_add_creates_build_and_plugins(tmp: Path) -> None:
    pom = tmp / "pom.xml"
    atomic_write_text(pom, _POM_NO_BUILD)
    wrote = guard.add_jacoco_plugin(pom, guard.load_canonical_block())
    assert wrote is True
    root = etree.parse(str(pom)).getroot()
    assert root.find("m:build/m:plugins", NS) is not None
    assert _jacoco_count(pom) == 1


# ── end-to-end via build_decisions + CLI main ──────────────────────────────────

def _state_with_module(state: Path, mod_dir: Path, archetype: str, implies_jacoco: str) -> None:
    atomic_write_json(state / "archetype-profile.json", {
        "schemaVersion": 1,
        "modules": [{
            "path": str(mod_dir.resolve()),
            "archetype": archetype,
            "implies": {"jacoco": implies_jacoco, "namespace": "javax", "junit": "5"},
        }],
    })


def case_apply_adds_for_java8(tmp: Path) -> None:
    state = tmp / "state"
    mod = tmp / "svc"
    state.mkdir(parents=True)
    mod.mkdir(parents=True)
    atomic_write_text(mod / "pom.xml", _POM_WITH_BUILD_PLUGINS)
    _state_with_module(state, mod, "java-8", "manual")

    rc = guard.main(["--state", str(state), "--apply"])
    assert rc == guard.RC_OK, rc
    assert _jacoco_count(mod / "pom.xml") == 1


def case_apply_refuses_java21(tmp: Path) -> None:
    state = tmp / "state"
    mod = tmp / "svc"
    state.mkdir(parents=True)
    mod.mkdir(parents=True)
    atomic_write_text(mod / "pom.xml", _POM_WITH_BUILD_PLUGINS)
    _state_with_module(state, mod, "java-21", "inherited")

    rc = guard.main(["--state", str(state), "--apply"])
    assert rc == guard.RC_FORBIDDEN, rc
    assert _jacoco_count(mod / "pom.xml") == 0  # untouched


def case_check_never_writes(tmp: Path) -> None:
    state = tmp / "state"
    mod = tmp / "svc"
    state.mkdir(parents=True)
    mod.mkdir(parents=True)
    atomic_write_text(mod / "pom.xml", _POM_WITH_BUILD_PLUGINS)
    _state_with_module(state, mod, "java-8", "manual")

    rc = guard.main(["--state", str(state)])  # default = check
    assert rc == guard.RC_OK, rc
    assert _jacoco_count(mod / "pom.xml") == 0  # check must not edit


def case_apply_noop_when_already_present(tmp: Path) -> None:
    state = tmp / "state"
    mod = tmp / "svc"
    state.mkdir(parents=True)
    mod.mkdir(parents=True)
    atomic_write_text(mod / "pom.xml", _POM_WITH_JACOCO)
    _state_with_module(state, mod, "java-8", "manual")

    rc = guard.main(["--state", str(state), "--apply"])
    assert rc == guard.RC_OK, rc
    assert _jacoco_count(mod / "pom.xml") == 1  # still exactly one


# ── pytest entry points (collected) + standalone runner ────────────────────────

def test_decide_already_present():
    case_decide_already_present()


def test_decide_inherited_forbidden():
    case_decide_inherited_forbidden()


def test_decide_java8_add():
    case_decide_java8_add()


def test_decide_nonbgba_absent_add():
    case_decide_nonbgba_absent_add()


def test_canonical_block_loads():
    case_canonical_block_loads()


def test_add_into_existing_plugins_then_idempotent(tmp_path):
    case_add_into_existing_plugins_then_idempotent(tmp_path)


def test_add_creates_build_and_plugins(tmp_path):
    case_add_creates_build_and_plugins(tmp_path)


def test_apply_adds_for_java8(tmp_path):
    case_apply_adds_for_java8(tmp_path)


def test_apply_refuses_java21(tmp_path):
    case_apply_refuses_java21(tmp_path)


def test_check_never_writes(tmp_path):
    case_check_never_writes(tmp_path)


def test_apply_noop_when_already_present(tmp_path):
    case_apply_noop_when_already_present(tmp_path)


def main() -> int:
    import tempfile

    simple = [
        ("decide-already-present", case_decide_already_present),
        ("decide-inherited-forbidden", case_decide_inherited_forbidden),
        ("decide-java8-add", case_decide_java8_add),
        ("decide-nonbgba-absent-add", case_decide_nonbgba_absent_add),
        ("canonical-block-loads", case_canonical_block_loads),
    ]
    with_tmp = [
        ("add-into-existing-then-idempotent", case_add_into_existing_plugins_then_idempotent),
        ("add-creates-build-and-plugins", case_add_creates_build_and_plugins),
        ("apply-adds-for-java8", case_apply_adds_for_java8),
        ("apply-refuses-java21", case_apply_refuses_java21),
        ("check-never-writes", case_check_never_writes),
        ("apply-noop-when-already-present", case_apply_noop_when_already_present),
    ]
    failed = 0
    for name, fn in simple:
        try:
            fn()
            print(f"OK   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    for name, fn in with_tmp:
        try:
            with tempfile.TemporaryDirectory() as td:
                fn(Path(td))
            print(f"OK   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    if failed:
        print(f"\n{failed} test(s) failed")
        return 1
    print("\nAll jacoco_pom_guard cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
