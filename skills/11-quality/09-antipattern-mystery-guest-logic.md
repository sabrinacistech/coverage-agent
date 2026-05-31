---
name: antipattern-mystery-guest-and-logic
description: >
  Detecta y corrige dos antipatrones frecuentes en tests Java:
  Mystery Guest (datos externos sin contexto visible) y lógica condicional
  dentro del test (if/for/switch que hacen el test no confiable).
---

# Antipatrón: Mystery Guest y lógica en tests

---

## Antipatrón 1: Mystery Guest

### Problema
El test referencia datos externos (archivos de fixtures, constantes de otras clases,
variables de clase sin nombre expresivo) sin que sea obvio qué valor tienen
ni por qué son relevantes para el caso que se prueba.

```java
// INCORRECTO — ¿qué es TEST_USER_ID? ¿qué devuelve el fixture?
@Test
void should_return_full_name() {
    var result = service.getFullName(TEST_USER_ID); // constante críptica
    assertThat(result).isEqualTo(EXPECTED_FULL_NAME); // ¿cuál es ese valor?
}

// INCORRECTO — fixture en archivo externo invisible
@Test
void should_calculate_tax() {
    var order = OrderFixtures.STANDARD_ORDER; // ¿qué tiene ese objeto?
    assertThat(service.calculateTax(order)).isEqualTo(15.0);
}
```

### Fix: datos inline con nombres expresivos

```java
// CORRECTO — todo el contexto está en el test
@Test
void should_return_full_name_when_user_exists() {
    var userId = "user-42";
    var user = new User(userId, "Ana", "García");
    when(repository.findById(userId)).thenReturn(Optional.of(user));

    var result = service.getFullName(userId);

    assertThat(result).isEqualTo("Ana García");
}

// CORRECTO — Object Mother para casos complejos (con nombre expresivo)
@Test
void should_calculate_tax_for_standard_domestic_order() {
    var order = OrderMother.standardDomesticOrder(); // nombre describe el caso
    assertThat(service.calculateTax(order)).isEqualTo(15.0);
}

// Object Mother — métodos de fábrica con nombres que describen el caso
class OrderMother {
    static Order standardDomesticOrder() {
        return new Order("item-1", 1, 100.0, Country.DOMESTIC);
    }
    static Order internationalOrderWithTaxExemption() {
        return new Order("item-2", 1, 200.0, Country.INTERNATIONAL, TaxExemption.ACTIVE);
    }
}
```

---

## Antipatrón 2: Lógica condicional en tests

### Problema
Un test con `if`, `for`, `switch` o `try/catch` puede pasar en falso
porque la lógica del test oculta qué caso realmente se verificó.

```java
// INCORRECTO — el if hace que el test pase sin verificar el caso negativo
@Test
void should_process_items() {
    var items = List.of("a", "b", "c");
    for (var item : items) {
        if (service.process(item)) {
            assertThat(item).isNotBlank(); // siempre true
        }
        // si process() devuelve false, no se verifica nada
    }
}

// INCORRECTO — try/catch traga la excepción real
@Test
void should_handle_invalid_input() {
    try {
        service.process(null);
    } catch (Exception e) {
        // ¿qué excepción? ¿cuál mensaje? el test pasa incluso si lanza algo inesperado
    }
}
```

### Fix: un caso por test, sin bifurcaciones

```java
// CORRECTO — cada caso es su propio test
@Test
void should_return_true_when_item_is_valid() {
    assertThat(service.process("a")).isTrue();
}

@Test
void should_return_false_when_item_is_blank() {
    assertThat(service.process("")).isFalse();
}

// CORRECTO — @ParameterizedTest en lugar de for
@ParameterizedTest
@ValueSource(strings = { "a", "b", "c" })
void should_process_each_valid_item(String item) {
    assertThat(service.process(item)).isTrue();
}

// CORRECTO — assertThatThrownBy en lugar de try/catch
@Test
void should_throw_when_item_is_null() {
    assertThatThrownBy(() -> service.process(null))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("item");
}
```

---

## Checklist combinado

- [ ] ¿Los valores de datos están inline o en un Object Mother con nombre expresivo?
- [ ] ¿No hay constantes crípticas sin contexto en el test?
- [ ] ¿No hay `if`, `for`, `switch` o `while` dentro del cuerpo del test?
- [ ] ¿No hay `try/catch` que pueda tragarse excepciones inesperadas?
- [ ] ¿Se usa `assertThatThrownBy` para verificar excepciones?
- [ ] ¿Los bucles se reemplazan con `@ParameterizedTest`?
