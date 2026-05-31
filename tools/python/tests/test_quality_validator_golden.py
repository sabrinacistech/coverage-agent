"""test_quality_validator_golden.py — golden cases for test_quality_validator (F4.R6).

Covers each AST-based check plus the regex passthrough:
  - TQG_12_SWALLOWED      (try/catch swallow)
  - TQG_10_TEST_ORDER_DEP (@TestMethodOrder without justification marker)
  - TQG_02_NO_AAA         (AAA out of order)
  - TQG_12_TAUTOLOGY      (assertNotNull(literal))
  - clean test            (no violations)
  - regex passthrough     (Thread.sleep → TQG_11_NON_DETERMINISTIC via test_linter)

Run: `python tools/python/tests/test_quality_validator_golden.py`
Exits non-zero on any failure.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from test_quality_validator import validate, _HAS_JAVALANG  # noqa: E402

FAILURES: list[str] = []


def _write(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".java", delete=False, encoding="utf-8"
    )
    f.write(content)
    f.close()
    return Path(f.name)


def _kinds(violations: list[dict]) -> set[str]:
    return {v["kind"] for v in violations}


def _assert(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [ OK ] {label}")
    else:
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        FAILURES.append(label)


# ── Cases ────────────────────────────────────────────────────────────────────

def case_swallowed_catch() -> None:
    print("== TQG_12_SWALLOWED ==")
    src = """
package com.foo;
public class FooTest {
    @org.junit.jupiter.api.Test
    void should_swallow_when_called() {
        // given
        // when
        // then
        try { service.call(); } catch (Exception e) { /* nothing */ }
    }
}
"""
    p = _write(src)
    v, err = validate(p, None)
    kinds = _kinds(v)
    _assert("javalang parsed", err is None, str(err))
    _assert("emits TQG_12_SWALLOWED", "TQG_12_SWALLOWED" in kinds, f"got {sorted(kinds)}")


def case_catch_with_assert_ok() -> None:
    print("== catch-with-assert (no SWALLOWED) ==")
    src = """
package com.foo;
public class FooTest {
    @org.junit.jupiter.api.Test
    void should_throw_when_invalid() {
        // given
        // when
        // then
        try { service.call(); } catch (Exception e) {
            org.junit.jupiter.api.Assertions.assertEquals("msg", e.getMessage());
        }
    }
}
"""
    p = _write(src)
    v, err = validate(p, None)
    kinds = _kinds(v)
    _assert("no TQG_12_SWALLOWED on asserted catch", "TQG_12_SWALLOWED" not in kinds,
            f"got {sorted(kinds)}")


def case_test_method_order_unjustified() -> None:
    print("== TQG_10_TEST_ORDER_DEP ==")
    src = """
package com.foo;
@org.junit.jupiter.api.TestMethodOrder(org.junit.jupiter.api.MethodOrderer.OrderAnnotation.class)
public class OrderedTest {
    @org.junit.jupiter.api.Test
    void should_first_when_called() {
        // given
        // when
        // then
        org.junit.jupiter.api.Assertions.assertEquals(1, 1);
    }
}
"""
    p = _write(src)
    v, err = validate(p, None)
    kinds = _kinds(v)
    _assert("javalang parsed", err is None, str(err))
    _assert("emits TQG_10_TEST_ORDER_DEP", "TQG_10_TEST_ORDER_DEP" in kinds,
            f"got {sorted(kinds)}")


def case_test_method_order_justified() -> None:
    print("== @TestMethodOrder + marker (no violation) ==")
    src = """
package com.foo;
// test-order-justified: integration suite requires deterministic order
@org.junit.jupiter.api.TestMethodOrder(org.junit.jupiter.api.MethodOrderer.OrderAnnotation.class)
public class OrderedTest {
    @org.junit.jupiter.api.Test
    void should_first_when_called() {
        // given
        // when
        // then
        org.junit.jupiter.api.Assertions.assertEquals(1, 1);
    }
}
"""
    p = _write(src)
    v, err = validate(p, None)
    kinds = _kinds(v)
    _assert("no TQG_10_TEST_ORDER_DEP with marker",
            "TQG_10_TEST_ORDER_DEP" not in kinds, f"got {sorted(kinds)}")


def case_aaa_out_of_order() -> None:
    print("== TQG_02_NO_AAA (order) ==")
    src = """
package com.foo;
public class FooTest {
    @org.junit.jupiter.api.Test
    void should_do_when_called() {
        // when
        // given
        // then
        org.junit.jupiter.api.Assertions.assertEquals(1, 1);
    }
}
"""
    p = _write(src)
    v, err = validate(p, None)
    # Filter to AAA only — test_linter may also emit other kinds
    aaa = [x for x in v if x["kind"] == "TQG_02_NO_AAA"]
    _assert("emits TQG_02_NO_AAA for out-of-order", len(aaa) >= 1,
            f"got {[x.get('reason') for x in aaa]}")


def case_aaa_in_order() -> None:
    print("== AAA in order (no order violation) ==")
    src = """
package com.foo;
public class FooTest {
    @org.junit.jupiter.api.Test
    void should_do_when_called() {
        // given
        int x = 1;
        // when
        int y = x + 1;
        // then
        org.junit.jupiter.api.Assertions.assertEquals(2, y);
    }
}
"""
    p = _write(src)
    v, err = validate(p, None)
    order_violations = [
        x for x in v
        if x["kind"] == "TQG_02_NO_AAA" and "out of order" in x.get("reason", "")
    ]
    _assert("no AAA-order violation when ordered",
            len(order_violations) == 0, f"got {[x['reason'] for x in order_violations]}")


def case_assertnotnull_tautology() -> None:
    print("== TQG_12_TAUTOLOGY assertNotNull(literal) ==")
    src = """
package com.foo;
public class FooTest {
    @org.junit.jupiter.api.Test
    void should_check_when_called() {
        // given
        // when
        // then
        org.junit.jupiter.api.Assertions.assertNotNull("literal");
        org.junit.jupiter.api.Assertions.assertNotNull(new java.util.ArrayList<String>());
    }
}
"""
    p = _write(src)
    v, err = validate(p, None)
    taut = [x for x in v if x["kind"] == "TQG_12_TAUTOLOGY"]
    _assert("emits TQG_12_TAUTOLOGY for literal", len(taut) >= 1,
            f"got {[x.get('reason') for x in taut]}")
    _assert("emits TQG_12_TAUTOLOGY for new X()", len(taut) >= 2,
            f"got count={len(taut)}")


def case_clean_test() -> None:
    print("== clean test (regex+AST emit zero blockers) ==")
    src = """
package com.foo;
public class FooTest {
    @org.junit.jupiter.api.Test
    void should_returnTrue_when_inputValid() {
        // given
        int x = 1;
        // when
        int y = x + 1;
        // then
        org.junit.jupiter.api.Assertions.assertEquals(2, y);
    }
}
"""
    p = _write(src)
    v, err = validate(p, None)
    _assert("clean test has 0 violations", len(v) == 0,
            f"got {[(x['kind'], x.get('reason')) for x in v]}")


def case_regex_passthrough_thread_sleep() -> None:
    print("== regex passthrough: Thread.sleep ==")
    src = """
package com.foo;
public class FooTest {
    @org.junit.jupiter.api.Test
    void should_wait_when_called() throws Exception {
        // given
        // when
        Thread.sleep(100);
        // then
        org.junit.jupiter.api.Assertions.assertEquals(1, 1);
    }
}
"""
    p = _write(src)
    v, err = validate(p, None)
    _assert(
        "regex check TQG_11_NON_DETERMINISTIC fires through validator",
        any(x["kind"] == "TQG_11_NON_DETERMINISTIC" for x in v),
        f"got {sorted({x['kind'] for x in v})}",
    )


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    print(f"[INFO] javalang available: {_HAS_JAVALANG}")
    case_swallowed_catch()
    case_catch_with_assert_ok()
    case_test_method_order_unjustified()
    case_test_method_order_justified()
    case_aaa_out_of_order()
    case_aaa_in_order()
    case_assertnotnull_tautology()
    case_clean_test()
    case_regex_passthrough_thread_sleep()

    print()
    if FAILURES:
        print(f"[FAIL] {len(FAILURES)} case(s) failed: {FAILURES}")
        return 1
    print("[OK] all cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
