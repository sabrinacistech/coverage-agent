---
name: test-parameterized
description: >
  Aplica este skill cuando un comportamiento debe verificarse con múltiples
  valores de entrada. Cubre @ParameterizedTest de JUnit 5 con @CsvSource,
  @MethodSource y @EnumSource, incluyendo valores borde y nulos.
---

# Skill: Tests parametrizados en JUnit 5

## Objetivo
Cubrir múltiples casos (happy path, valores borde, nulos, enums) sin duplicar
el cuerpo del test, manteniendo cada caso nombrado y fácil de diagnosticar.

---

## `@CsvSource` — casos simples con valores inline

Usar para casos con pocos parámetros escalares (String, int, double, boolean).

```java
@ParameterizedTest(name = "amount={0} → expected={1}")
@CsvSource({
    "100.0,  80.0",   // descuento del 20%
    "50.0,   40.0",
    "0.0,    0.0",    // valor borde: cero
    "0.01,   0.008",  // valor borde: mínimo positivo
})
void should_apply_20_percent_discount(double amount, double expected) {
    assertThat(calculator.apply(amount)).isCloseTo(expected, within(0.001));
}
```

---

## `@MethodSource` — casos complejos con objetos

Usar cuando los parámetros son objetos, listas o necesitan construcción.

```java
@ParameterizedTest(name = "{0}")
@MethodSource("invalidOrderCases")
void should_throw_when_order_is_invalid(String description, Order order, String expectedMessage) {
    assertThatThrownBy(() -> service.place(order))
        .isInstanceOf(InvalidOrderException.class)
        .hasMessageContaining(expectedMessage);
}

static Stream<Arguments> invalidOrderCases() {
    return Stream.of(
        Arguments.of("null order",       null,                         "order"),
        Arguments.of("zero quantity",    new Order("item-1", 0),       "quantity"),
        Arguments.of("negative amount",  new Order("item-1", -1),      "quantity"),
        Arguments.of("blank item id",    new Order("", 1),             "item"),
        Arguments.of("null item id",     new Order(null, 1),           "item")
    );
}
```

---

## `@EnumSource` — cubrir todos los valores de un enum

```java
@ParameterizedTest(name = "status={0} should not allow cancellation")
@EnumSource(value = OrderStatus.class, names = {"SHIPPED", "DELIVERED", "CANCELLED"})
void should_reject_cancellation_when_order_is_not_cancellable(OrderStatus status) {
    var order = new Order("item-1", 1, status);
    assertThatThrownBy(() -> service.cancel(order))
        .isInstanceOf(OrderNotCancellableException.class);
}
```

---

## `@NullSource` y `@NullAndEmptySource` — cubrir nulos y vacíos

```java
@ParameterizedTest
@NullAndEmptySource
@ValueSource(strings = { "  ", "\t", "\n" })
void should_throw_when_name_is_blank(String name) {
    assertThatThrownBy(() -> new User(name, "user@email.com"))
        .isInstanceOf(IllegalArgumentException.class);
}
```

---

## Convenciones de nombrado en tests parametrizados

El atributo `name` del `@ParameterizedTest` convierte el reporte de fallos
en algo legible:

```java
// Reporte genérico (malo)
@ParameterizedTest
@CsvSource({"0, REJECTED", "100, APPROVED"})
void test(double amount, String status) { ... }
// En CI: "[1] 0, REJECTED" — no explica nada

// Reporte descriptivo (correcto)
@ParameterizedTest(name = "amount={0} → expected status={1}")
@CsvSource({"0.0, REJECTED", "100.0, APPROVED"})
void should_return_expected_status_for_amount(double amount, String status) { ... }
// En CI: "amount=0.0 → expected status=REJECTED"
```

---

## Casos que siempre deben parametrizarse

| Tipo | Ejemplo |
|------|---------|
| Límites numéricos | `0`, `-1`, `MAX_VALUE`, valor mínimo válido |
| Strings inválidos | `null`, `""`, `"   "`, string muy largo |
| Enums completos | Todos los valores del enum para un comportamiento |
| Variantes del mismo cálculo | Múltiples inputs → outputs del mismo algoritmo |

---

## Checklist

- [ ] ¿Los casos nulos y vacíos están cubiertos con `@NullAndEmptySource`?
- [ ] ¿Los casos de enums completos usan `@EnumSource`?
- [ ] ¿El atributo `name` hace que el fallo sea legible en CI?
- [ ] ¿Los casos complejos con objetos usan `@MethodSource` con `Arguments.of`?
- [ ] ¿La fuente de datos del `@MethodSource` es un método `static`?
