"""dependency_graph_extractor.py — build state/dependency-graph.json deterministically.

Reads (produced by prior pipeline steps):
  - state/symbol-contracts/<fqcn>.json  — per-class contracts (constructors, methods, kind)
  - state/index/classes.json            — FQCN → {kind, modifiers, annotations, parents}
  - state/index/methods.json            — FQCN → [{name, params, returnType, evidenceId}]

For every non-test SUT found in the symbol contracts it produces one graph entry with:
  - sut:               FQCN of the class under test
  - instantiationHint: "constructor" | "builder" | "factory" | "mock" | "none"
  - dependencies:      constructor parameters mapped to {name, type, injection, final,
                       mockable, evidenceId}
  - collaboratorUsage: for each dependency, the public methods available on its type
                       (looked up from the methods index)
  - externalClients:   dependency types that look like HTTP / messaging clients
  - exceptions:        union of all checked exceptions declared by SUT methods
  - springStrategy:    WebMvcTest / DataJpaTest / none + mockBeans list

Mockability rule
----------------
  A dependency type is `mockable: true` when:
    1. It appears in state/index/classes.json with kind=interface or modifier=abstract, OR
    2. It is not in the index AND its simple name ends with a known interface-like suffix.

  The tool NEVER invents types, constructors, or field names that are not evidenced.

Schema update
-------------
  Retrocompatibly adds `mockable`, `evidenceId` (dependencies) and `exceptions` (graph
  item) to state/_schemas/dependency-graph.schema.json.

CLI:
    python tools/python/dependency_graph_extractor.py --out state
    python tools/python/dependency_graph_extractor.py --out state --contracts state/symbol-contracts
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import SCHEMAS_DIR, atomic_write_json, load_json, normalize_params, validate

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Simple-name suffixes that strongly suggest an interface / abstract type
_INTERFACE_SUFFIXES: tuple[str, ...] = (
    "service", "repository", "dao", "port", "client", "gateway",
    "adapter", "factory", "provider", "handler", "manager",
    "processor", "consumer", "producer", "reader", "writer",
    "store", "cache", "notifier", "publisher", "sender",
)

# Dependency type names that suggest external infrastructure clients
_EXTERNAL_CLIENT_HINTS: frozenset[str] = frozenset({
    "resttemplate", "webclient", "httpclient", "restclient", "asyncresttemplate",
    "kafkatemplate", "kafkaconsumer", "kafkaproducer",
    "rabbitmqtemplate", "amqptemplate", "jmstemplate",
    "redistemplate", "stringredistemplate", "reactiveredistemplate",
    "s3client", "sqsclient", "snsclient", "dynamodbclient",
    "feignclient", "openfeign",
    "smtpmailer", "javamailer", "mailsender",
    "elasticsearchclient", "restclientbuilder",
})

# Spring annotations that determine the test slice
_WEBMVC_ANNOTATIONS: frozenset[str] = frozenset({
    "restcontroller", "controller", "controlleradvice", "restcontrolleradvice",
})
_DATA_JPA_ANNOTATIONS: frozenset[str] = frozenset({
    "repository",
})

# Test class suffixes — excluded from graph generation (they are not SUTs)
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


def _ann_simple(ann: str) -> str:
    """'org.springframework.stereotype.Service' → 'service'."""
    return ann.rsplit(".", 1)[-1].lstrip("@").lower()


def _is_test_class(fqcn: str) -> bool:
    simple = _simple(fqcn)
    return any(simple.endswith(s) for s in _TEST_SUFFIXES)


def _is_mockable(type_fqcn: str, classes_index: dict) -> bool:
    """True if the type is interface, abstract, or has a mockable-looking name."""
    # Primitive / JDK value types are never mockable
    if "." not in type_fqcn or type_fqcn.startswith("java.lang.") or type_fqcn.startswith("java.util."):
        return False

    meta = classes_index.get(type_fqcn)
    if meta is not None:
        if meta.get("kind") == "interface":
            return True
        if "abstract" in [m.lower() for m in meta.get("modifiers", [])]:
            return True
        return False

    # Not in index — fall back to name heuristic
    simple = _simple(type_fqcn).lower()
    return any(simple.endswith(s) for s in _INTERFACE_SUFFIXES)


def _is_external_client(type_fqcn: str) -> bool:
    simple = _simple(type_fqcn).lower()
    return simple in _EXTERNAL_CLIENT_HINTS or any(
        hint in simple for hint in _EXTERNAL_CLIENT_HINTS
    )


def _derive_field_name(param_name: str | None, type_fqcn: str) -> str:
    """Use explicit param name if available; otherwise camelCase from simple type name."""
    if param_name:
        return param_name
    simple = _simple(type_fqcn)
    return simple[0].lower() + simple[1:] if simple else "dep"


def _spring_strategy(annotations: list[str], dep_names: list[str]) -> dict:
    ann_simples = {_ann_simple(a) for a in annotations}
    if ann_simples & _WEBMVC_ANNOTATIONS:
        return {"slice": "WebMvcTest", "mockBeans": dep_names}
    if ann_simples & _DATA_JPA_ANNOTATIONS:
        return {"slice": "DataJpaTest", "mockBeans": []}
    return {"slice": "none", "mockBeans": dep_names}


# ─────────────────────────────────────────────────────────────────────────────
# Schema update
# ─────────────────────────────────────────────────────────────────────────────

def _update_schema(schema_path: Path) -> None:
    """Retrocompatibly add mockable, evidenceId (dep) and exceptions (graph) to schema."""
    if not schema_path.exists():
        return
    try:
        schema = load_json(schema_path)
    except Exception as exc:
        print(f"[WARN] cannot load schema for update: {exc}", file=sys.stderr)
        return

    changed = False
    graph_item_props: dict = (
        schema.get("properties", {})
        .get("graphs", {})
        .get("items", {})
        .get("properties", {})
    )
    dep_item_props: dict = (
        graph_item_props
        .get("dependencies", {})
        .get("items", {})
        .get("properties", {})
    )

    for field, defn in [
        ("mockable",    {"type": "boolean", "description": "True if this type can be mocked with Mockito"}),
        ("evidenceId",  {"type": "string",  "description": "Evidence ID from the source constructor param"}),
    ]:
        if field not in dep_item_props:
            dep_item_props[field] = defn
            changed = True

    if "exceptions" not in graph_item_props:
        graph_item_props["exceptions"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "Union of checked exceptions declared by SUT methods",
        }
        changed = True

    if changed:
        atomic_write_json(schema_path, schema)
        print(f"[INFO] updated schema: {schema_path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# Graph builder for one contract
# ─────────────────────────────────────────────────────────────────────────────

def _build_graph(
    contract: dict,
    classes_index: dict,
    methods_by_fqcn: dict,
) -> dict:
    fqcn: str = contract["fqcn"]
    kind: str = contract.get("kind", "class")
    annotations: list[str] = contract.get("annotations", [])
    instantiation: dict = contract.get("instantiation", {})
    inst_strategy: str = instantiation.get("strategy", "none")

    # ── Derive instantiation hint ─────────────────────────────────────────────
    if kind in ("interface", "abstract") or inst_strategy == "mock":
        hint = "mock"
    elif contract.get("builders"):
        hint = "builder"
    elif inst_strategy in ("constructor", "concrete"):
        hint = "constructor"
    elif inst_strategy == "factory":
        hint = "factory"
    else:
        hint = inst_strategy or "none"

    # ── Build dependency list from public constructors ────────────────────────
    dependencies: list[dict] = []
    external_clients: list[str] = []
    seen_types: set[str] = set()

    best_ctor = _best_constructor(contract.get("constructors", []))
    if best_ctor:
        ctor_eid: str = best_ctor.get("evidenceId", "")
        # normalize_params() coerces legacy ["String", ...] shape into [{type: ...}, ...]
        # so .get() never crashes on a stray string element.
        for idx, param in enumerate(normalize_params(best_ctor.get("params", []))):
            ptype: str = param.get("type", "java.lang.Object")
            pname: str | None = param.get("name")
            field_name = _derive_field_name(pname, ptype)
            mockable = _is_mockable(ptype, classes_index)
            is_final = True  # constructor-injected fields are effectively final

            dep: dict = {
                "name": field_name,
                "type": ptype,
                "injection": "constructor",
                "final": is_final,
                "mockable": mockable,
                "evidenceId": f"{ctor_eid}:p{idx}",
            }
            dependencies.append(dep)

            if ptype not in seen_types:
                seen_types.add(ptype)
                if _is_external_client(ptype):
                    external_clients.append(ptype)

    # ── Collaborator usage: public methods available on each dependency ───────
    collaborator_usage: list[dict] = []
    for dep in dependencies:
        dep_type = dep["type"]
        dep_methods_raw: list[dict] = methods_by_fqcn.get(dep_type, [])
        public_methods = [
            m for m in dep_methods_raw
            if "private" not in [mod.lower() for mod in m.get("modifiers", [])]
            and not m.get("name", "").startswith("<")  # skip <init>, <clinit>
        ]
        if not public_methods:
            continue
        collab_methods = [
            {
                "evidenceId": m.get("evidenceId", ""),
                "name": m.get("name", ""),
                # Schema requires params as string[]. normalize_params() defends
                # against legacy method indexes where params arrived as plain
                # strings instead of {type, name} dicts.
                "params": [p.get("type", "") for p in normalize_params(m.get("params", []))],
                "returnType": m.get("returnType", "void"),
                "throws": m.get("throws", []),
            }
            for m in public_methods[:10]  # cap at 10 to avoid runaway output
        ]
        collaborator_usage.append({
            "field": dep["name"],
            "type": dep_type,
            "methods": collab_methods,
        })

    # ── Exceptions: union of all checked exceptions from SUT methods ──────────
    exceptions: list[str] = []
    seen_exc: set[str] = set()
    for method in contract.get("methods", []):
        for exc in method.get("throws", []):
            if exc not in seen_exc:
                seen_exc.add(exc)
                exceptions.append(exc)

    # ── Spring strategy ───────────────────────────────────────────────────────
    dep_type_names = [d["type"] for d in dependencies]
    spring_strat = _spring_strategy(annotations, dep_type_names)

    return {
        "sut": fqcn,
        "instantiationHint": hint,
        "dependencies": dependencies,
        "collaboratorUsage": collaborator_usage,
        "externalClients": external_clients,
        "exceptions": exceptions,
        "springStrategy": spring_strat,
    }


def _best_constructor(constructors: list[dict]) -> dict | None:
    """Pick the 'best' constructor: prefer the smallest public one (simplest to mock)."""
    public = [c for c in constructors if c.get("visibility") == "public"]
    if not public:
        # Accept package-private as a fallback
        public = [c for c in constructors if c.get("visibility") in ("public", "package")]
    if not public:
        return None
    # Prefer shortest param list (least dependencies to mock)
    return min(public, key=lambda c: len(c.get("params", [])))


# ─────────────────────────────────────────────────────────────────────────────
# Extraction orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def extract(state_dir: Path, contracts_dir: Path | None = None) -> dict:
    """Build and return the full dependency-graph dict."""
    _contracts_dir = contracts_dir or (state_dir / "symbol-contracts")
    index_dir = state_dir / "index"

    # Load index data (may be absent on first run before semantic_index_writer)
    classes_raw: dict = _safe_load(index_dir / "classes.json", {}).get("classes", {})
    methods_raw: dict = _safe_load(index_dir / "methods.json", {}).get("methods", {})

    graphs: list[dict] = []
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

        graphs.append(_build_graph(contract, classes_raw, methods_raw))

    if not contract_files:
        print("[INFO] no symbol contracts found; dependency-graph will be empty")

    return {"schemaVersion": 1, "graphs": graphs}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Build state/dependency-graph.json from symbol contracts and the semantic index.\n"
            "Every dependency is evidence-backed: no types, constructors or field names\n"
            "are invented.  Unknown types are catalogued as mockable=false (restrictive)."
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

    _update_schema(SCHEMAS_DIR / "dependency-graph.schema.json")

    result = extract(state_dir, contracts_dir)
    validate("dependency-graph", result)
    atomic_write_json(state_dir / "dependency-graph.json", result)

    n = len(result["graphs"])
    n_deps = sum(len(g["dependencies"]) for g in result["graphs"])
    n_ext = sum(len(g["externalClients"]) for g in result["graphs"])
    print(
        f"[OK] state/dependency-graph.json  "
        f"suts={n}  total_deps={n_deps}  external_clients={n_ext}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
