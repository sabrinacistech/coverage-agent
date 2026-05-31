"""fixture_catalog_builder.py — build state/fixture-catalog.json deterministically.

Reads (produced by prior pipeline steps):
  - state/symbol-contracts/<fqcn>.json  — per-class contracts with instantiation strategy,
                                          constructors, builders, and methods
  - state/index/methods.json            — FQCN → [{name, params, returnType, ...}]
  - state/stack-profile.json            — whether Mockito is available (for mock strategy)
  - state/classification-index.json     — SUT type (service, controller, …) → variants

Strategy selection (strict, evidence-only, priority order)
----------------------------------------------------------
  1. contract.builders[] non-empty AND kind != interface/abstract
       → strategy: "builder"
       → builderEvidence: builders[0].entry   (must contain ".Builder()", never "_Builder")
  2. contract.constructors[] has at least one public constructor
       → strategy: "constructor"
       → constructorEvidence: best_ctor.evidenceId
  3. Static factory method (static method whose returnType == FQCN)
       → strategy: "factory"
       → factoryEvidence: method.evidenceId
  4. kind == interface OR kind == abstract AND Mockito available
       → strategy: "mock"
  5. All other cases (private-only ctors, Mockito unavailable, etc.)
       → strategy: "mock", degraded: true, cycleSafe: false
       (schema enum forbids "none"; downstream must treat as fragile)

Strict prohibitions
-------------------
  - NEVER emit builderEvidence containing "_Builder" (direct generated class).
  - NEVER invent constructor params that are not in constructors[].params.
  - NEVER suggest a factory method that does not appear in the contract's methods[].

Default values for primitive/well-known types
---------------------------------------------
  String → ""          int/Integer → 0        long/Long → 0L
  boolean/Boolean → false   double/Double → 0.0   float/Float → 0.0f
  BigDecimal → BigDecimal.ZERO
  LocalDate → LocalDate.of(2024, 1, 1)
  LocalDateTime → LocalDateTime.of(2024, 1, 1, 0, 0)
  Instant → Instant.EPOCH
  UUID → UUID.fromString("00000000-0000-0000-0000-000000000000")
  List/Collection → Collections.emptyList()
  Map → Collections.emptyMap()
  Set → Collections.emptySet()
  Unknown reference types → null

CLI:
    python tools/python/fixture_catalog_builder.py --out state
    python tools/python/fixture_catalog_builder.py --out state --contracts state/symbol-contracts
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import SCHEMAS_DIR, atomic_write_json, load_json, validate

# ─────────────────────────────────────────────────────────────────────────────
# Default values for Java types
# ─────────────────────────────────────────────────────────────────────────────

# Maps the simple class name (or primitive name) to a valid Java literal / expression
_TYPE_DEFAULTS: dict[str, str] = {
    # Primitives
    "String":        '""',
    "int":           "0",
    "Integer":       "0",
    "long":          "0L",
    "Long":          "0L",
    "short":         "0",
    "Short":         "0",
    "byte":          "0",
    "Byte":          "0",
    "char":          "'\\0'",
    "Character":     "'\\0'",
    "boolean":       "false",
    "Boolean":       "false",
    "double":        "0.0",
    "Double":        "0.0",
    "float":         "0.0f",
    "Float":         "0.0f",
    # Numeric/Financial
    "BigDecimal":    "BigDecimal.ZERO",
    "BigInteger":    "BigInteger.ZERO",
    "Number":        "0",
    # Date/Time
    "LocalDate":     "LocalDate.of(2024, 1, 1)",
    "LocalDateTime": "LocalDateTime.of(2024, 1, 1, 0, 0)",
    "LocalTime":     "LocalTime.of(0, 0)",
    "ZonedDateTime": "ZonedDateTime.of(2024, 1, 1, 0, 0, 0, 0, ZoneOffset.UTC)",
    "OffsetDateTime":"OffsetDateTime.of(2024, 1, 1, 0, 0, 0, 0, ZoneOffset.UTC)",
    "Instant":       "Instant.EPOCH",
    "Date":          "new Date(0L)",
    "Calendar":      "Calendar.getInstance()",
    "Duration":      "Duration.ZERO",
    "Period":        "Period.ZERO",
    # Identity
    "UUID":          'UUID.fromString("00000000-0000-0000-0000-000000000000")',
    # Collections (java.util)
    "List":          "Collections.emptyList()",
    "ArrayList":     "new ArrayList<>()",
    "LinkedList":    "new LinkedList<>()",
    "Set":           "Collections.emptySet()",
    "HashSet":       "new HashSet<>()",
    "LinkedHashSet": "new LinkedHashSet<>()",
    "SortedSet":     "Collections.emptySortedSet()",
    "TreeSet":       "new TreeSet<>()",
    "Map":           "Collections.emptyMap()",
    "HashMap":       "new HashMap<>()",
    "LinkedHashMap": "new LinkedHashMap<>()",
    "SortedMap":     "Collections.emptySortedMap()",
    "TreeMap":       "new TreeMap<>()",
    "Collection":    "Collections.emptyList()",
    "Iterable":      "Collections.emptyList()",
    "Iterator":      "Collections.emptyIterator()",
    "Optional":      "Optional.empty()",
    # Reactive
    "Mono":          "Mono.empty()",
    "Flux":          "Flux.empty()",
    "Publisher":     "Mono.empty()",
    # Void / Object
    "void":          "/* void */",
    "Object":        "new Object()",
    # Array shorthand
    "byte[]":        "new byte[0]",
    "int[]":         "new int[0]",
    "String[]":      "new String[0]",
}

# Test variant names keyed on SUT classification type
_VARIANTS: dict[str, list[str]] = {
    "controller":       ["valid-request", "invalid-request", "not-found", "unauthorized"],
    "service":          ["happy-path", "not-found", "validation-error", "dependency-failure"],
    "repository":       ["find-found", "find-not-found", "save-success", "delete-success"],
    "component":        ["happy-path", "error-case"],
    "mapper":           ["valid-mapping", "null-input"],
    "data-carrier":     ["default-values", "full-values"],
    "enum":             ["all-values"],
    "non-instantiable": [],
    "generated/excluded": [],
    "configuration":    [],
}

_DEFAULT_VARIANTS: list[str] = ["happy-path"]

# Test suffixes — exclude from fixture catalog
_TEST_SUFFIXES: tuple[str, ...] = ("Test", "Tests", "IT", "Spec", "Specs", "TestCase")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_load(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return load_json(path)
    except Exception as exc:
        print(f"[WARN] cannot load {path}: {exc}", file=sys.stderr)
        return default


def _simple(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[-1]


def _is_test_class(fqcn: str) -> bool:
    return any(_simple(fqcn).endswith(s) for s in _TEST_SUFFIXES)


def _mockito_available(stack_profile: dict) -> bool:
    """True if any module declares Mockito as mock framework."""
    for module in stack_profile.get("modules", []):
        if module.get("mock", {}).get("framework") == "mockito":
            return True
    return False


def _default_for_type(java_type: str) -> str:
    """Return a deterministic default literal for a Java type."""
    # Strip generics: 'List<String>' → 'List'
    base = java_type.split("<")[0].strip()
    # Simple name: 'java.util.List' → 'List'
    simple = base.rsplit(".", 1)[-1]
    # Array shorthand check
    if java_type.endswith("[]"):
        arr_key = simple + "[]"
        if arr_key in _TYPE_DEFAULTS:
            return _TYPE_DEFAULTS[arr_key]
        return f"new {simple}[0]"
    return _TYPE_DEFAULTS.get(simple, "null")


def _params_defaults(params: list[dict]) -> dict[str, str]:
    """Build {paramName: defaultValue} for a list of constructor/method params."""
    values: dict[str, str] = {}
    for idx, p in enumerate(params):
        name = p.get("name") or f"arg{idx}"
        values[name] = _default_for_type(p.get("type", "Object"))
    return values


def _best_public_constructor(constructors: list[dict]) -> dict | None:
    """Smallest public constructor (fewest params → easiest to construct in tests)."""
    public = [c for c in constructors if c.get("visibility") == "public"]
    if not public:
        return None
    return min(public, key=lambda c: len(c.get("params", [])))


def _valid_builder(builders: list[dict]) -> dict | None:
    """First builder whose entry does NOT contain '_Builder' (direct generated class)."""
    for b in builders:
        entry: str = b.get("entry", "")
        if "_Builder" in entry and ".Builder" not in entry:
            # Looks like a raw generated class — skip (architecture rule)
            continue
        if ".Builder" in entry or entry.endswith("Builder()"):
            return b
    return None


def _static_factory_method(fqcn: str, methods: list[dict]) -> dict | None:
    """Find a static method whose return type is the same as the class."""
    simple = _simple(fqcn)
    for m in methods:
        mods = [mod.lower() for mod in m.get("modifiers", [])]
        if "static" not in mods:
            continue
        ret = m.get("returnType", "")
        if ret == fqcn or _simple(ret) == simple:
            return m
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Fixture builder for one contract
# ─────────────────────────────────────────────────────────────────────────────

def _build_fixture(
    contract: dict,
    sut_type: str,
    mockito_ok: bool,
) -> dict | None:
    fqcn: str = contract["fqcn"]
    kind: str = contract.get("kind", "class").lower()
    constructors: list[dict] = contract.get("constructors", [])
    builders: list[dict] = contract.get("builders", [])
    methods: list[dict] = contract.get("methods", [])

    strategy: str
    evidence_key: str
    evidence_val: str
    values: dict[str, str] = {}

    # ── Strategy 1: builder ────────────────────────────────────────────────────
    valid_bld = _valid_builder(builders) if kind not in ("interface", "abstract") else None
    if valid_bld:
        entry: str = valid_bld.get("entry", "")
        build: str = valid_bld.get("build", "build()")
        strategy = "builder"
        evidence_key = "builderEvidence"
        evidence_val = f"{entry} … {build}"
        # Default values for all required setters
        for s in valid_bld.get("setters", []):
            if s.get("required", False):
                values[s["name"]] = _default_for_type(s.get("type", "Object"))
        fixture: dict = {
            "id": fqcn,
            "type": fqcn,
            "strategy": strategy,
            evidence_key: evidence_val,
            "values": values,
            "variants": _VARIANTS.get(sut_type, _DEFAULT_VARIANTS),
            "cycleSafe": True,
        }
        return fixture

    # ── Strategy 2: constructor ────────────────────────────────────────────────
    best_ctor = _best_public_constructor(constructors)
    if best_ctor and kind not in ("interface", "abstract", "annotation"):
        strategy = "constructor"
        evidence_key = "constructorEvidence"
        evidence_val = best_ctor.get("evidenceId", "")
        values = _params_defaults(best_ctor.get("params", []))
        return {
            "id": fqcn,
            "type": fqcn,
            "strategy": strategy,
            evidence_key: evidence_val,
            "values": values,
            "variants": _VARIANTS.get(sut_type, _DEFAULT_VARIANTS),
            "cycleSafe": True,
        }

    # ── Strategy 3: static factory method ─────────────────────────────────────
    factory_m = _static_factory_method(fqcn, methods)
    if factory_m and kind not in ("interface", "abstract"):
        strategy = "factory"
        evidence_key = "factoryEvidence"
        evidence_val = factory_m.get("evidenceId", "")
        values = _params_defaults(factory_m.get("params", []))
        return {
            "id": fqcn,
            "type": fqcn,
            "strategy": strategy,
            evidence_key: evidence_val,
            "values": values,
            "variants": _VARIANTS.get(sut_type, _DEFAULT_VARIANTS),
            "cycleSafe": True,
        }

    # ── Strategy 4: mock ──────────────────────────────────────────────────────
    if kind in ("interface", "abstract") and mockito_ok:
        return {
            "id": fqcn,
            "type": fqcn,
            "strategy": "mock",
            "values": {},
            "variants": _VARIANTS.get(sut_type, _DEFAULT_VARIANTS),
            "cycleSafe": True,
        }

    # ── Strategy 5: degraded mock ─────────────────────────────────────────────
    # No builder, no public ctor, no static factory, and not a clean
    # interface/abstract+Mockito case. The schema's `strategy` enum does not
    # admit "none", so we fall back to a degraded mock and flag it explicitly:
    #   - strategy:  "mock"          (only enum-valid choice for this case)
    #   - degraded:  true            (consumers must treat as fragile)
    #   - cycleSafe: false           (no guarantees about transitive cycles)
    #   - variants:  empty           (no scenarios are safe to enumerate)
    # If Mockito itself is unavailable, mocking will simply fail at runtime —
    # the orchestrator should skip generation rather than retry.
    return {
        "id": fqcn,
        "type": fqcn,
        "strategy": "mock",
        "degraded": True,
        "values": {},
        "variants": [],
        "cycleSafe": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Build orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def build(state_dir: Path, contracts_dir: Path | None = None) -> dict:
    """Build and return the full fixture-catalog dict."""
    _contracts_dir = contracts_dir or (state_dir / "symbol-contracts")

    stack_profile = _safe_load(state_dir / "stack-profile.json", {})
    mockito_ok = _mockito_available(stack_profile)

    # Build a lookup: fqcn → sut_type from classification-index
    classification_raw = _safe_load(state_dir / "classification-index.json", {})
    type_by_fqcn: dict[str, str] = {
        c["fqcn"]: c.get("type", "component")
        for c in classification_raw.get("classes", [])
    }

    fixtures: list[dict] = []
    contract_files = sorted(_contracts_dir.glob("*.json")) if _contracts_dir.exists() else []

    for cp in contract_files:
        try:
            contract = load_json(cp)
        except Exception as exc:
            print(f"[WARN] cannot read contract {cp.name}: {exc}", file=sys.stderr)
            continue

        fqcn = contract.get("fqcn", "")
        if not fqcn or _is_test_class(fqcn):
            continue

        sut_type = type_by_fqcn.get(fqcn, "component")
        fixture = _build_fixture(contract, sut_type, mockito_ok)
        if fixture:
            fixtures.append(fixture)

    if not contract_files:
        print("[INFO] no symbol contracts found; fixture-catalog will be empty")

    return {"schemaVersion": 1, "fixtures": fixtures}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Build state/fixture-catalog.json from symbol contracts and stack profile.\n\n"
            "Strategy selection (evidence-only, no guessing):\n"
            "  builder     — contract.builders[] non-empty with valid .Builder() entry\n"
            "  constructor — public constructor in contract.constructors[]\n"
            "  factory     — static factory method returning the type\n"
            "  mock        — interface/abstract + Mockito available in stack\n"
            "  none        — no valid instantiation strategy found\n\n"
            "Strictly prohibited: Type_Builder direct usage, invented params."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out", required=True, help="State directory (e.g. state/)")
    ap.add_argument(
        "--contracts",
        default=None,
        help="Symbol-contracts directory (default: <out>/symbol-contracts)",
    )
    args = ap.parse_args()

    state_dir = Path(args.out).resolve()
    contracts_dir = Path(args.contracts).resolve() if args.contracts else None

    result = build(state_dir, contracts_dir)
    validate("fixture-catalog", result)
    atomic_write_json(state_dir / "fixture-catalog.json", result)

    n = len(result["fixtures"])
    by_strat: dict[str, int] = {}
    for f in result["fixtures"]:
        s = f["strategy"]
        by_strat[s] = by_strat.get(s, 0) + 1
    summary = "  ".join(f"{s}={c}" for s, c in sorted(by_strat.items()))
    print(f"[OK] state/fixture-catalog.json  {n} fixture(s)  {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
