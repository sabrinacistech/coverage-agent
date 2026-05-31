# Test Quality Gate

## Objetivo
Rechazar tests de baja señal antes de gastar build/coverage. Cada regla
es el enforcement de un skill normativo en
[skills/11-quality/](../11-quality/README.md). Las violaciones se reportan
con código `TQG_<skill>_<rule>` y nunca se compilan.

## Pipeline

1. `test_linter.py` corre con `--quality-checks` y emite violaciones G6-quality.
2. Cualquier violación con `severity: blocker` → test descartado a `discardedTests[]`
   con `reason: TQG_<skill>`.
3. `repair-agent` consume las violaciones reparables vía
   [repair-rules/quality.rules](../../repair-rules/quality.rules).

## Reglas (cada una bloquea el test)

| # | Regla | Skill normativo | Código TQG |
|---|---|---|---|
| 1 | Debe contener al menos un assert real o `verify(...)` con interacción del SUT. | [11-quality/12](../11-quality/12-antipattern-overmocking-assertfree.md) | `TQG_12_ASSERT_FREE` |
| 2 | Prohibido `assertTrue(true)`, `assertNotNull(obj)` como único assert. | [11-quality/12](../11-quality/12-antipattern-overmocking-assertfree.md) | `TQG_12_TAUTOLOGY` |
| 3 | Prohibido `Thread.sleep`, `Awaitility` sin timeout, `System.currentTimeMillis()`, `Math.random()`. | [11-quality/11](../11-quality/11-antipattern-eager-sleeping.md), [11-quality/05](../11-quality/05-test-first-principles.md) | `TQG_11_NON_DETERMINISTIC` |
| 4 | Prohibida dependencia entre tests (`@TestMethodOrder` solo si justificado y registrado). | [11-quality/10](../11-quality/10-antipattern-coupled-brittle.md) | `TQG_10_TEST_ORDER_DEP` |
| 5 | Prohibidos stubs irrelevantes (`when(mock.x()).thenReturn(...)` sin que el SUT invoque `x`). Verificar contra `dependency-graph.json`. | [11-quality/06](../11-quality/06-test-doubles.md) | `TQG_06_UNUSED_STUB` |
| 6 | Prohibido `verifyNoMoreInteractions` salvo en escenarios negativos explícitos. | [11-quality/10](../11-quality/10-antipattern-coupled-brittle.md) | `TQG_10_OVER_VERIFY` |
| 7 | Cada `try/catch` debe terminar en `fail()` o assert sobre la excepción; nunca silenciar. | [11-quality/12](../11-quality/12-antipattern-overmocking-assertfree.md) | `TQG_12_SWALLOWED` |
| 8 | Cobertura accidental: si el test no toca el método objetivo (verificado por JaCoCo del batch), descartar. | [11-quality/08](../11-quality/08-test-coverage-quality.md) | `TQG_08_NO_TARGET_HIT` |
| 9 | Nombre del método debe matchear `^should[A-Z]\w*_when[A-Z]\w*$` o `^[a-z]\w+_[a-z]\w+_[a-z]\w+$`. | [11-quality/03](../11-quality/03-test-naming.md) | `TQG_03_NAMING` |
| 10 | Body debe contener separadores `// given`, `// when`, `// then` en orden. | [11-quality/02](../11-quality/02-test-structure-aaa.md) | `TQG_02_NO_AAA` |
| 11 | Prohibido mockear el SUT, `String`, `Optional`, value objects ni records. | [11-quality/12](../11-quality/12-antipattern-overmocking-assertfree.md) | `TQG_12_OVER_MOCK` |
| 12 | Prohibido `if/for/while/switch` en el `body` del test. | [11-quality/09](../11-quality/09-antipattern-mystery-guest-logic.md) | `TQG_09_LOGIC_IN_TEST` |
| 13 | Prohibido más de un `// when` (un único concepto por test). | [11-quality/11](../11-quality/11-antipattern-eager-sleeping.md) | `TQG_11_EAGER_TEST` |
| 14 | Estado mutable estático en la clase test → bloquear. | [11-quality/10](../11-quality/10-antipattern-coupled-brittle.md) | `TQG_10_STATIC_STATE` |

## Salida

```json
{
  "discardedTests": [
    {
      "testClass": "com.acme.FooServiceTest",
      "method": "testMethod1",
      "reason": "TQG_03_NAMING",
      "skill": "11-quality/03",
      "evidence": "method name does not match should*_when* or snake_case spec form"
    }
  ]
}
```

Los tests rechazados se registran en `state/discarded-tests.json` para que
`failure-memory.json` evite regenerarlos con la misma forma.
