"""jacoco_pom_guard.py — the ONE deterministic gate for editing a project's POM.

Closes audit finding F2 (2026-06-03): the architecture's single sanctioned write
into the analyzed project's source tree — adding `jacoco-maven-plugin` to a
`pom.xml` — was governed only by prose in skills/docs. No code verified the
precondition ("only add when the POM lacks JaCoCo AND the archetype requires it"),
so the decision rested on LLM judgement and burned tokens re-reasoning JaCoCo each
cycle. This tool moves that decision into deterministic code.

It reads the artifacts the deterministic pre-stage already produced —
`state/build-tool-contract.json` (jacoco.configured) and
`state/archetype-profile.json` (`modules[].archetype` + `implies.jacoco`) — and,
per module, resolves to exactly one of:

  none       JaCoCo is already in this POM            → never touch (no duplicates).
  forbidden  java-21 / implies.jacoco == "inherited"  → REFUSE (parent provides it);
                                                         measure via bootstrap CLI.
  add        java-8 ("manual") or non-BGBA ("absent") AND no JaCoCo in this POM
                                                       → the ONE permitted app edit.

Policy source of truth: docs/archetype-policy.md. The canonical `<plugin>` block is
NOT duplicated here — it is extracted from that document at apply time, so there is
a single definition (audit consistency rule).

Modes:
  --check   (default) report the per-module decision as JSON; exit 0. Never writes.
  --apply   perform the edit only for modules whose decision is `add` (and only if
            the POM still lacks JaCoCo, double-checked). A `forbidden` request exits
            3 without writing; `none` is a no-op (exit 0).

Hard guarantee: this is the only code path allowed to modify a project pom.xml. The
test patcher (test_patch_applier.py) still refuses everything outside test roots, so
no other write path can add the plugin.

Usage:
  python tools/python/jacoco_pom_guard.py --state ../.agent-state            # check all
  python tools/python/jacoco_pom_guard.py --state ../.agent-state --module . --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from lxml import etree

from common import _TimedRun, atomic_write_text, load_json

HERE = Path(__file__).resolve().parent
from pom_parser import parse_pom  # noqa: E402  (per-module jacoco detection, single source)

# Policy doc — the single source of truth for both the decision table and the
# canonical XML block (audit consistency rule: one definition, referenced not copied).
_POLICY_DOC = HERE.parents[1] / "docs" / "archetype-policy.md"

_POM_NS = "http://maven.apache.org/POM/4.0.0"
NS = {"m": _POM_NS}

# Exit codes (aligned with test_patch_applier perimeter conventions).
RC_OK = 0
RC_FAIL = 2          # unreadable/missing state, I/O error.
RC_FORBIDDEN = 3     # an --apply was requested for a module where editing is forbidden.


# ── Decision (pure, deterministic, unit-testable) ──────────────────────────────

def decide(archetype: str, implies_jacoco: str | None, jacoco_in_pom: bool) -> dict:
    """Resolve the POM-edit decision for ONE module.

    Precedence (matches docs/archetype-policy.md):
      1. Already present in this POM            → none (never duplicate).
      2. Inherited from parent (java-21)        → forbidden.
      3. Otherwise missing + archetype needs it → add (the one permitted app edit).
    """
    if jacoco_in_pom:
        return {"action": "none", "reason": "JACOCO_ALREADY_IN_POM"}
    if archetype == "java-21" or implies_jacoco == "inherited":
        return {"action": "forbidden", "reason": "JACOCO_INHERITED_FROM_PARENT"}
    return {"action": "add", "reason": "JACOCO_REQUIRED_FOR_DEPLOY_GATE"}


# ── Canonical block extraction (single source: the policy doc) ─────────────────

def load_canonical_block(doc_path: Path = _POLICY_DOC) -> str:
    """Extract the canonical `<plugin>…</plugin>` XML from docs/archetype-policy.md.

    Reads the first ```xml fenced block under the "Bloque JaCoCo canónico" heading
    so the plugin definition is never duplicated in code.
    """
    text = doc_path.read_text(encoding="utf-8")
    heading = text.find("## Bloque JaCoCo canónico")
    if heading == -1:
        raise ValueError(f"canonical JaCoCo heading not found in {doc_path}")
    m = re.search(r"```xml\s*\n(.*?)```", text[heading:], re.DOTALL)
    if not m:
        raise ValueError(f"canonical ```xml block not found under heading in {doc_path}")
    return m.group(1).strip()


def _plugin_element_in_pom_ns(block_xml: str):
    """Parse the canonical block INTO the POM default namespace.

    The block is namespace-less in the doc; declaring the POM namespace on its root
    makes every child inherit it, so it merges cleanly under <build><plugins> (no
    stray xmlns="" children).
    """
    ns_block = block_xml.replace("<plugin>", f'<plugin xmlns="{_POM_NS}">', 1)
    return etree.fromstring(ns_block.encode("utf-8"))


# ── POM editing (lxml — correct namespace/structure handling) ──────────────────

def add_jacoco_plugin(pom_path: Path, block_xml: str) -> bool:
    """Insert the canonical jacoco-maven-plugin into <build><plugins> of *pom_path*.

    Idempotent: returns False (no write) if the plugin is already present. Creates
    <build> and/or its direct <plugins> if absent. Note: lxml re-serializes the
    file, so whitespace/formatting is normalized — acceptable for this rare,
    once-per-module sanctioned edit.
    """
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.parse(str(pom_path), parser)
    root = tree.getroot()

    # Double-check: never duplicate an existing jacoco-maven-plugin (any depth).
    if root.xpath("//m:plugin[m:artifactId='jacoco-maven-plugin']", namespaces=NS):
        return False

    build = root.find("m:build", NS)
    if build is None:
        build = etree.SubElement(root, f"{{{_POM_NS}}}build")
    # Direct-child <plugins> only (NOT pluginManagement/plugins).
    plugins = build.find("m:plugins", NS)
    if plugins is None:
        plugins = etree.SubElement(build, f"{{{_POM_NS}}}plugins")

    plugins.append(_plugin_element_in_pom_ns(block_xml))

    etree.indent(tree, space="  ")
    xml_bytes = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    atomic_write_text(pom_path, xml_bytes.decode("utf-8"))
    return True


# ── Module enumeration (join archetype-profile + per-module pom parse) ─────────

def _selected(module_path: Path, selector: str | None) -> bool:
    """True if this module is in scope. None/""/"." → all modules (the run flow
    invokes the pipeline with `--module .`); otherwise match by name or full path."""
    if selector in (None, "", "."):
        return True
    return module_path.name == selector or str(module_path) == selector


def build_decisions(state_dir: Path, selector: str | None) -> list[dict]:
    """Per-module decisions, joining archetype-profile with a fresh per-module POM
    parse so jacoco presence is evaluated for THAT module (not the repo aggregate)."""
    profile = load_json(state_dir / "archetype-profile.json")
    decisions: list[dict] = []
    for mod in profile.get("modules", []):
        mod_path = Path(mod.get("path", ""))
        if not _selected(mod_path, selector):
            continue
        pom = mod_path / "pom.xml"
        jacoco_in_pom = False
        pom_exists = pom.exists()
        if pom_exists:
            try:
                jacoco_in_pom = bool(parse_pom(pom).get("jacoco_configured"))
            except Exception:
                jacoco_in_pom = False
        archetype = mod.get("archetype", "unknown")
        implies_jacoco = (mod.get("implies") or {}).get("jacoco")
        d = decide(archetype, implies_jacoco, jacoco_in_pom)
        d.update({
            "module": mod_path.name or str(mod_path),
            "pom": str(pom),
            "pomExists": pom_exists,
            "archetype": archetype,
            "jacocoInPom": jacoco_in_pom,
        })
        decisions.append(d)
    return decisions


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic gate for adding jacoco-maven-plugin to a project POM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--state", required=True, type=Path,
                    help="State directory holding archetype-profile.json + build-tool-contract.json.")
    ap.add_argument("--module", default=None,
                    help="Restrict to one module (name, path, or '.' for all). Default: all.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Report decisions only (default). Never writes.")
    mode.add_argument("--apply", action="store_true", help="Apply the edit for modules whose decision is 'add'.")
    args = ap.parse_args(argv)

    state_dir = args.state.resolve()
    if not (state_dir / "archetype-profile.json").exists():
        print(f"[FAIL] archetype-profile.json not found under {state_dir}", file=sys.stderr)
        return RC_FAIL

    try:
        decisions = build_decisions(state_dir, args.module)
    except Exception as exc:
        print(f"[FAIL] cannot read state: {exc}", file=sys.stderr)
        return RC_FAIL

    do_apply = args.apply
    rc = RC_OK
    applied: list[str] = []

    if do_apply:
        try:
            block = load_canonical_block()
        except Exception as exc:
            print(f"[FAIL] cannot load canonical JaCoCo block: {exc}", file=sys.stderr)
            return RC_FAIL

    for d in decisions:
        action = d["action"]
        if not do_apply:
            continue
        if action == "forbidden":
            print(f"[BLOCKED] {d['module']}: editing POM is forbidden ({d['reason']}); "
                  "JaCoCo is inherited — measure via bootstrap CLI.", file=sys.stderr)
            rc = RC_FORBIDDEN
            continue
        if action == "none":
            continue
        # action == "add"
        if not d["pomExists"]:
            print(f"[FAIL] {d['module']}: pom.xml not found at {d['pom']}", file=sys.stderr)
            rc = RC_FAIL
            continue
        try:
            wrote = add_jacoco_plugin(Path(d["pom"]), block)
        except Exception as exc:
            print(f"[FAIL] {d['module']}: could not edit POM: {exc}", file=sys.stderr)
            rc = RC_FAIL
            continue
        d["applied"] = wrote
        if wrote:
            applied.append(d["module"])
            print(f"[ADDED] {d['module']}: jacoco-maven-plugin inserted into {d['pom']}")
        else:
            print(f"[SKIP] {d['module']}: jacoco-maven-plugin already present (no change).")

    summary = {
        "mode": "apply" if do_apply else "check",
        "module": args.module,
        "decisions": decisions,
        "applied": applied,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    with _TimedRun("jacoco_pom_guard") as _tr:
        _rc = main()
        if _rc != RC_OK:
            _tr.set_status("FAIL")
        _tr.add("exitCode", _rc)
    sys.exit(_rc)
