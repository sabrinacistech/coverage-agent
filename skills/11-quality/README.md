# 11-quality — Java Test Quality Standards

Estándares normativos de calidad para todo test generado por el pipeline.
Cada archivo define una regla auditable; el conjunto es el contrato que
[test-quality-gate.md](../07-generation/test-quality-gate.md) enforza y
[test_linter.py](../../tools/python/test_linter.py) valida pre-compile.

## Estructura

```
skills/11-quality/
├── README.md                                   ← este archivo
│
├── Fundamentos
│   ├── 01-testeable-design.md                  ← código que se puede testear
│   ├── 02-test-structure-aaa.md                ← patrón AAA / Given-When-Then
│   ├── 03-test-naming.md                       ← nombres que son especificaciones
│   └── 04-test-toolstack.md                    ← JUnit 5 + Mockito + AssertJ + PIT
│
├── Buenas prácticas
│   ├── 05-test-first-principles.md             ← Fast, Isolated, Repeatable, Self-validating
│   ├── 06-test-doubles.md                      ← Stub vs Mock vs Fake vs Spy
│   ├── 07-test-parameterized.md                ← @ParameterizedTest, edge cases, nulos
│   └── 08-test-coverage-quality.md             ← branch coverage + mutation testing
│
└── Antipatrones
    ├── 09-antipattern-mystery-guest-logic.md   ← datos ocultos + lógica en tests
    ├── 10-antipattern-coupled-brittle.md       ← tests acoplados + tests frágiles
    ├── 11-antipattern-eager-sleeping.md        ← eager test + Thread.sleep
    └── 12-antipattern-overmocking-assertfree.md ← over-mocking + tests sin asserts
```

## Stack de herramientas

| Herramienta | Versión | Propósito |
|-------------|---------|-----------|
| JUnit 5 (Jupiter) | 5.10.x | Framework de tests |
| Mockito | 5.11.x | Test doubles (mock, stub, spy) |
| AssertJ | 3.25.x | Assertions fluidas y legibles |
| JaCoCo | 0.8.x | Cobertura de ramas (branch coverage) |
| PIT | 1.15.x | Mutation testing |
| Awaitility | 4.2.x | Tests asíncronos sin Thread.sleep |

## Orden de adopción recomendado

1. Empezar por `01-testeable-design.md` — si el diseño no lo permite, nada más funciona.
2. Establecer `02-test-structure-aaa.md` y `03-test-naming.md` como estándar de equipo.
3. Configurar el stack con `04-test-toolstack.md`.
4. Revisar los antipatrones (`09` al `12`) en el código existente.
5. Agregar `08-test-coverage-quality.md` (JaCoCo + PIT) al pipeline de CI.

## Principio guía

> Testear comportamiento observable, no implementación interna.
> Un test que falla al refactorizar sin cambiar el contrato es un test mal escrito.

## Integración con el pipeline

| Consumidor | Cómo aplica los skills |
|---|---|
| [test-intent-agent](../../agents/test-intent-agent.md) | Un concepto por test (11), parametrización (07), naming desde el escenario (03). |
| [test-body-agent](../../agents/test-body-agent.md) | Estructura AAA (02), naming (03), test doubles (06), anti-mystery-guest (09), anti-overmocking (12). |
| [test-quality-gate](../07-generation/test-quality-gate.md) | Mapea cada regla del gate a su skill `11-quality/NN` correspondiente. |
| [test_linter.py](../../tools/python/test_linter.py) | Checks G6-quality estáticos: naming regex, `Thread.sleep`, `assertTrue(true)`, mock del SUT. |
| [repair-rules/quality.rules](../../repair-rules/quality.rules) | Reparaciones determinísticas de violaciones detectadas por el linter. |
