# Assert Strategy

Selección **determinística** del dialecto de aserción a partir de
`contextPack.stack.assertFramework` (`stk[3]` en el compact pack). Igual que
[`mockito-strategy.md`](mockito-strategy.md) para los mocks: el LLM **no elige**
la librería de asserts — la dicta el stack del proyecto. Mezclar dialectos
produce `cannot find symbol` (p. ej. `Assertions.assertEquals(...)` sin
`import org.junit.jupiter.api.Assertions;`).

## Un solo dialecto por `assertFramework`

| `assertFramework` | API de igualdad/estado | Import (estático salvo el tipo) |
|---|---|---|
| `assertj` | `assertThat(actual).isEqualTo(...)`, `.isNotNull()`, `.contains(...)` | `import static org.assertj.core.api.Assertions.assertThat;` |
| `hamcrest` | `assertThat(actual, is(...))`, `assertThat(x, notNullValue())` | `import static org.hamcrest.MatcherAssert.assertThat;` + `import static org.hamcrest.Matchers.*;` |
| `junit-builtin` | `assertEquals(exp, act)`, `assertTrue(...)`, `assertNotNull(...)` | `import static org.junit.jupiter.api.Assertions.assertEquals;` (uno por helper) |

Reglas:

- **No mezclar.** Si `assertFramework == assertj` no usar `assertEquals`/`assertTrue`
  de JUnit; usar `assertThat(...)`. Si `assertFramework == junit-builtin` no usar
  `assertThat` de AssertJ.
- Si usás la forma **cualificada** `Assertions.assertX(...)` (en vez del import
  estático), `Assertions` es un **tipo** y requiere `import org.junit.jupiter.api.Assertions;`.
  Preferir el import estático para evitar el olvido.
- `assertFramework` ausente / `none` / `unknown` ⇒ **default `assertj`** (es lo que
  trae `spring-boot-starter-test`, el caso más común).

## Excepciones (independiente del dialecto)

La aserción de excepciones sigue `stack.testFramework`, **no** `assertFramework`:

- JUnit 5: `assertThrows(Tipo.class, () -> sut.metodo(args))`
  → `import static org.junit.jupiter.api.Assertions.assertThrows;`
  (en proyectos AssertJ también vale `assertThatThrownBy(() -> ...).isInstanceOf(Tipo.class)`).
- JUnit 4: `@Test(expected = Tipo.class)` o el patrón try/fail.

## Garantía determinística (no depende del LLM)

El patcher resuelve e **inyecta** el import faltante de cualquier símbolo de
aserción usado (`test_patch_applier._ensure_required_imports`, vía
`framework_imports`), y el linter **bloquea** pre-Maven un símbolo usado sin su
import (`test_linter.check_g1_reverse`, reverse-G1). Esta skill alinea el output
del LLM con esa garantía para minimizar reparaciones.
