"""jacoco_parser.py — parse jacoco.xml into coverage-targets.json or coverage-delta.json.

Usage:
  python jacoco_parser.py --xml target/site/jacoco/jacoco.xml --out state/coverage-targets.json --mode targets
  python jacoco_parser.py --before baseline.xml --after final.xml --out state/coverage-delta.json --mode delta
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lxml import etree

from common import atomic_write_json, validate


def _counters(elem) -> dict:
    out = {}
    for c in elem.findall("counter"):
        t = c.get("type", "")
        out[t] = {"covered": int(c.get("covered", 0)), "missed": int(c.get("missed", 0))}
    return out


def parse_report(xml_path: Path) -> dict:
    """Return per-class+method counters keyed by FQCN and method signature."""
    tree = etree.parse(str(xml_path))
    root = tree.getroot()
    perclass: dict[str, dict] = {}
    for pkg in root.findall("package"):
        pkg_name = pkg.get("name", "").replace("/", ".")
        for cls in pkg.findall("class"):
            fqcn = cls.get("name", "").replace("/", ".")
            methods = {}
            for m in cls.findall("method"):
                methods[f"{m.get('name')}{m.get('desc','')}"] = _counters(m)
            perclass[fqcn] = {
                "counters": _counters(cls),
                "methods": methods,
            }
    return perclass


def emit_targets(perclass: dict, mode: str) -> dict:
    targets = []
    tid = 0
    for fqcn, data in perclass.items():
        line_missed = data["counters"].get("LINE", {}).get("missed", 0)
        branch_missed = data["counters"].get("BRANCH", {}).get("missed", 0)
        for mname, mcnt in data["methods"].items():
            ml = mcnt.get("LINE", {}).get("missed", 0)
            mb = mcnt.get("BRANCH", {}).get("missed", 0)
            if mode == "branch-coverage" and mb == 0:
                continue
            if mode == "coverage" and ml == 0:
                continue
            tid += 1
            targets.append(
                {
                    "id": f"tgt:{tid:04d}",
                    "sut": fqcn,
                    "method": mname,
                    "missedLines": ml,
                    "missedBranches": mb,
                    "cxty": mcnt.get("COMPLEXITY", {}).get("missed", 0) + mcnt.get("COMPLEXITY", {}).get("covered", 0),
                    "risk": 0.0,
                    "score": float(ml * 2 + mb * 3),
                    "hasContract": False,
                    "hasFixtures": False,
                }
            )
    targets.sort(key=lambda t: t["score"], reverse=True)
    return {"schemaVersion": 1, "mode": mode, "targets": targets}


def emit_delta(before: dict, after: dict, cycle: int, mode: str) -> dict:
    def total(d, counter, field):
        s = 0
        for v in d.values():
            s += v["counters"].get(counter, {}).get(field, 0)
        return s
    totals = {}
    for counter, key in (("LINE", "lines"), ("BRANCH", "branches")):
        b = total(before, counter, "covered")
        a = total(after, counter, "covered")
        totals[key] = {"before": b, "after": a, "delta": a - b}
    perclass = []
    keys = set(before) | set(after)
    regressions = []
    for fqcn in sorted(keys):
        b = before.get(fqcn, {"counters": {}})
        a = after.get(fqcn, {"counters": {}})
        bl = b["counters"].get("LINE", {}).get("covered", 0)
        al = a["counters"].get("LINE", {}).get("covered", 0)
        bb = b["counters"].get("BRANCH", {}).get("covered", 0)
        ab = a["counters"].get("BRANCH", {}).get("covered", 0)
        if al < bl or ab < bb:
            regressions.append({"fqcn": fqcn, "linesDelta": al - bl, "branchesDelta": ab - bb})
        perclass.append(
            {
                "fqcn": fqcn,
                "lines": {"before": bl, "after": al, "delta": al - bl},
                "branches": {"before": bb, "after": ab, "delta": ab - bb},
                "attributedTests": [],
            }
        )
    return {
        "schemaVersion": 1,
        "cycle": cycle,
        "mode": mode,
        "totals": totals,
        "perClass": perclass,
        "regressions": regressions,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["targets", "delta"], required=True)
    ap.add_argument("--xml", help="single jacoco.xml (targets mode)")
    ap.add_argument("--before", help="baseline jacoco.xml (delta mode)")
    ap.add_argument("--after", help="final jacoco.xml (delta mode)")
    ap.add_argument("--cycle", type=int, default=1)
    ap.add_argument("--coverage-mode", default="coverage", choices=["coverage", "branch-coverage", "mutation-hardening"])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.mode == "targets":
        if not args.xml or not Path(args.xml).exists():
            print("[FAIL] --xml required and must exist", file=sys.stderr)
            return 2
        per = parse_report(Path(args.xml))
        out = emit_targets(per, args.coverage_mode)
        validate("coverage-targets", out)
    else:
        if not args.before or not args.after:
            print("[FAIL] --before and --after required", file=sys.stderr)
            return 2
        per_b = parse_report(Path(args.before))
        per_a = parse_report(Path(args.after))
        out = emit_delta(per_b, per_a, args.cycle, args.coverage_mode)
        validate("coverage-delta", out)
    atomic_write_json(Path(args.out), out)
    print(f"[OK] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
