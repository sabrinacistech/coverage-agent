"""batch_protocol.py — pure logic for the incremental batch handoff (no I/O).

The batch handoff turns the old "1 target → 1 request → wait → 1 response →
apply" loop into "up to N targets → 1 batch request → 1 batch response → apply
all → repair only the failures". This module owns the *deterministic* pieces so
they are unit-testable in isolation, free of disk/Maven/handoff side effects:

  * target selection (first N still-pending plan items)
  * the generation request envelope (schema test-generation-batch-v1)
  * validation of the generation response (unknown target ⇒ reject; a per-item
    `skipped`/`failed` never fails the whole batch)
  * the per-target state machine + run manifest with rolled-up totals
  * the between-batches advance decision (the 80% / 50% rules)
  * the repair request envelope (only the failed items) + abandon-after-N rounds

batch_runner.py is the thin I/O shell that wires these to the file handoff, the
test_patch_applier and the test runner. Everything here is a pure function over
plain dicts.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# ── Schema version tags (the file protocol's contract) ─────────────────────────
SCHEMA_GENERATION_REQUEST = "test-generation-batch-v1"
SCHEMA_GENERATION_RESPONSE = "test-generation-batch-response-v1"
SCHEMA_REPAIR_REQUEST = "test-repair-batch-v1"
SCHEMA_REPAIR_RESPONSE = "test-repair-batch-response-v1"
SCHEMA_MANIFEST = "test-batch-manifest-v1"

# ── Per-target lifecycle states (manifest.targets[id].status) ──────────────────
PENDING = "PENDING"
GENERATION_REQUESTED = "GENERATION_REQUESTED"
GENERATED = "GENERATED"
SKIPPED = "SKIPPED"
GENERATION_FAILED = "GENERATION_FAILED"
APPLIED = "APPLIED"
PATCH_FAILED = "PATCH_FAILED"
COMPILE_FAILED = "COMPILE_FAILED"
TEST_FAILED = "TEST_FAILED"
REPAIR_REQUESTED = "REPAIR_REQUESTED"
REPAIRED = "REPAIRED"
PASSED = "PASSED"
ABANDONED = "ABANDONED"

# Terminal states never get re-selected or repaired again.
TERMINAL_STATES = frozenset({PASSED, SKIPPED, ABANDONED})
# States that still count as "failed and repairable" (a repair round may fix them).
REPAIRABLE_STATES = frozenset({COMPILE_FAILED, TEST_FAILED, PATCH_FAILED})

# Item-level statuses inside the LLM responses. NEED_MORE_CONTEXT is the answer
# the contextPolicy mandates when a needed symbol is absent from the request — the
# model must NOT invent it (task 3). It is accepted case-insensitively (the spec
# writes it upper-case) via _is_needs_context and maps to SKIPPED(MISSING_CONTEXT).
_GEN_ITEM_STATUSES = frozenset({"generated", "skipped", "failed"})
_REPAIR_ITEM_STATUSES = frozenset({"repaired", "skipped", "abandoned", "failed"})


def _is_needs_context(status: Any) -> bool:
    """True when an item status is the NEED_MORE_CONTEXT signal (any casing/dashes)."""
    if not isinstance(status, str):
        return False
    return status.strip().upper().replace("-", "_") in (
        "NEED_MORE_CONTEXT", "NEEDS_MORE_CONTEXT")


# ── Context policy (batch-only) + pre-flight / repair-loop vocabulary ──────────
# Shipped verbatim in every generation/repair request. It is the machine-readable
# form of SELF_CONTAINED_RULE: the model must NOT read the repository or production
# code, and when a needed symbol is absent from the request it must answer
# NEED_MORE_CONTEXT instead of inventing a constructor/method/getter/constant.
CONTEXT_POLICY = {
    "scope": "batch_only",
    "allowRepositoryRead": False,
    "allowProductionCodeRead": False,
    "onMissingContext": "NEED_MORE_CONTEXT",
}

# Reason persisted on a target the pre-flight evidence gate skips before any LLM
# call (task 2): there is not enough symbol/type evidence in the request metadata
# to generate a test without reading the repository.
PREFLIGHT_SKIP_REASON = "Falta de evidencia de tipos/parámetros en metadatos"

# Reason persisted when the method under test has NO body in the shipped
# sutSourceCode projection (task 2). Without the body the model would have to
# read the repository (forbidden) or invent the behaviour, so the target is
# skipped before the handoff instead of being sent to fail/hallucinate. Two
# aliases for the same constant: the runner-facing name and the abandon name.
PREFLIGHT_BODY_MISSING_REASON = "TARGET_METHOD_BODY_MISSING"
ABANDON_TARGET_BODY_MISSING = PREFLIGHT_BODY_MISSING_REASON

# Reason persisted when the target is a static initializer (<clinit>) but the
# request carries no enum-constant evidence to exercise it (task 3). Generating a
# <clinit> test without the constants forces a repository read or invented
# symbols, so it is skipped/deferred to NEED_MORE_CONTEXT instead.
PREFLIGHT_CLINIT_NO_CONSTANTS = "CLINIT_WITHOUT_ENUM_CONSTANTS"

# repairCause.kind set when the missing compiler symbol is an undeclared
# test-local variable/identifier (task 4) — the exact failure mode this milestone
# attacks (the model used `controller`/`adapter`/… without declaring them). It
# tells the repair model the fix is to DECLARE/CONSTRUCT the fixture, not to
# change production behaviour.
UNDECLARED_TEST_FIXTURE_KIND = "UNDECLARED_TEST_FIXTURE"

# Repair-loop abandon reasons (task 6): why an item is dropped instead of spending
# another handoff/round of tokens on it.
ABANDON_NO_ACTIONABLE_LOGS = "NO_ACTIONABLE_LOGS"
ABANDON_PATCHER_NO_DIAGNOSTICS = "PATCHER_REJECTED_WITHOUT_DIAGNOSTICS"
ABANDON_NO_PROGRESS = "NO_PROGRESS_AFTER_REPAIR"
ABANDON_REPEATED_SIGNATURE = "REPEATED_FAILURE_SIGNATURE"
ABANDON_MISSING_CONTEXT = "MISSING_CONTEXT"
_PATCH_DESCRIPTOR_REQUIRED = frozenset({
    "schemaVersion",
    "patchId",
    "sut",
    "testClass",
    "methods",
})
_FULL_FILE_PATCH_KEYS = frozenset({
    "operation",
    "targetFile",
    "language",
    "content",
    "coveredMethod",
    "testMethods",
})
_ANNOTATION_IMPORTS = {
    "@DisplayName": "org.junit.jupiter.api.DisplayName",
    "DisplayName": "org.junit.jupiter.api.DisplayName",
    "@Autowired": "org.springframework.beans.factory.annotation.Autowired",
    "Autowired": "org.springframework.beans.factory.annotation.Autowired",
    "@SpringBootTest": "org.springframework.boot.test.context.SpringBootTest",
    "SpringBootTest": "org.springframework.boot.test.context.SpringBootTest",
}
_COMMON_FORBIDDEN_IMPORTS = [
    "org.junit.jupiter.api.DisplayName",
    "org.springframework.beans.factory.annotation.Autowired",
    "org.springframework.boot.test.context.SpringBootTest",
]
_JAVA_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "new", "throw",
})

# ── Generation / repair rules shipped to Claude Code in every request ──────────
# The QUALITY_GATE_RULES mirror the deterministic G6 linter (tools/python/
# test_linter.py, skills/11-quality). A test that breaks any of these is ROLLED
# BACK by the patcher (G6_LINTER_FAIL) and bounced to a repair round — so stating
# them up front is what makes a batch pass on the first handoff instead of after
# one or two repair rounds. Keep this list in sync with test_linter's TQG_* kinds.
QUALITY_GATE_RULES = [
    # TQG_02_NO_AAA — every @Test body must contain the three AAA marker comments.
    "Every @Test method body MUST contain the three Arrange/Act/Assert marker "
    "comments, literally: `// given`, `// when`, `// then`.",
    # TQG_11_EAGER_TEST — exactly one `// when` per test.
    "Use exactly ONE `// when` marker per test method (one action under test); "
    "split multiple actions into separate @Test methods.",
    # TQG_03_NAMING — method names must match one of the two accepted forms.
    "Name every @Test method as either `shouldX_whenY` (e.g. "
    "`shouldReturnUnknown_whenInputBlank`) OR three lowercase-led snake_case parts "
    "`method_condition_expected` (e.g. `getId_afterSetId_returnsValue`). No other "
    "shape passes the naming gate.",
    # TQG_11_NON_DETERMINISTIC — no wall-clock / randomness / sleeps.
    "Never call non-deterministic APIs in a test: no Instant.now(), "
    "LocalDate/LocalDateTime.now(), System.currentTimeMillis()/nanoTime(), "
    "Math.random(), UUID.randomUUID(), or Thread.sleep(...). Use fixed values "
    "(e.g. Instant.parse(\"2026-01-01T00:00:00Z\")).",
    # TQG_12_OVER_MOCK / _SUT — never mock the class under test or value objects.
    "Never mock the system under test (no @Mock/@Spy on the SUT type) nor value "
    "objects/DTOs/enums/collections; construct them directly.",
    # TQG_12_TAUTOLOGY — assert real behaviour.
    "Assert the real behaviour; never assertTrue(true)/assertFalse(false) or "
    "assert a value against itself.",
    # TQG_09_LOGIC_IN_TEST — no control flow in test bodies.
    "No control flow inside a test body (no if/for/while/switch); keep tests "
    "straight-line Arrange/Act/Assert.",
]

# ── Hermetic / self-contained payload rule ─────────────────────────────────────
# The request JSON under the batch folder is the LLM's ONLY world. Every fact it
# needs is embedded in this file: the SUT verbatim (target.sutSourceCode), the
# dependency shapes (target.dependencySignatures), the failing test
# (failedItem.currentTestSource) and the exact compiler output
# (failedItem.compilerErrorDetails). Reading the local Git working tree invites
# stale/ghost code (the bug this milestone fixes) — so it is forbidden.
SELF_CONTAINED_RULE = (
    "You are an ISOLATED entity. Operate ONLY on the information contained in "
    "THIS request JSON inside the batch folder. NEVER read, index, open, glob, "
    "or infer from local repository files, the Git working tree, production "
    "sources, pom.xml/build files, jacoco, or any path outside this JSON. The "
    "behaviour of the system under test is provided as method/constructor bodies "
    "in target.sutSourceCode (its signatures, imports and fields are in "
    "target.allowedImports / target.evidenceRefs); the shapes of its "
    "collaborators in target.dependencySignatures; the test that just "
    "failed in failedItem.currentTestSource; the exact javac/Maven output "
    "in failedItem.compilerErrorDetails; and any patcher gate/perimeter "
    "rejection (e.g. G2 orphan evidence) in failedItem.patcherErrorDetails. "
    "Treat these fields as the single source "
    "of truth. If a fact is not present in this JSON, it does not exist for you — "
    "skip/abandon the target instead of reading the repository."
)

GENERATION_RULES = [
    SELF_CONTAINED_RULE,
    "Generate Java tests that compile.",
    "Do not modify production code.",
    "Do not add dependencies unless explicitly authorized.",
    "Use Arrange / Act / Assert.",
    "Use the JUnit/Mockito/assertion framework already used by the project.",
    "Do not invent expected outputs; derive them from source behaviour, existing "
    "tests, the symbol contract, execution feedback, or explicit evidence.",
    "Prefer small deterministic unit tests; mock external dependencies.",
    "Avoid starting a full Spring context unless strictly necessary.",
    # Completion contract — the LLM returns a MINIMAL completion; Python builds the
    # canonical patchDescriptor (schemaVersion/patchId/sut/testClass/testPackage/
    # template/allowedImports) from the target. This removes the whole class of
    # "patchDescriptor missing required keys" failures and forbids the model from
    # overriding any structural metadata.
    "Do NOT return a patchDescriptor, testSource, or full test file. For each target "
    "return ONLY: targetId, status, methods, reason, missingSymbols. The runner "
    "builds the canonical patchDescriptor (test class, package, imports, patchId) "
    "from the target — never set or override the test class, package or imports "
    "yourself. The canonical test class is target.canonicalTestClass.",
    "Return methods ONLY for targets with status 'generated'. Each method is "
    "{name, annotations, body, evidenceIds}; annotations default to [\"@Test\"].",
    "Use ONLY symbols whose imports are in target.allowedImports in your method "
    "bodies and annotations. Do not use DisplayName, Autowired, SpringBootTest, "
    "Spring injection annotations, or domain exceptions unless they are explicitly "
    "listed in target.allowedImports.",
    "Use ONLY target.allowedEvidenceIds in every method.evidenceIds. If no "
    "allowedEvidenceIds justify a test method, mark that target skipped/failed "
    "instead of inventing symbols.",
    "The Java body may call methods on the SUT only when those method names are "
    "listed in target.evidenceRefs with kind='method'. Constructors alone do not "
    "authorize assertions through unevidenced SUT getters/methods.",
    # fixturePlan contract (task 1) — the deterministic construction recipe the
    # runner derives from the context-pack, so the model COPIES the fixture instead
    # of inventing collaborator variables (the undeclared-symbol bug this fixes).
    "Use the provided target.fixturePlan to build the test fixture: declare the "
    "system under test as the local variable target.fixturePlan.sutVariable, "
    "instantiate it with target.fixturePlan.constructor, and create EACH entry of "
    "target.fixturePlan.requiredCollaborators using its creationStrategy — "
    "'literal' uses the given value, 'new' constructs a real instance, 'mock' "
    "declares a Mockito mock (a local mock(Type.class) or a @Mock field, allowed "
    "only when Mockito is in target.allowedImports). Do NOT reference any variable "
    "that is not declared locally in the test method body; never invent a "
    "collaborator the fixturePlan did not list.",
    "If target.fixturePlan.complete is false, do NOT improvise the missing "
    "construction: respond with status NEED_MORE_CONTEXT and put the entries of "
    "target.fixturePlan.unresolvedCollaborators in missingSymbols.",
    # creationRecipe contract (task 1/2) — when a 'new' collaborator carries HOW to
    # build it, copy that exactly instead of inventing a different construction.
    "When a target.fixturePlan.requiredCollaborators entry has a creationRecipe, "
    "build it EXACTLY as the recipe says: use creationRecipe.expression verbatim "
    "when present; otherwise construct via creationRecipe.kind + creationRecipe."
    "evidenceId (constructor/builder/factory) using only the projected argument "
    "values and dependencySignatures. Never substitute a different constructor.",
    # enumConstants contract (task 3) — an enum/<clinit> target lists its constants.
    "If target.enumConstants is non-empty the SUT is an enum: reference ONLY those "
    "constant names (e.g. EnumType.NAME or EnumType.valueOf(\"NAME\")) and never "
    "invent an enum constant that is not in target.enumConstants.",
    "When target.targetEvidenceRequired is true, every generated test method MUST "
    "include at least one id from target.targetEvidenceIds in method.evidenceIds. "
    "If target.targetEvidenceIds is empty, mark the target skipped/failed instead "
    "of generating code for an unevidenced method.",
    "If a target includes context.syntheticCoverageTargets, DO NOT skip it as a "
    "lambda. Generate tests for the listed real parent method and cover the "
    "internal lambda branch behaviour through that parent method. Prefer at "
    "least one success-path test and one missing/fallback/exception-path test "
    "when the parent method branches through Optional.orElse*, suppliers, or "
    "similar deferred lambdas.",
    "Escape Java string literals correctly: \\n \\r \\t \\\\ \\\" — never a raw "
    "newline/tab inside a normal String literal. If you need a control character "
    "in test input, prefer building it explicitly (e.g. \"a\" + (char) 10 + \"b\").",
    "For sanitizers/encoders/maskers/normalizers/parsers include edge cases when "
    "applicable: null, blank, newline, tab, CR, quotes, backslash, angle brackets, "
    "unicode/non-ASCII, already-sanitized input, very long input.",
    *QUALITY_GATE_RULES,
]
REPAIR_RULES = [
    SELF_CONTAINED_RULE,
    "Do not modify production code; fix only the generated tests.",
    "Keep the original test intent.",
    "Prefer minimal changes.",
    "If an expected value is wrong, infer it from the source behaviour.",
    "If a Java string literal is invalid, escape it (\\n \\r \\t \\\\ \\\").",
    "Use the canonical test class exactly: patchDescriptor.testClass MUST equal "
    "failedItem.canonicalTestClass. Do not keep or create suffix variants such "
    "as *CtorTest, *ConstructorTest, *GeneratedTest, or *UnitTest.",
    "Use ONLY failedItem.allowedImports in patchDescriptor.allowedImports. Remove "
    "any import reported as not whitelisted. Do not add DisplayName, Autowired, "
    "SpringBootTest, Spring injection annotations, or domain exceptions unless "
    "they are explicitly listed in failedItem.allowedImports.",
    "Use ONLY failedItem.allowedEvidenceIds in every method.evidenceIds. If no "
    "allowedEvidenceIds justify a repair, mark that target abandoned with a reason.",
    "The repaired Java body may call methods on the SUT only when those method "
    "names are listed in failedItem.evidenceRefs with kind='method'. Constructors "
    "alone do not authorize unevidenced SUT getters/methods.",
    "When failedItem.targetEvidenceRequired is true, every repaired test method "
    "MUST include at least one id from failedItem.targetEvidenceIds. If that list "
    "is empty, abandon the target instead of repairing with invented symbols.",
    *QUALITY_GATE_RULES,
]


# ── Target selection ────────────────────────────────────────────────────────────

def select_batch(plan_items: list[dict], processed_ids: set[str], batch_size: int) -> list[dict]:
    """First ``batch_size`` plan items whose targetId is not already processed.

    Order is preserved (the planner already sorted by descending coverage score),
    so the highest-value, lowest-risk targets fill the early batches.
    """
    out: list[dict] = []
    for item in plan_items:
        tid = item.get("targetId")
        if tid and tid not in processed_ids:
            out.append(item)
            if len(out) >= batch_size:
                break
    return out


# ── Pre-flight evidence gate (task 2) ────────────────────────────────────────────

def preflight_evidence_gate(target: dict) -> str | None:
    """Decide, BEFORE the LLM is called, whether a target carries enough evidence
    in its OWN request fields to be tested batch-only (no repository read). Returns
    a skip reason when context is insufficient, else None.

    The runner already projects every usable symbol of the SUT into
    ``allowedEvidenceIds`` / ``evidenceRefs`` (constructors, public methods,
    getters/setters, repository methods, constants/enum values, inherited Throwable
    getters, …) and the method under test into ``targetEvidenceIds``. So the
    presence of evidence is a faithful proxy for "can be generated from the request
    alone". A target with NO evidence at all — or whose method-under-test requires
    evidence that was not found — cannot be generated without reading production
    code, so it is skipped here instead of being sent to the model and later rolled
    back by G2 (a wasted handoff).

    Two further gates run on the ENRICHED target (the runner calls this after
    _enrich_targets_with_imports, so targetMethodName/sutSourceCode are present):
      * task 3 — a <clinit> target with no enum-constant evidence is skipped
        (CLINIT_WITHOUT_ENUM_CONSTANTS);
      * task 2 — a target whose method body is absent from the shipped
        sutSourceCode projection is skipped (TARGET_METHOD_BODY_MISSING)."""
    evidence_ids = target.get("allowedEvidenceIds") or []
    evidence_refs = target.get("evidenceRefs") or []
    if not evidence_ids and not evidence_refs:
        return PREFLIGHT_SKIP_REASON
    if target.get("targetEvidenceRequired") and not (target.get("targetEvidenceIds") or []):
        return PREFLIGHT_SKIP_REASON
    name = str(target.get("targetMethodName") or "").strip()
    if name == "<clinit>" and not _has_enum_constant_evidence(target):
        return PREFLIGHT_CLINIT_NO_CONSTANTS
    # Enum WITHOUT constant names (golden rule: never hand the model a target the
    # system already knows it cannot solve). An enum's value/valueOf/getter targets
    # are NOT named <clinit>/<init> — they are ordinary method targets that pass the
    # evidence gate above — yet every meaningful test for them needs a constant name
    # (Status.valueOf("ACTIVE"), Status.ACTIVE.getLabel(), …). When the SUT is an
    # enum (detected from the request alone by its implicit values()/valueOf()
    # surface) but NO constant NAME travels in the request, the model can only answer
    # NEED_MORE_CONTEXT. Skip it here instead of burning a handoff. Guarded by
    # _has_enum_value_method (values/valueOf) — NOT by the broader getter test — so a
    # normal class with a getter and no enum constants is never caught.
    if _is_enum_surface(target) and not _has_enum_constant_evidence(target):
        return PREFLIGHT_CLINIT_NO_CONSTANTS
    # Enum synthetic constructor (task 3): an enum's <init> is a compiler-generated
    # bytecode detail — EnumType(String name, int ordinal, ...) — that CANNOT be
    # called from a test; enums are exercised via values()/valueOf()/getters, not by
    # construction. Skip it ONLY when the SUT is an enum (guarded by
    # _has_enum_constant_evidence, so a NORMAL class constructor — <init> on a
    # non-enum SUT — is never affected), and allow it through only when the request
    # also projects a genuinely testable enum method (values/valueOf/public getter),
    # which would let a useful test be written. <clinit> keeps its constants-only
    # rule above (a values() method alone does not unlock it).
    if (name == "<init>" and _has_enum_constant_evidence(target)
            and not _has_testable_enum_method(target)):
        return PREFLIGHT_CLINIT_NO_CONSTANTS
    if _target_body_missing(target):
        return PREFLIGHT_BODY_MISSING_REASON
    return None


# Evidence kinds (case-insensitive) that count as an enum constant / static
# constant the generator can name in a <clinit> test (task 3).
_ENUM_CONSTANT_KINDS = frozenset({
    "enumconstant", "enumvalue", "constant", "field", "enum",
})


def _has_enum_constant_evidence(target: dict) -> bool:
    """True when the request advertises at least one enum constant / static
    constant for a <clinit> target. Looks in evidenceRefs (by kind) and in the
    explicit ``enumConstants`` hints (target-level or under ``context``)."""
    for ref in target.get("evidenceRefs") or []:
        if isinstance(ref, dict) and str(ref.get("kind") or "").lower() in _ENUM_CONSTANT_KINDS:
            return True
    if target.get("enumConstants"):
        return True
    ctx = target.get("context") or {}
    if isinstance(ctx, dict) and ctx.get("enumConstants"):
        return True
    return False


# Enum methods that make an enum target genuinely testable from the request alone:
# the implicit values()/valueOf(...) plus any public getter (get*/is*).
_TESTABLE_ENUM_METHODS = frozenset({"values", "valueof"})


def _has_enum_value_method(target: dict) -> bool:
    """True when the request projects an enum's implicit values()/valueOf(...). These
    two are a reliable enum fingerprint from the request alone — every enum exposes
    them and an ordinary class essentially never declares both — so they classify the
    SUT as an enum WITHOUT the false positives a plain getter (handled by
    _has_testable_enum_method) would introduce."""
    for ref in target.get("evidenceRefs") or []:
        if not isinstance(ref, dict) or str(ref.get("kind") or "").lower() != "method":
            continue
        if str(ref.get("name") or "").lower() in _TESTABLE_ENUM_METHODS:
            return True
    return False


def _is_enum_surface(target: dict) -> bool:
    """True when the request lets us tell the SUT is an enum, using request fields
    only (so the pre-flight gate stays self-contained). Signals: an explicit
    ``sutIsEnum`` hint the runner derived from the schema-validated symbol-contract,
    enum-constant evidence (kind enum/enumConstant), or the implicit values()/valueOf()
    methods. Deliberately stricter than _has_testable_enum_method (no bare getter) so a
    normal class is never misread as an enum."""
    if target.get("sutIsEnum"):
        return True
    if _has_enum_constant_evidence(target):
        return True
    return _has_enum_value_method(target)


def _has_testable_enum_method(target: dict) -> bool:
    """True when the request projects a genuinely testable enum method — values(),
    valueOf(...) or a public getter — so a useful test can be written even though the
    target itself is the enum constructor. Used to avoid OVER-skipping an enum
    <init> that does carry a testable surface."""
    for ref in target.get("evidenceRefs") or []:
        if not isinstance(ref, dict) or str(ref.get("kind") or "").lower() != "method":
            continue
        nm = str(ref.get("name") or "")
        if nm.lower() in _TESTABLE_ENUM_METHODS:
            return True
        if (nm.startswith("get") or nm.startswith("is")) and len(nm) > 2:
            return True
    return False


def _target_body_missing(target: dict) -> bool:
    """True when the method under test has NO body in the shipped sutSourceCode
    projection (task 2), so the model could only proceed by reading the repo or
    inventing behaviour.

    Conservative on purpose — returns False (does NOT skip) when:
      * the method is <clinit> or a synthetic lambda (handled elsewhere / has no
        own body),
      * no source projection was shipped at all (sutSourceCode empty: e.g. a
        bodies-less DTO or a run without a repo) — the evidence gate above governs
        those, over-skipping here would be wrong,
      * the projection was truncated (we cannot prove absence).
    The projection anchors every body by its signature `… name(params) { … }`, so
    the method/constructor name followed by `(` is a faithful presence probe."""
    name = str(target.get("targetMethodName") or "").strip()
    if not name or name == "<clinit>" or name.startswith("lambda$"):
        return False
    source = str(target.get("sutSourceCode") or "")
    if not source.strip() or target.get("sutSourceTruncated"):
        return False
    sut = str(target.get("sut") or "")
    simple = sut.rsplit(".", 1)[-1]
    # A constructor body (<init>) is anchored by the class simple name.
    search_name = simple if name == "<init>" else name
    if not search_name:
        return False
    return re.search(rf"\b{re.escape(search_name)}\s*\(", source) is None


# ── Repair-loop control (task 6) ─────────────────────────────────────────────────

# Summary strings that carry no actionable cause (the loop must not re-send on them).
_GENERIC_SUMMARIES = frozenset({"PATCH_REJECTED", "TEST_FAILURE", "COMPILATION_ERROR", ""})


def _has_actionable_diagnostics(failed_item: dict) -> bool:
    """True when the failure carries a concrete, model-actionable cause: a verbatim
    compiler error, a patcher gate/perimeter rejection, or raw build output."""
    return bool(
        (failed_item.get("compilerErrorDetails") or "").strip()
        or (failed_item.get("patcherErrorDetails") or "").strip()
        or (failed_item.get("buildOutput") or "").strip()
    )


def failure_signature(failed_item: dict) -> str:
    """Stable short hash of a failure's actionable cause, so the loop can detect the
    SAME failure recurring across repair rounds (same signature ⇒ abandon, never
    re-send — REPEATED_FAILURE_SIGNATURE). Built from the failure kind, the concise
    error summary, the first compiler-error lines and the patcher [BLOCKED] lines —
    the parts that change only when the underlying cause changes."""
    comp = (failed_item.get("compilerErrorDetails") or "")
    patcher = (failed_item.get("patcherErrorDetails") or "")
    blocked = [ln for ln in patcher.splitlines() if "[BLOCKED]" in ln]
    parts = [
        str(failed_item.get("failureKind") or ""),
        str(failed_item.get("errorSummary") or "").strip(),
        "\n".join(comp.splitlines()[:5]),
        "\n".join(blocked[:3]),
    ]
    raw = "||".join(p.strip() for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def repair_admission(failed_item: dict, *, previous_signature: str | None = None) -> tuple[bool, str | None]:
    """Decide whether a failed item is worth a NEW repair handoff. Returns
    ``(should_repair, abandon_reason)``; ``False`` ⇒ abandon with that reason so no
    tokens are spent on a request the model cannot act on (task 6):

      * same failure signature as the previous round → REPEATED_FAILURE_SIGNATURE
      * a patcher rejection with no patcher/compiler diagnostics → it is a bare
        ``patcher rc=3`` with no semantic cause → PATCHER_REJECTED_WITHOUT_DIAGNOSTICS
      * no actionable logs and only a generic summary → NO_ACTIONABLE_LOGS

    The NO_PROGRESS_AFTER_REPAIR rule is decided by the loop (it needs the round's
    re-apply/re-test outcome), not here."""
    signature = failure_signature(failed_item)
    if previous_signature is not None and signature == previous_signature:
        return False, ABANDON_REPEATED_SIGNATURE

    # A patcher rejection (rc=3) with no patcher/compiler diagnostics is a bare
    # "patcher rc=3" with no semantic cause → never re-send.
    if failed_item.get("failureKind") == "PATCH_REJECTED" and not (
        (failed_item.get("patcherErrorDetails") or "").strip()
        or (failed_item.get("compilerErrorDetails") or "").strip()
    ):
        return False, ABANDON_PATCHER_NO_DIAGNOSTICS

    # A genuine test failure always produced a surefire report and carries the
    # current test source to reason about, so it is actionable on its own (it gets
    # at most ONE round via weak_diagnostics if no logs accompany it).
    if failed_item.get("failureKind") == "TEST_FAILURE":
        return True, None

    summary = str(failed_item.get("errorSummary") or "").strip()
    generic = summary in _GENERIC_SUMMARIES or summary.lower().startswith("patcher rc=")
    if not (_has_actionable_diagnostics(failed_item) or not generic):
        # rc != 0 but stdout/stderr/diagnostics empty and only a generic summary.
        return False, ABANDON_NO_ACTIONABLE_LOGS
    return True, None


def weak_diagnostics(failed_item: dict) -> bool:
    """True when an item was admitted to repair on a non-generic summary alone (no
    compiler/patcher/build logs). Such an item gets AT MOST one repair round: if it
    is still failing afterwards the loop abandons it rather than guessing again."""
    return not _has_actionable_diagnostics(failed_item)


# ── Structured repair cause (task 7) ─────────────────────────────────────────────

def classify_repair_cause(failed_item: dict) -> str:
    """Label the dominant cause so the repair request is not a bare 'patcher rc=3'.

    Order matters: a verbatim compiler error is the most specific; then a patcher
    gate (naming/imports/schema/evidence/patch-descriptor) made legible from its
    [BLOCKED] line; then the lifecycle failure kind."""
    if (failed_item.get("compilerErrorDetails") or "").strip():
        return "COMPILER_ERROR"
    patcher = (failed_item.get("patcherErrorDetails") or "")
    blocked = " ".join(ln for ln in patcher.splitlines() if "[BLOCKED]" in ln).upper()
    if blocked:
        if "G6" in blocked or "LINTER" in blocked or "NAMING" in blocked:
            return "NAMING_OR_QUALITY_RULE"
        if "IMPORT" in blocked:
            return "IMPORT_RULE"
        if "G2" in blocked or "EVIDENCE" in blocked or "SYMBOL" in blocked:
            return "EVIDENCE_RULE"
        if "SCHEMA" in blocked or "DESCRIPTOR" in blocked:
            return "PATCH_DESCRIPTOR_RULE"
        return "PATCHER_GATE"
    kind = failed_item.get("failureKind")
    if kind == "TEST_FAILURE":
        return "ASSERTION_OR_RUNTIME"
    if kind == "COMPILATION_ERROR":
        return "COMPILER_ERROR"
    return "UNKNOWN"


# javac prints an undeclared identifier as a multi-line block:
#   File.java:12: error: cannot find symbol
#           controller.handle();
#           ^
#     symbol:   variable controller
#     location: class com.acme.FooTest
# The `symbol:` line carries the kind (variable/method/class) and the identifier;
# the `location:` line carries where it was referenced.
_CANNOT_FIND_SYMBOL = "cannot find symbol"
_SYMBOL_LINE_RE = re.compile(r"symbol\s*:\s*(\w+)\s+([A-Za-z_$][\w$.<>]*)")
_LOCATION_LINE_RE = re.compile(r"location\s*:\s*(.+?)\s*$")


def parse_missing_symbols(text: str) -> list[dict]:
    """Extract structured ``{symbol, name, kind, location}`` entries from javac
    'cannot find symbol' diagnostics (task 4). Deduplicated, order-preserving.
    Returns [] when the text carries no such diagnostic."""
    if not text or _CANNOT_FIND_SYMBOL not in text:
        return []
    lines = text.splitlines()
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for i, line in enumerate(lines):
        if _CANNOT_FIND_SYMBOL not in line:
            continue
        kind = name = ""
        location = ""
        # The symbol:/location: lines follow within a few lines of the header.
        for j in range(i + 1, min(i + 7, len(lines))):
            if j != i and _CANNOT_FIND_SYMBOL in lines[j]:
                break  # next diagnostic started
            if not name:
                sm = _SYMBOL_LINE_RE.search(lines[j])
                if sm:
                    kind, name = sm.group(1), sm.group(2)
            if not location:
                lm = _LOCATION_LINE_RE.search(lines[j])
                if lm:
                    location = lm.group(1)
        if not name:
            continue
        key = (kind, name, location)
        if key in seen:
            continue
        seen.add(key)
        out.append({"symbol": f"{kind} {name}".strip(), "name": name,
                    "kind": kind, "location": location})
    return out


def _rejected_files(failed_item: dict) -> list[str]:
    out = []
    for key in ("testFile", "canonicalTestFile"):
        v = failed_item.get(key)
        if v and v not in out:
            out.append(v)
    return out


def build_repair_cause(failed_item: dict, *, previous_signature: str | None = None) -> dict:
    """Structured diagnostic block embedded in each repair item (task 7). Replaces a
    bare 'patcher rc=3' with the cause kind, a one-line summary, the verbatim
    stdout/stderr the runner captured, the patcher diagnostics, the failed rules and
    the rejected files/methods, plus the previous-round signature."""
    patcher = (failed_item.get("patcherErrorDetails") or "")
    patcher_diagnostics = [ln for ln in patcher.splitlines() if "[BLOCKED]" in ln]
    compiler = (failed_item.get("compilerErrorDetails") or "")
    build_output = (failed_item.get("buildOutput") or "")
    failed_rules = [ln.split("]", 1)[0].lstrip("[")
                    for ln in compiler.splitlines() if ln.startswith("[")]
    # Task 4: structured undeclared-symbol extraction. The compiler text first
    # (it carries the per-class diagnostic), then the raw build output as a fallback.
    missing_symbols = parse_missing_symbols(compiler) or parse_missing_symbols(build_output)
    kind = classify_repair_cause(failed_item)
    # When the missing symbol is a test-local variable/identifier, the fix is to
    # DECLARE/CONSTRUCT the fixture — relabel so the repair model does not look for
    # a production-code cause.
    if any(m.get("kind") in ("variable", "") and m.get("name") for m in missing_symbols):
        kind = UNDECLARED_TEST_FIXTURE_KIND
    return {
        "kind": kind,
        "summary": str(failed_item.get("errorSummary") or "").strip(),
        "stdout": build_output,
        "stderr": compiler,
        "patcherDiagnostics": patcher_diagnostics,
        "failedRules": sorted(set(failed_rules)),
        "missingSymbols": missing_symbols,
        "rejectedFiles": _rejected_files(failed_item),
        "rejectedMethods": ([failed_item["rejectedTestClass"]]
                            if failed_item.get("rejectedTestClass") else []),
        "previousFailureSignature": previous_signature or "",
    }


# ── Generation request envelope ─────────────────────────────────────────────────

def _suggested_test_file(sut: str) -> str:
    """Conventional test path for a SUT FQCN (src/test/java/<pkg>/<Name>Test.java)."""
    return "src/test/java/" + sut.replace(".", "/") + "Test.java"


def _suggested_test_class(sut: str) -> str:
    """Conventional test FQCN for a SUT FQCN (<pkg>.<Name>Test)."""
    return f"{sut}Test" if sut else ""


def _production_file(sut: str) -> str:
    return "src/main/java/" + sut.replace(".", "/") + ".java"


def _import_policy(allowed_imports: list[str] | None) -> dict:
    allowed = set(allowed_imports or [])
    forbidden = [imp for imp in _COMMON_FORBIDDEN_IMPORTS if imp not in allowed]
    return {
        "rule": "patchDescriptor.allowedImports must be a subset of allowedImports.",
        "forbiddenUnlessExplicitlyAllowed": forbidden,
        "notes": [
            "Do not use @DisplayName unless org.junit.jupiter.api.DisplayName is allowed.",
            "Do not use @Autowired or Spring injection in unit tests unless explicitly allowed.",
            "Do not import domain exceptions unless the exact FQCN appears in allowedImports.",
        ],
    }


def _sanitize_allowed_imports(
    imports: list[str] | None, *, whitelist: list[str] | None = None
) -> list[str]:
    """Return the patcher-safe subset of *imports*: concrete FQCNs only.

    Drops:
      * wildcard / whole-package imports (``com.foo.*`` or a trailing ``*``) — the
        linter rejects any non-whitelisted ``pkg.*`` as IMPORT_PKG_NOT_WHITELISTED
        (surfaced as the G6_LINTER_FAIL gate), and the applier's prune pass
        cannot prove a wildcard unused so it would survive straight to the gate;
      * blanks / non-strings / duplicates (order preserved).

    When *whitelist* is given, entries outside it are dropped too — so a repair
    descriptor can never smuggle a non-whitelisted import past the gate. Python is
    the single source of truth for imports: we ship the full concrete whitelist and
    let test_patch_applier prune the unused ones, yielding a minimal import block.
    """
    allowed = set(whitelist) if whitelist is not None else None
    out: list[str] = []
    seen: set[str] = set()
    for imp in imports or []:
        if not isinstance(imp, str):
            continue
        imp = imp.strip()
        if not imp or imp.endswith("*"):   # blank or wildcard/whole-package
            continue
        if allowed is not None and imp not in allowed:
            continue
        if imp in seen:
            continue
        seen.add(imp)
        out.append(imp)
    return out


def _evidence_policy(allowed_evidence_ids: list[str] | None) -> dict:
    return {
        "rule": "Every method.evidenceIds entry must exist in allowedEvidenceIds.",
        "allowedCount": len(allowed_evidence_ids or []),
        "notes": [
            "Do not cite evidenceIds not listed in this request.",
            "Do not use symbols, constructors, methods, exceptions, constants, or assertions without evidence.",
            "When targetEvidenceRequired is true, cite targetEvidenceIds in every generated/repaired method.",
            "If evidence is insufficient, skip/abandon the target instead of guessing.",
        ],
    }


def _allowed_method_names(evidence_refs: list[dict] | None) -> set[str]:
    out: set[str] = set()
    for ref in evidence_refs or []:
        if not isinstance(ref, dict) or ref.get("kind") != "method":
            continue
        name = ref.get("name")
        if isinstance(name, str) and name:
            out.add(name)
    return out


def _strip_java_literals_and_comments(body: str) -> str:
    body = re.sub(r'//.*', '', body)
    body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    body = re.sub(r'"(?:\\.|[^"\\])*"', '""', body)
    body = re.sub(r"'(?:\\.|[^'\\])*'", "''", body)
    return body


def _sut_vars_in_body(body: str, sut_fqcn: str) -> set[str]:
    if not sut_fqcn:
        return set()
    simple = sut_fqcn.rsplit(".", 1)[-1]
    type_pat = rf"(?:{re.escape(sut_fqcn)}|{re.escape(simple)})"
    decl = re.compile(rf"\b(?:final\s+)?{type_pat}(?:<[^;=()]+>)?\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?:=|;)")
    return {m.group(1) for m in decl.finditer(body)}


def _validate_sut_method_calls(
    body: str,
    *,
    target_id: str,
    method_index: int,
    sut_fqcn: str,
    allowed_method_names: set[str],
) -> None:
    stripped = _strip_java_literals_and_comments(body)
    sut_vars = _sut_vars_in_body(stripped, sut_fqcn)
    if not sut_vars:
        return
    for var in sut_vars:
        call_re = re.compile(rf"\b{re.escape(var)}\s*\.\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
        for match in call_re.finditer(stripped):
            method_name = match.group(1)
            if method_name in _JAVA_KEYWORDS:
                continue
            if method_name not in allowed_method_names:
                raise BatchResponseError(
                    f"{target_id!r} patchDescriptor.methods[{method_index}].body "
                    f"calls {var}.{method_name}(), but {method_name!r} is not present "
                    "in evidenceRefs. Cite evidence for the method or skip/abandon "
                    "the target instead of using unevidenced SUT symbols."
                )


# ── fixturePlan advisory signal (round-2 task 4) ─────────────────────────────────
# fixturePlan is advisory; nothing flagged when the model STILL referenced a
# collaborator variable that is neither declared locally nor part of the plan (the
# exact undeclared-symbol bug the plan exists to prevent). This is a SIGNAL, never a
# gate: the patcher's own G-gates remain the hard contract; this only surfaces the
# smell so the runner can log it before the test runs.
# A method-call receiver: a lowercase-initial identifier immediately followed by
# `.method(`. Lowercase-initial keeps it to variable-like names (a Capitalized
# receiver is a type/static, not a fixture variable).
_RECEIVER_CALL_RE = re.compile(r"\b([a-z][A-Za-z0-9_$]*)\s*\.\s*[A-Za-z_$][\w$]*\s*\(")
# Names we treat as "declared" (generous on purpose so the advisory signal does not
# cry wolf): the target of an `=` assignment, and a name that follows a type at a
# statement / parameter / for-each boundary.
_ASSIGN_TARGET_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*=(?!=)")
_DECL_TARGET_RE = re.compile(
    r"(?:^|[;{(,])\s*(?:final\s+)?[A-Za-z_$][\w$.<>\[\],]*\s+([A-Za-z_$][\w$]*)\s*[;=:)]")


def undeclared_fixture_signal(patch: dict, fixture_plan: dict | None) -> list[str]:
    """ADVISORY (never a gate): names the model used as a method-call receiver that
    are neither declared locally, nor a patchDescriptor field, nor part of the
    fixturePlan. These are the undeclared-collaborator references the fixturePlan
    exists to prevent; surfacing them lets the runner log a signal without rejecting
    the patch (task 4). Returns [] when nothing suspicious is found."""
    if not isinstance(patch, dict):
        return []
    plan = fixture_plan if isinstance(fixture_plan, dict) else {}
    declared: set[str] = set()
    sut_var = plan.get("sutVariable")
    if isinstance(sut_var, str) and sut_var:
        declared.add(sut_var)
    for collab in plan.get("requiredCollaborators") or []:
        if isinstance(collab, dict) and isinstance(collab.get("name"), str):
            declared.add(collab["name"])
    ctor = plan.get("constructor") if isinstance(plan.get("constructor"), dict) else {}
    for p in ctor.get("params") or []:
        if isinstance(p, dict) and isinstance(p.get("name"), str):
            declared.add(p["name"])
    for field in patch.get("fields") or []:
        if not isinstance(field, dict):
            continue
        decl = str(field.get("declaration") or field.get("source") or "")
        for m in _DECL_TARGET_RE.finditer(decl):
            declared.add(m.group(1))
        fname = field.get("name")
        if isinstance(fname, str) and fname:
            declared.add(fname)
    # Package roots from imports must never be flagged (e.g. the `org` of `org.junit…`).
    import_roots: set[str] = set()
    for imp in patch.get("allowedImports") or []:
        if isinstance(imp, str) and imp:
            import_roots.add(imp.split(".", 1)[0])

    suspicious: list[str] = []
    seen: set[str] = set()
    for method in patch.get("methods") or []:
        if not isinstance(method, dict):
            continue
        body = method.get("body")
        if not isinstance(body, str) or not body:
            continue
        stripped = _strip_java_literals_and_comments(body)
        local = set(declared)
        for m in _ASSIGN_TARGET_RE.finditer(stripped):
            local.add(m.group(1))
        for m in _DECL_TARGET_RE.finditer(stripped):
            local.add(m.group(1))
        for m in _RECEIVER_CALL_RE.finditer(stripped):
            name = m.group(1)
            if (name in local or name in import_roots or name in _JAVA_KEYWORDS
                    or name in ("this", "super") or name in seen):
                continue
            seen.add(name)
            suspicious.append(name)
    return suspicious


def build_generation_request(
    run_id: str,
    batch_id: str,
    targets: list[dict],
    *,
    batch_size: int,
    mode: str = "coverage",
) -> dict:
    """Build the batch generation request (schema test-generation-batch-v1).

    One request carries up to ``batch_size`` targets. Each target projects the
    plan item into the fields the generator needs; ``context`` stays a thin dict
    the runner can enrich with snippets/evidence without changing this shape.
    """
    out_targets = []
    for i, item in enumerate(targets, start=1):
        sut = item.get("sut", "")
        allowed_imports = list(item.get("allowedImports") or [])
        allowed_evidence_ids = list(item.get("allowedEvidenceIds") or [])
        target_evidence_ids = list(item.get("targetEvidenceIds") or [])
        out_targets.append({
            "targetId": item.get("targetId", sut),
            "sut": sut,
            "method": item.get("method", ""),
            "productionFile": _production_file(sut) if sut else "",
            # Hermetic payload: the SUT's method/constructor BODIES are shipped so
            # the generator never reads the Git working tree (avoids stale/ghost
            # code). Only the bodies travel — signatures/imports/fields are already
            # carried by allowedImports/evidenceRefs above. The runner
            # (batch_runner._enrich_targets_with_imports) reads productionFile and
            # injects these; absent enrichment they stay empty/defaulted.
            "sutSourceCode": item.get("sutSourceCode", ""),
            "sutSourceTruncated": bool(item.get("sutSourceTruncated", False)),
            "dependencySignatures": list(item.get("dependencySignatures") or []),
            "canonicalTestClass": _suggested_test_class(sut),
            "suggestedTestFile": _suggested_test_file(sut) if sut else "",
            "allowedImports": allowed_imports,
            "forbiddenImports": _import_policy(allowed_imports)["forbiddenUnlessExplicitlyAllowed"],
            "importPolicy": _import_policy(allowed_imports),
            "allowedEvidenceIds": allowed_evidence_ids,
            "evidenceRefs": list(item.get("evidenceRefs") or []),
            "targetMethodName": item.get("targetMethodName", ""),
            "targetEvidenceRequired": bool(item.get("targetEvidenceRequired", False)),
            "targetEvidenceIds": target_evidence_ids,
            "evidencePolicy": _evidence_policy(allowed_evidence_ids),
            # Deterministic construction recipe derived by the runner from the
            # context-pack (task 1). When absent (request built without enrichment)
            # it stays an empty dict — the GENERATION_RULES then fall back to the
            # evidence/allowedImports contract. The model COPIES this instead of
            # inventing collaborator variables.
            "fixturePlan": item.get("fixturePlan") or {},
            # Enum-constant names for an enum/<clinit> target (task 3), derived by the
            # runner from the production source. Empty for non-enum SUTs. The model
            # references ONLY these constants — never invents one.
            "enumConstants": list(item.get("enumConstants") or []),
            "template": item.get("template"),
            "priority": item.get("score", i),
            "fixtureIds": item.get("fixtureIds", []),
            "context": item.get("context", {}),
            # Self-sufficient, batch-only structured context (task 3): the same
            # facts as the flat fields above, grouped so the model can see at a
            # glance that the request is its ONLY world. If a fact it needs is not
            # here, it must answer NEED_MORE_CONTEXT, never read the repo.
            "structuredContext": {
                "targetSource": {
                    "sut": sut,
                    "productionFile": _production_file(sut) if sut else "",
                    "sourceCode": item.get("sutSourceCode", ""),
                    "truncated": bool(item.get("sutSourceTruncated", False)),
                },
                "dependencySources": list(item.get("dependencySignatures") or []),
                "allowedApi": list(item.get("evidenceRefs") or []),
                "fixturePlan": item.get("fixturePlan") or {},
                "enumConstants": list(item.get("enumConstants") or []),
                "existingRelatedTests": list(item.get("existingRelatedTests") or []),
                "expectedBehavior": list(item.get("expectedBehavior") or []),
                "missingContextPolicy": {"allowedStatus": "NEED_MORE_CONTEXT"},
            },
        })
    return {
        # Task 4: most prominent possible machine-readable directive — first key in
        # the JSON so it is the first thing seen when the file is opened. The
        # contextPolicy + selfContainedPolicy below are the authoritative enforcement;
        # this field is the visual sentinel that stops accidental repository reads.
        "_IMPORTANT_WARNING": (
            "ISOLATED ENTITY. Operate ONLY on the information in THIS JSON. "
            "DO NOT read, open, glob, or infer from any repository file, "
            "source code, pom.xml, Git working tree, or path outside this request. "
            "If a needed symbol is absent from this JSON, respond NEED_MORE_CONTEXT."
        ),
        "schemaVersion": SCHEMA_GENERATION_REQUEST,
        "runId": run_id,
        "batchId": batch_id,
        "role": "generation",
        "mode": mode,
        "batchSize": batch_size,
        "targets": out_targets,
        "rules": list(GENERATION_RULES),
        "contextPolicy": dict(CONTEXT_POLICY),
        "missingContextPolicy": {
            "allowedStatus": "NEED_MORE_CONTEXT",
            "rule": "If a constructor, method, getter/setter, repository method, "
                    "constant/enum value, exception, domain object, factory/builder "
                    "or configuration needed to write the test is NOT present in this "
                    "request, answer with status NEED_MORE_CONTEXT and list the "
                    "missing symbols. Never invent or read them from the repository.",
            "responseShape": {"status": "NEED_MORE_CONTEXT", "missingSymbols": [], "reason": ""},
        },
        "selfContainedPolicy": {
            "rule": SELF_CONTAINED_RULE,
            "forbiddenActions": [
                "READ_SOURCE_CODE", "READ_GIT_WORKING_TREE", "READ_POM",
                "READ_JACOCO_XML", "READ_CLASSPATH", "INDEX_REPOSITORY",
            ],
            "authoritativeFields": [
                "target.sutSourceCode", "target.dependencySignatures",
                "target.allowedImports", "target.evidenceRefs",
            ],
        },
        "testClassPolicy": {
            "canonical": "Use target.canonicalTestClass exactly for patchDescriptor.testClass.",
            "forbidden": [
                "Do not create suffix variants such as *CtorTest, *ConstructorTest, *GeneratedTest, or *UnitTest.",
                "Do not derive testClass from the target method name.",
            ],
        },
        # Minimal-completion contract (the LLM does NOT build the patchDescriptor):
        # it returns only status + methods + reason + missingSymbols, and Python
        # hydrates the canonical patchDescriptor (orchestrator.batch_protocol.
        # hydrate_generation_response). This is the machine-readable shape the model
        # must follow; expectedResponse below is the concrete per-target echo.
        "responseCompletionContract": {
            "schemaVersion": "test-generation-completion-v1",
            "rule": "Do not return patchDescriptor. Do not return testSource. Return "
                    "only status, methods, reason and missingSymbols. The runner "
                    "builds the canonical patchDescriptor.",
            "targetShape": {
                "targetId": "<targetId>",
                "status": "generated|skipped|failed|NEED_MORE_CONTEXT",
                "methods": [
                    {"name": "method_condition_expected",
                     "annotations": ["@Test"],
                     "body": "// given\n...\n// when\n...\n// then\n...",
                     "evidenceIds": []}
                ],
                "reason": "",
                "missingSymbols": [],
            },
        },
        "expectedResponse": {
            "schemaVersion": SCHEMA_GENERATION_RESPONSE,
            "runId": run_id,
            "batchId": batch_id,
            "role": "generation",
            "targets": [
                {"targetId": t["targetId"],
                 "status": "generated|skipped|failed|NEED_MORE_CONTEXT",
                 "methods": [], "reason": "", "missingSymbols": []}
                for t in out_targets
            ],
        },
    }


# ── Generation response validation ──────────────────────────────────────────────

class BatchResponseError(ValueError):
    """The batch response does not satisfy the minimal protocol contract."""


def _validate_patch_descriptor(
    patch: Any,
    *,
    target_id: str,
    expected_prefix: str,
    expected_sut: str | None = None,
    expected_test_class: str | None = None,
    expected_allowed_imports: list[str] | None = None,
    expected_evidence_ids: list[str] | None = None,
    expected_evidence_refs: list[dict] | None = None,
    expected_target_evidence_ids: list[str] | None = None,
    target_evidence_required: bool = False,
) -> None:
    """Validate the canonical patch-descriptor shape before the patcher runs.

    This intentionally mirrors the stable, handoff-facing contract instead of
    importing the side-effecting patcher. The goal is to reject common LLM drift
    (full-file patches with operation/targetFile/content) at response validation
    time, with a message the user can hand back to the generator.
    """
    if not isinstance(patch, dict) or not patch:
        raise BatchResponseError(f"{target_id!r} has an empty or non-object patchDescriptor")

    full_file_keys = sorted(k for k in _FULL_FILE_PATCH_KEYS if k in patch)
    if full_file_keys:
        raise BatchResponseError(
            f"{target_id!r} patchDescriptor uses full-file patch keys {full_file_keys}; "
            "expected canonical patch-descriptor keys "
            "schemaVersion, patchId, sut, testClass, methods"
        )

    missing = sorted(_PATCH_DESCRIPTOR_REQUIRED - patch.keys())
    if missing:
        raise BatchResponseError(
            f"{target_id!r} patchDescriptor missing required keys: {missing}"
        )

    if patch.get("schemaVersion") != 1:
        raise BatchResponseError(
            f"{target_id!r} patchDescriptor.schemaVersion must be 1, "
            f"got {patch.get('schemaVersion')!r}"
        )

    patch_id = patch.get("patchId")
    if not isinstance(patch_id, str) or not patch_id.startswith(expected_prefix):
        raise BatchResponseError(
            f"{target_id!r} patchDescriptor.patchId must start with "
            f"{expected_prefix!r}, got {patch_id!r}"
        )

    sut = patch.get("sut")
    if isinstance(sut, dict):
        sut_ok = bool(sut.get("fqcn"))
    else:
        sut_ok = isinstance(sut, str) and bool(sut.strip())
    if not sut_ok:
        raise BatchResponseError(
            f"{target_id!r} patchDescriptor.sut must be a non-empty FQCN "
            "string or {fqcn: ...}"
        )
    patch_sut = sut.get("fqcn") if isinstance(sut, dict) else sut.strip()
    if expected_sut and patch_sut != expected_sut:
        raise BatchResponseError(
            f"{target_id!r} patchDescriptor.sut must be {expected_sut!r}, "
            f"got {patch_sut!r}"
        )

    test_class = patch.get("testClass")
    if not isinstance(test_class, str) or not test_class.strip():
        raise BatchResponseError(
            f"{target_id!r} patchDescriptor.testClass must be a non-empty string"
        )
    if expected_test_class and test_class.strip() != expected_test_class:
        raise BatchResponseError(
            f"{target_id!r} patchDescriptor.testClass must be canonical "
            f"{expected_test_class!r}, got {test_class!r}. Do not create "
            "suffix variants such as *CtorTest."
        )

    methods = patch.get("methods")
    if not isinstance(methods, list) or not methods:
        raise BatchResponseError(
            f"{target_id!r} patchDescriptor.methods must be a non-empty list"
        )

    if expected_allowed_imports is not None:
        allowed = set(expected_allowed_imports)
        patch_imports = patch.get("allowedImports") or []
        if not isinstance(patch_imports, list):
            raise BatchResponseError(
                f"{target_id!r} patchDescriptor.allowedImports must be a list"
            )
        for imp in patch_imports:
            if imp not in allowed:
                raise BatchResponseError(
                    f"{target_id!r} patchDescriptor.allowedImports contains "
                    f"non-whitelisted import {imp!r}. Use only target/failedItem.allowedImports."
                )

        annotations: list[str] = []
        for field in patch.get("fields") or []:
            if isinstance(field, dict) and field.get("annotation"):
                annotations.append(str(field.get("annotation")))
        for method in methods:
            if isinstance(method, dict):
                annotations.extend(str(a) for a in (method.get("annotations") or []))
        for ann in annotations:
            normalized = ann.split("(", 1)[0].strip()
            fqcn = _ANNOTATION_IMPORTS.get(normalized)
            if fqcn and fqcn not in allowed:
                raise BatchResponseError(
                    f"{target_id!r} uses annotation {normalized!r}, which requires "
                    f"non-whitelisted import {fqcn!r}"
                )

    if expected_evidence_ids is not None:
        allowed_evidence = set(expected_evidence_ids)
        required_target_evidence = set(expected_target_evidence_ids or [])
        if target_evidence_required and not required_target_evidence:
            raise BatchResponseError(
                f"{target_id!r} requires target method evidence but targetEvidenceIds is empty. "
                "Skip/fail the target instead of generating code for an unevidenced method."
            )
        for idx, method in enumerate(methods):
            if not isinstance(method, dict):
                continue
            evidence_ids = method.get("evidenceIds")
            if not isinstance(evidence_ids, list) or not evidence_ids:
                raise BatchResponseError(
                    f"{target_id!r} patchDescriptor.methods[{idx}].evidenceIds "
                    "must be a non-empty list from allowedEvidenceIds"
                )
            for evidence_id in evidence_ids:
                if evidence_id not in allowed_evidence:
                    raise BatchResponseError(
                        f"{target_id!r} patchDescriptor.methods[{idx}].evidenceIds "
                        f"contains unknown evidenceId {evidence_id!r}. Use only "
                        "target/failedItem.allowedEvidenceIds."
                    )
            if target_evidence_required and required_target_evidence:
                if not (set(evidence_ids) & required_target_evidence):
                    raise BatchResponseError(
                        f"{target_id!r} patchDescriptor.methods[{idx}].evidenceIds "
                        "must include at least one targetEvidenceIds entry for the "
                        "method under test."
                    )

    for idx, method in enumerate(methods):
        if not isinstance(method, dict):
            raise BatchResponseError(
                f"{target_id!r} patchDescriptor.methods[{idx}] must be an object"
            )
        for key in ("name", "body", "evidenceIds"):
            if key not in method:
                raise BatchResponseError(
                    f"{target_id!r} patchDescriptor.methods[{idx}] missing {key!r}"
                )
        if not isinstance(method.get("name"), str) or not method["name"].strip():
            raise BatchResponseError(
                f"{target_id!r} patchDescriptor.methods[{idx}].name must be a non-empty string"
            )
        if not isinstance(method.get("body"), str):
            raise BatchResponseError(
                f"{target_id!r} patchDescriptor.methods[{idx}].body must be a string"
            )
        if expected_evidence_refs is not None:
            _validate_sut_method_calls(
                method["body"],
                target_id=target_id,
                method_index=idx,
                sut_fqcn=patch_sut,
                allowed_method_names=_allowed_method_names(expected_evidence_refs),
            )
        evidence_ids = method.get("evidenceIds")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise BatchResponseError(
                f"{target_id!r} patchDescriptor.methods[{idx}].evidenceIds "
                "must be a non-empty list"
            )


def validate_generation_envelope(resp: dict, *, batch_id: str) -> list[dict]:
    """Validate ONLY the response envelope (the batch-level contract), returning targets.

    These are the ONLY breaches that may abort the whole batch in the hydration flow:
    a malformed/foreign response wrapper the runner cannot trust at all —
      * ``resp`` is a JSON object,
      * schemaVersion / role / batchId match the request,
      * ``targets`` is a list.

    Everything per-target (unknown/duplicate targetId, status, methods, descriptor) is
    decided by hydrate_generation_response WITHOUT aborting the batch. Keeping the
    envelope separate is what lets a single bad target fail only itself."""
    if not isinstance(resp, dict):
        raise BatchResponseError("response is not a JSON object")
    if resp.get("schemaVersion") != SCHEMA_GENERATION_RESPONSE:
        raise BatchResponseError(
            f"schemaVersion must be {SCHEMA_GENERATION_RESPONSE!r}, got {resp.get('schemaVersion')!r}")
    if resp.get("role") != "generation":
        raise BatchResponseError(f"role must be 'generation', got {resp.get('role')!r}")
    if resp.get("batchId") != batch_id:
        raise BatchResponseError(f"batchId mismatch: expected {batch_id!r}, got {resp.get('batchId')!r}")
    targets = resp.get("targets")
    if not isinstance(targets, list):
        raise BatchResponseError("targets must be a list")
    return targets


def validate_generation_response(resp: dict, batch_targets: list[dict], *, batch_id: str) -> list[dict]:
    """Validate a generation response against the batch it answers (LEGACY/compat path).

    Minimal-schema checks (raise BatchResponseError on a structural breach):
      * schemaVersion / role / batchId match the request,
      * ``items`` is a list,
      * every item names a target that belongs to THIS batch (unknown ⇒ reject),
      * each item.status ∈ {generated, skipped, failed},
      * a ``generated`` item carries a canonical patchDescriptor.

    A per-item ``skipped``/``failed`` is VALID — it must not fail the batch; the
    caller maps it to SKIPPED / GENERATION_FAILED. Returns the validated items.

    NOTE: this is the OLD contract where the LLM ships the patchDescriptor. The
    hydration flow (validate_generation_envelope + hydrate_generation_response) is
    the new path; this function is retained for backward compatibility and reuses
    the same envelope checks so the two never drift."""
    items = validate_generation_envelope(resp, batch_id=batch_id)

    target_by_id = {t.get("targetId"): t for t in batch_targets}
    known = set(target_by_id)
    for it in items:
        if not isinstance(it, dict):
            raise BatchResponseError("each target must be an object")
        tid = it.get("targetId")
        if tid not in known:
            raise BatchResponseError(f"unknown targetId not in this batch: {tid!r}")
        status = it.get("status")
        # NEED_MORE_CONTEXT is a VALID answer (contextPolicy): the model declares it
        # cannot generate from the request alone. It carries no patchDescriptor; the
        # caller maps it to SKIPPED(MISSING_CONTEXT). Never fails the batch.
        if _is_needs_context(status):
            continue
        if status not in _GEN_ITEM_STATUSES:
            raise BatchResponseError(f"invalid target status {status!r} for {tid!r}")
        if status == "generated":
            target = target_by_id.get(tid, {})
            sut = target.get("sut") or None
            _validate_patch_descriptor(
                it.get("patchDescriptor"),
                target_id=tid,
                expected_prefix="patch:",
                expected_sut=sut,
                expected_test_class=_suggested_test_class(sut) if sut else None,
                expected_allowed_imports=target.get("allowedImports"),
                expected_evidence_ids=target.get("allowedEvidenceIds"),
                expected_evidence_refs=target.get("evidenceRefs"),
                expected_target_evidence_ids=target.get("targetEvidenceIds"),
                target_evidence_required=bool(target.get("targetEvidenceRequired")),
            )
    return items


# ── Generation-response hydration (LLM completion → canonical patchDescriptor) ───
# The contract evolved from "the LLM returns a full patchDescriptor" to "the LLM
# returns a MINIMAL completion (status + methods + reason + missingSymbols) and
# Python builds the canonical patchDescriptor from the target". This removes the
# whole class of "patchDescriptor missing required keys" failures (the model used
# to forget schemaVersion/patchId/sut) and lets a single bad ITEM fail ONLY itself
# instead of dragging the whole batch (every valid/skipped/NEED_MORE_CONTEXT
# sibling) into GENERATION_FAILED. Python is the single source of truth for every
# structural field; only the method bodies/evidence come from the model.

# Per-item hydration/validation failure reasons (persisted in validation-result.json).
HYDRATION_COMPLETION_SCHEMA_ERROR = "COMPLETION_SCHEMA_ERROR"
HYDRATION_DESCRIPTOR_ERROR = "PATCH_DESCRIPTOR_HYDRATION_ERROR"
HYDRATION_UNKNOWN_TARGET = "UNKNOWN_TARGET_ID"
HYDRATION_DUPLICATED_TARGET = "DUPLICATED_TARGET_ID"
HYDRATION_MISSING_METHODS = "MISSING_METHODS"
HYDRATION_INVALID_EVIDENCE = "INVALID_EVIDENCE_ID"
HYDRATION_TARGET_EVIDENCE_REQUIRED = "TARGET_EVIDENCE_REQUIRED"
HYDRATION_OMITTED = "OMITTED_FROM_RESPONSE"


def _patch_id_slug(target_id: str) -> str:
    """Deterministic slug for a canonical patchId (``patch:<slug>``)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(target_id or "")).strip("-").lower()
    return slug or "target"


def _completion_methods(item: dict) -> Any:
    """The method completions, accepting both the new and the legacy shapes.

    New format: ``item['methods']``. Legacy/transition format:
    ``item['patchDescriptor']['methods']`` — every OTHER patchDescriptor field the
    LLM may still send (schemaVersion/patchId/sut/testClass/template/allowedImports)
    is deliberately ignored; Python rebuilds them from the target."""
    methods = item.get("methods")
    if methods is None:
        legacy = item.get("patchDescriptor")
        if isinstance(legacy, dict):
            methods = legacy.get("methods")
    return methods


def build_canonical_patch_descriptor(
    target: dict, methods: list, *, patch_id_prefix: str = "patch:"
) -> dict:
    """Construct the canonical patchDescriptor deterministically from the TARGET.

    The LLM never supplies schemaVersion/patchId/sut/testClass/template/allowedImports
    — Python is the single source of truth for all structural metadata; only the
    method names/bodies/evidence come from the model. ``allowedImports`` is the full
    target whitelist (minus wildcard/whole-package entries — see
    _sanitize_allowed_imports) on purpose: the patcher auto-resolves any body-required
    import (test_patch_applier._ensure_required_imports) and prunes the unused ones
    (_prune_unused_imports), so shipping the whole concrete whitelist compiles without
    an unused-import lint hit, and is trivially a subset of target.allowedImports. The
    wildcard filter keeps a non-whitelisted ``pkg.*`` from ever reaching the linter
    (IMPORT_PKG_NOT_WHITELISTED / G6_LINTER_FAIL), which prune cannot strip."""
    sut = target.get("sut") or ""
    test_class = target.get("canonicalTestClass") or (_suggested_test_class(sut) if sut else "")
    test_package = test_class.rsplit(".", 1)[0] if "." in test_class else ""
    norm_methods: list[dict] = []
    for m in methods:
        if not isinstance(m, dict):
            continue
        norm_methods.append({
            "name": m.get("name"),
            "annotations": list(m.get("annotations") or ["@Test"]),
            "body": m.get("body"),
            "evidenceIds": list(m.get("evidenceIds") or []),
        })
    return {
        "schemaVersion": 1,
        "patchId": f"{patch_id_prefix}{_patch_id_slug(target.get('targetId') or sut)}",
        "sut": sut,
        "testClass": test_class,
        "testPackage": test_package,
        "template": target.get("template"),
        "allowedImports": _sanitize_allowed_imports(target.get("allowedImports")),
        "fields": [],
        "methods": norm_methods,
    }


def _classify_descriptor_error(message: str) -> str:
    """Map a _validate_patch_descriptor failure message to a stable per-item reason."""
    low = message.lower()
    if "targetevidenceids" in low or "target method evidence" in low:
        return HYDRATION_TARGET_EVIDENCE_REQUIRED
    if "evidenceid" in low:  # unknown / empty evidenceIds, unevidenced SUT calls
        return HYDRATION_INVALID_EVIDENCE
    if ".methods must be a non-empty list" in low:
        return HYDRATION_MISSING_METHODS
    return HYDRATION_DESCRIPTOR_ERROR


def hydrate_generation_response(request: dict, response: dict) -> dict:
    """Turn an LLM generation completion into apply-ready items + per-item diagnostics.

    Pure function (no I/O). Validates EACH item independently:

      * a structurally broken response (request/response not an object, targets/items
        not a list) raises BatchResponseError — that aborts the batch (caller's job);
      * a single bad ITEM never aborts the batch: it becomes status='failed' with a
        classified reason and a diagnostic entry, so valid/skipped/NEED_MORE_CONTEXT
        siblings are unaffected.

    Returns ``{items, diagnostics, counts}``:
      items        normalized for batch_runner._process_generation (status ∈
                   generated/skipped/failed/NEED_MORE_CONTEXT; a valid 'generated'
                   carries a canonical patchDescriptor). One item per REQUEST target
                   (omitted targets included as failed/OMITTED_FROM_RESPONSE), so the
                   runner needs no separate omission pass.
      diagnostics  ``[{targetId, reason, message}]`` for every failed/omitted/unknown/
                   duplicated item — the audit trail for validation-result.json.
      counts       received/generatedValid/generatedInvalid/skipped/needMoreContext/
                   failed/omitted/duplicated/unknown.
    """
    if not isinstance(request, dict):
        raise BatchResponseError("request is not a JSON object")
    if not isinstance(response, dict):
        raise BatchResponseError("response is not a JSON object")
    targets = request.get("targets")
    if not isinstance(targets, list):
        raise BatchResponseError("request.targets must be a list")
    resp_items = response.get("targets")
    if not isinstance(resp_items, list):
        raise BatchResponseError("response.targets must be a list")

    target_by_id = {t.get("targetId"): t for t in targets if isinstance(t, dict)}
    counts = {"received": 0, "generatedValid": 0, "generatedInvalid": 0,
              "skipped": 0, "needMoreContext": 0, "failed": 0,
              "omitted": 0, "duplicated": 0, "unknown": 0}
    diagnostics: list[dict] = []

    # First pass: index the response by targetId, flagging unknown/duplicate items.
    resp_by_id: dict[Any, dict] = {}
    for raw in resp_items:
        counts["received"] += 1
        if not isinstance(raw, dict):
            diagnostics.append({"targetId": None, "reason": HYDRATION_COMPLETION_SCHEMA_ERROR,
                                "message": "response target is not an object"})
            continue
        tid = raw.get("targetId")
        if tid not in target_by_id:
            counts["unknown"] += 1
            diagnostics.append({"targetId": tid, "reason": HYDRATION_UNKNOWN_TARGET,
                                "message": f"targetId {tid!r} is not in this batch"})
            continue
        if tid in resp_by_id:
            counts["duplicated"] += 1
            diagnostics.append({"targetId": tid, "reason": HYDRATION_DUPLICATED_TARGET,
                                "message": f"targetId {tid!r} appears more than once"})
            continue
        resp_by_id[tid] = raw

    # Second pass: one normalized item per REQUEST target (the source of truth).
    items: list[dict] = []
    for tid, target in target_by_id.items():
        raw = resp_by_id.get(tid)
        if raw is None:
            counts["omitted"] += 1
            diagnostics.append({"targetId": tid, "reason": HYDRATION_OMITTED,
                                "message": "target omitted from the response"})
            items.append({"targetId": tid, "status": "failed",
                          "reason": HYDRATION_OMITTED, "missingSymbols": []})
            continue
        status = raw.get("status")
        reason = raw.get("reason") or ""
        missing = raw.get("missingSymbols") or []
        if _is_needs_context(status):
            counts["needMoreContext"] += 1
            items.append({"targetId": tid, "status": "NEED_MORE_CONTEXT",
                          "reason": reason, "missingSymbols": missing})
            continue
        if status == "skipped":
            counts["skipped"] += 1
            items.append({"targetId": tid, "status": "skipped",
                          "reason": reason, "missingSymbols": missing})
            continue
        if status == "failed":
            counts["failed"] += 1
            items.append({"targetId": tid, "status": "failed",
                          "reason": reason or "model reported failed",
                          "missingSymbols": missing})
            continue
        if status != "generated":
            counts["generatedInvalid"] += 1
            diagnostics.append({"targetId": tid, "reason": HYDRATION_COMPLETION_SCHEMA_ERROR,
                                "message": f"invalid target status {status!r}"})
            items.append({"targetId": tid, "status": "failed",
                          "reason": HYDRATION_COMPLETION_SCHEMA_ERROR, "missingSymbols": []})
            continue

        # status == 'generated' → hydrate + validate THIS item only.
        methods = _completion_methods(raw)
        if not isinstance(methods, list) or not methods:
            counts["generatedInvalid"] += 1
            diagnostics.append({"targetId": tid, "reason": HYDRATION_MISSING_METHODS,
                                "message": "generated target has no methods to hydrate"})
            items.append({"targetId": tid, "status": "failed",
                          "reason": HYDRATION_MISSING_METHODS, "missingSymbols": []})
            continue
        if any(not isinstance(m, dict) for m in methods):
            counts["generatedInvalid"] += 1
            diagnostics.append({"targetId": tid, "reason": HYDRATION_COMPLETION_SCHEMA_ERROR,
                                "message": "each method must be an object"})
            items.append({"targetId": tid, "status": "failed",
                          "reason": HYDRATION_COMPLETION_SCHEMA_ERROR, "missingSymbols": []})
            continue
        patch = build_canonical_patch_descriptor(target, methods)
        sut = target.get("sut") or None
        try:
            _validate_patch_descriptor(
                patch, target_id=tid, expected_prefix="patch:",
                expected_sut=sut,
                expected_test_class=(target.get("canonicalTestClass")
                                     or (_suggested_test_class(sut) if sut else None)),
                expected_allowed_imports=target.get("allowedImports"),
                expected_evidence_ids=target.get("allowedEvidenceIds"),
                expected_evidence_refs=target.get("evidenceRefs"),
                expected_target_evidence_ids=target.get("targetEvidenceIds"),
                target_evidence_required=bool(target.get("targetEvidenceRequired")),
            )
        except BatchResponseError as exc:
            counts["generatedInvalid"] += 1
            why = _classify_descriptor_error(str(exc))
            diagnostics.append({"targetId": tid, "reason": why, "message": str(exc)})
            items.append({"targetId": tid, "status": "failed",
                          "reason": why, "missingSymbols": []})
            continue
        counts["generatedValid"] += 1
        items.append({"targetId": tid, "status": "generated",
                      "patchDescriptor": patch, "reason": "", "missingSymbols": []})

    return {"targets": items, "diagnostics": diagnostics, "counts": counts}


# ── Repair request envelope ──────────────────────────────────────────────────────

def build_repair_request(
    run_id: str,
    batch_id: str,
    repair_round: int,
    failed_items: list[dict],
) -> dict:
    """Build a repair request (schema test-repair-batch-v1) for the FAILED items only.

    ``failed_items`` are pre-shaped dicts: targetId, failureKind, testFile, line,
    errorSummary, buildOutput, currentTestSource, compilerErrorDetails. The
    runner (batch_runner._failed_items_for_repair) MUST populate currentTestSource
    with the exact test that just failed (reconstructed from the rejected patch
    descriptor when the patcher never wrote it to disk) and compilerErrorDetails
    with the verbatim javac/Maven errors for this test. Empty ⇒ caller must not
    request repair (kept caller-side so this stays a pure builder).
    """
    return {
        "_IMPORTANT_WARNING": (
            "ISOLATED ENTITY. Operate ONLY on the information in THIS JSON. "
            "DO NOT read, open, glob, or infer from any repository file, "
            "source code, pom.xml, Git working tree, or path outside this request. "
            "If a needed symbol is absent, respond NEED_MORE_CONTEXT or abandon."
        ),
        "schemaVersion": SCHEMA_REPAIR_REQUEST,
        "runId": run_id,
        "batchId": batch_id,
        "role": "repair",
        "repairRound": repair_round,
        "failedItems": list(failed_items),
        "rules": list(REPAIR_RULES),
        "contextPolicy": dict(CONTEXT_POLICY),
        "missingContextPolicy": {
            "allowedStatus": "NEED_MORE_CONTEXT",
            "rule": "If repairing the test requires a symbol not present in this "
                    "request, answer NEED_MORE_CONTEXT (or abandon the target); never "
                    "invent symbols or read the repository.",
            "responseShape": {"status": "NEED_MORE_CONTEXT", "missingSymbols": [], "reason": ""},
        },
        "selfContainedPolicy": {
            "rule": SELF_CONTAINED_RULE,
            "forbiddenActions": [
                "READ_SOURCE_CODE", "READ_GIT_WORKING_TREE", "READ_POM",
                "READ_JACOCO_XML", "READ_CLASSPATH", "INDEX_REPOSITORY",
            ],
            "authoritativeFields": [
                "failedItem.currentTestSource", "failedItem.compilerErrorDetails",
                "failedItem.patcherErrorDetails",
                "failedItem.allowedImports", "failedItem.evidenceRefs",
            ],
        },
        "importPolicy": {
            "rule": "Each repaired patchDescriptor.allowedImports must be a subset of "
                    "failedItem.allowedImports. Use ONLY concrete FQCNs — never a "
                    "wildcard / whole-package import (e.g. 'jakarta.*', "
                    "'org.springframework.*'); those are rejected as "
                    "IMPORT_PKG_NOT_WHITELISTED (G6_LINTER_FAIL). The runner rebuilds "
                    "schemaVersion/patchId/sut/testClass/testPackage/allowedImports "
                    "from the failed target, so do not invent or omit them; only the "
                    "method bodies need to change.",
            "forbiddenByDefault": list(_COMMON_FORBIDDEN_IMPORTS),
            "noWildcardImports": True,
        },
        "evidencePolicy": {
            "rule": "Each repaired method.evidenceIds must be a non-empty subset of failedItem.allowedEvidenceIds.",
        },
        "testClassPolicy": {
            "canonical": "Use failedItem.canonicalTestClass exactly for patchDescriptor.testClass.",
            "forbidden": [
                "Do not keep previously rejected suffix variants such as *CtorTest.",
                "Do not create *ConstructorTest, *GeneratedTest, or *UnitTest variants.",
            ],
        },
        "expectedResponse": {
            "schemaVersion": SCHEMA_REPAIR_RESPONSE,
            "runId": run_id,
            "batchId": batch_id,
            "role": "repair",
            "repairRound": repair_round,
            "targets": [
                {"targetId": f.get("targetId"),
                 "status": "repaired|skipped|abandoned|failed|NEED_MORE_CONTEXT",
                 "patchDescriptor": {}, "missingSymbols": [], "reason": ""}
                for f in failed_items
            ],
        },
    }


def hydrate_repair_descriptor(
    patch: Any, requested: dict, *, target_id: str, repair_round: int
) -> dict:
    """Backfill the canonical structural metadata of a repaired patchDescriptor.

    Just like generation (build_canonical_patch_descriptor), Python — not the LLM —
    owns schemaVersion / patchId / sut / testClass / testPackage / template /
    allowedImports for a repair. The repair model only contributes methods (and
    optional fields); everything structural is rebuilt from the *requested* failed
    item (the source of truth). This removes the entire class of
    'patchDescriptor missing required keys: [schemaVersion, sut]' repair failures —
    the model can no longer break the round by forgetting a structural key — and
    guarantees the imports shipped to the patcher are concrete, whitelisted FQCNs
    (no wildcard / package import that would trip G6_LINTER_FAIL).
    """
    out = dict(patch) if isinstance(patch, dict) else {}

    # Python owns schemaVersion — never trust/echo the model's value.
    out["schemaVersion"] = 1

    # patchId: synthesize one only when the model omitted it. A PRESENT-but-wrong
    # prefix (e.g. a generation-style 'patch:' id) is left intact so validation
    # still rejects it — that guardrail catches a model echoing the wrong role.
    pid = out.get("patchId")
    if not (isinstance(pid, str) and pid.strip()):
        out["patchId"] = f"repair:{_patch_id_slug(target_id)}:r{repair_round}"

    # sut: copy from the original target when the model omitted it.
    sut = out.get("sut")
    sut_ok = (isinstance(sut, dict) and sut.get("fqcn")) or (
        isinstance(sut, str) and sut.strip()
    )
    if not sut_ok and requested.get("sut"):
        out["sut"] = requested["sut"]

    # testClass / testPackage: canonical class from the failed item.
    canonical = requested.get("canonicalTestClass")
    tc = out.get("testClass")
    if not (isinstance(tc, str) and tc.strip()) and canonical:
        out["testClass"] = canonical
    tc = out.get("testClass")
    if not out.get("testPackage") and isinstance(tc, str) and "." in tc:
        out["testPackage"] = tc.rsplit(".", 1)[0]

    if not out.get("template") and requested.get("template"):
        out["template"] = requested["template"]

    # Imports: ship the full concrete whitelist (applier prunes the unused ones),
    # dropping wildcard / non-whitelisted entries. Ignore whatever the model sent.
    out["allowedImports"] = _sanitize_allowed_imports(
        requested.get("allowedImports"), whitelist=requested.get("allowedImports")
    )
    return out


def validate_repair_response(
    resp: dict,
    requested_ids: set[str],
    *,
    batch_id: str,
    repair_round: int,
    requested_items: list[dict] | None = None,
) -> list[dict]:
    """Validate a repair response. Same contract as generation, with repair statuses.

    For every ``repaired`` item the patchDescriptor is first hydrated
    (hydrate_repair_descriptor) so Python — not the model — supplies schemaVersion /
    patchId / sut / testClass / imports, then validated. The hydrated descriptor is
    written back into the item so the patcher applies the canonical version."""
    if not isinstance(resp, dict):
        raise BatchResponseError("response is not a JSON object")
    if resp.get("schemaVersion") != SCHEMA_REPAIR_RESPONSE:
        raise BatchResponseError(
            f"schemaVersion must be {SCHEMA_REPAIR_RESPONSE!r}, got {resp.get('schemaVersion')!r}")
    if resp.get("role") != "repair":
        raise BatchResponseError(f"role must be 'repair', got {resp.get('role')!r}")
    if resp.get("batchId") != batch_id:
        raise BatchResponseError(f"batchId mismatch: expected {batch_id!r}, got {resp.get('batchId')!r}")
    items = resp.get("targets")
    if not isinstance(items, list):
        raise BatchResponseError("targets must be a list")
    requested_by_id = {i.get("targetId"): i for i in (requested_items or [])}
    for it in items:
        if not isinstance(it, dict):
            raise BatchResponseError("each target must be an object")
        tid = it.get("targetId")
        if tid not in requested_ids:
            raise BatchResponseError(f"repair target not requested: {tid!r}")
        status = it.get("status")
        if _is_needs_context(status):
            continue
        if status not in _REPAIR_ITEM_STATUSES:
            raise BatchResponseError(f"invalid repair status {status!r} for {tid!r}")
        if status == "repaired":
            requested = requested_by_id.get(tid, {})
            # Python owns the structural metadata: rebuild it from the requested
            # failed item so a model that forgot schemaVersion/sut/imports cannot
            # break the round. Write it back so the patcher applies this version.
            patch = hydrate_repair_descriptor(
                it.get("patchDescriptor"), requested,
                target_id=tid, repair_round=repair_round,
            )
            it["patchDescriptor"] = patch
            _validate_patch_descriptor(
                patch,
                target_id=tid,
                expected_prefix="repair:",
                expected_sut=requested.get("sut") or None,
                expected_test_class=requested.get("canonicalTestClass") or None,
                expected_allowed_imports=requested.get("allowedImports"),
                expected_evidence_ids=requested.get("allowedEvidenceIds"),
                expected_evidence_refs=requested.get("evidenceRefs"),
                expected_target_evidence_ids=requested.get("targetEvidenceIds"),
                target_evidence_required=bool(requested.get("targetEvidenceRequired")),
            )
    return items


# ── Manifest + per-target state machine ─────────────────────────────────────────

def new_manifest(run_id: str, repo: str, *, generation_mode: str, batch_size: int, max_repair_rounds: int) -> dict:
    return {
        "schemaVersion": SCHEMA_MANIFEST,
        "runId": run_id,
        "repo": repo,
        "generationMode": generation_mode,
        "batchSize": batch_size,
        "maxRepairRounds": max_repair_rounds,
        "status": "RUNNING",
        "batches": [],
        "targets": {},
        "totals": {},
    }


def ensure_target(
    manifest: dict,
    target_id: str,
    *,
    sut: str = "",
    batch_id: str | None = None,
    method: str | None = None,
) -> dict:
    """Get (creating if needed) the per-target record, defaulting to PENDING."""
    rec = manifest.setdefault("targets", {}).get(target_id)
    if rec is None:
        rec = {"status": PENDING, "sut": sut, "batchId": batch_id, "repairRounds": 0}
        manifest["targets"][target_id] = rec
    if sut and not rec.get("sut"):
        rec["sut"] = sut
    if batch_id and not rec.get("batchId"):
        rec["batchId"] = batch_id
    if method and not rec.get("method"):
        rec["method"] = method
    return rec


def set_status(manifest: dict, target_id: str, status: str, **fields: Any) -> dict:
    """Set a target's status (and optional fields), then recompute totals."""
    rec = ensure_target(manifest, target_id)
    rec["status"] = status
    for k, v in fields.items():
        if v is not None:
            rec[k] = v
    recompute_totals(manifest)
    return rec


def bump_repair_round(manifest: dict, target_id: str) -> int:
    rec = ensure_target(manifest, target_id)
    rec["repairRounds"] = int(rec.get("repairRounds", 0)) + 1
    return rec["repairRounds"]


def should_abandon(manifest: dict, target_id: str, max_repair_rounds: int) -> bool:
    """True once a target has consumed all its repair rounds and is still failing."""
    rec = ensure_target(manifest, target_id)
    return int(rec.get("repairRounds", 0)) >= max_repair_rounds


# How each lifecycle state rolls up into the manifest totals buckets.
_TOTAL_BUCKET = {
    PENDING: "pending", GENERATION_REQUESTED: "pending",
    GENERATED: "generated", APPLIED: "generated",
    REPAIR_REQUESTED: "generated", REPAIRED: "generated",
    PASSED: "passed",
    COMPILE_FAILED: "failed", TEST_FAILED: "failed",
    PATCH_FAILED: "failed", GENERATION_FAILED: "failed",
    SKIPPED: "skipped",
    ABANDONED: "abandoned",
}


def recompute_totals(manifest: dict) -> dict:
    totals = {"pending": 0, "generated": 0, "passed": 0, "failed": 0,
              "skipped": 0, "abandoned": 0}
    for rec in manifest.get("targets", {}).values():
        bucket = _TOTAL_BUCKET.get(rec.get("status"), "pending")
        totals[bucket] += 1
    manifest["totals"] = totals
    return totals


def failing_target_ids(manifest: dict, batch_target_ids: list[str]) -> list[str]:
    """IDs in this batch currently in a repairable failed state, order-preserving."""
    targets = manifest.get("targets", {})
    return [tid for tid in batch_target_ids
            if targets.get(tid, {}).get("status") in REPAIRABLE_STATES]


# ── Between-batches advance decision (the 80% / 50% rules) ───────────────────────

# action ∈ these; the runner reads it to decide what to do after a batch.
ADVANCE_CONTINUE = "continue"          # batch healthy, go to the next batch
ADVANCE_REPAIR_THEN_CONTINUE = "repair-then-continue"
ADVANCE_STOP = "stop"                  # too many failures / global compile error


def advance_decision(passed: int, total: int, *, had_global_compile_error: bool = False,
                     all_need_more_context: bool = False) -> dict:
    """Decide what to do after applying+testing a batch.

    Rules (section 7):
      * global compile error → never advance until a repair round runs.
      * every target answered NEED_MORE_CONTEXT → nothing failed, continue.
      * pass rate ≥ 80%      → repair the failures, then continue.
      * 50% ≤ pass rate < 80% → repair before continuing.
      * pass rate < 50%      → STOP and recommend a smaller --batch-size.
    A 100%-pass batch needs no repair → continue.

    ``all_need_more_context`` is set by the runner when EVERY sendable target ended
    SKIPPED because the model answered NEED_MORE_CONTEXT (so the effective denominator
    collapsed to 0). Those targets did not FAIL — the system cleanly recognised they
    lacked context — so the batch must not trip the < 50% brake and abort unrelated
    later batches. It continues with no repair.
    """
    rate = (passed / total) if total else 1.0
    if had_global_compile_error:
        return {"action": ADVANCE_REPAIR_THEN_CONTINUE, "rate": rate,
                "reason": "global compilation error — repair before advancing"}
    if all_need_more_context and total == 0:
        return {"action": ADVANCE_CONTINUE, "rate": rate,
                "reason": "all targets were need-more-context — skipping batch, continuing"}
    if passed == total:
        return {"action": ADVANCE_CONTINUE, "rate": rate, "reason": "all targets passed"}
    if rate >= 0.80:
        return {"action": ADVANCE_REPAIR_THEN_CONTINUE, "rate": rate,
                "reason": "pass rate ≥ 80% — repair failures, then continue"}
    if rate >= 0.50:
        return {"action": ADVANCE_REPAIR_THEN_CONTINUE, "rate": rate,
                "reason": "pass rate 50–80% — repair before continuing"}
    return {"action": ADVANCE_STOP, "rate": rate,
            "reason": "pass rate < 50% — stopping; consider a smaller --batch-size"}
