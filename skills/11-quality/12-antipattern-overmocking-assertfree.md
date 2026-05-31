---
name: antipattern-overmocking-assertfree
description: >
  Detecta y corrige dos antipatrones que producen tests que no detectan bugs:
  Over-mocking (mockear hasta la clase bajo prueba) y Assert-free Test
  (tests que pasan sin verificar ningún comportamiento útil).
---

# Antipatrón: Over-mocking y Assert-free Test

---

## Antipatrón 1: Over-mocking

### Problema
Se mockean tantas cosas que el test ya no verifica nada real. En el extremo,
se mockea la propia clase bajo prueba, lo que la hace completamente inútil.

```java
// INCORRECTO — mockear la clase bajo prueba
@Test
void should_calculate_discount() {
    var calculator = mock(DiscountCalculator.class);  // mockeando lo que se prueba
    when(calculator.apply(100.0, coupon)).thenReturn(80.0);

    var result = calculator.apply(100.0, coupon);

    assertThat(result).isEqualTo(80.0);
    // Solo verifica que Mockito funciona, no que DiscountCalculator funciona
}
```

```java
// INCORRECTO — mockear dependencias transitivas (dependencias de dependencias)
@Test
void should_process_payment() {
    var connection = mock(DatabaseConnection.class);
    var repo = mock(OrderRepository.class);
    var dao = mock(OrderDao.class);
    // Se mockea toda la cadena — ¿qué se está probando?
    when(repo.getDao()).thenReturn(dao);
    when(dao.getConnection()).thenReturn(connection);
    // ...
}
```

```java
// INCORRECTO — mockear clases de valor o utilidades simples
var coupon = mock(Coupon.class);          // Coupon es un simple value object
when(coupon.getRate()).thenReturn(0.20);  // innecesario, usar new Coupon(0.20)
```

### Fix: mockear solo dependencias externas directas

```java
// CORRECTO — instanciar la clase real, mockear solo sus dependencias directas
@Test
void should_apply_discount_to_amount() {
    var calculator = new DiscountCalculator();     // clase real
    var coupon = new Coupon(0.20);                 // value object real

    var result = calculator.apply(100.0, coupon);

    assertThat(result).isEqualTo(80.0);
}

// CORRECTO — mockear solo la dependencia externa directa
@Test
void should_save_order_and_return_with_id() {
    var repository = mock(OrderRepository.class);
    when(repository.save(any())).thenAnswer(inv -> {
        var order = inv.getArgument(0, Order.class);
        return order.withId("order-" + UUID.randomUUID());
    });
    var service = new OrderService(repository);  // clase real

    var result = service.create("item-1", 2);

    assertThat(result.getId()).startsWith("order-");
}
```

### Señales de over-mocking

- La clase de test tiene más `mock()` que líneas de lógica real
- El test pasa aunque se cambie completamente la implementación
- Se mockea una clase que no tiene dependencias de infraestructura
- Se usa `mock()` sobre clases del JDK (`String`, `List`, `Optional`)

---

## Antipatrón 2: Assert-free Test

### Problema
El test pasa siempre porque no hay ningún assert que pueda fallar.
Las variantes más comunes son el `try/catch` vacío y el test que solo
verifica que no se lanza excepción sin verificar el resultado.

```java
// INCORRECTO — try/catch vacío traga la excepción
@Test
void should_handle_null_input() {
    try {
        service.process(null);
    } catch (Exception e) {
        // silencioso — el test pasa aunque se lance NullPointerException
    }
}
```

```java
// INCORRECTO — el test pasa aunque el método devuelva basura
@Test
void should_parse_config() {
    assertThatCode(() -> ConfigParser.parse(validYaml)).doesNotThrowAnyException();
    // Solo verifica que no lanza — no verifica que parsea correctamente
}
```

```java
// INCORRECTO — assert siempre verdadero
@Test
void should_return_non_null_result() {
    var result = service.process(input);
    assertThat(result).isNotNull(); // pasa incluso si result es un objeto vacío inútil
}
```

### Fix: AssertJ para verificaciones precisas

```java
// CORRECTO — assertThatThrownBy verifica tipo Y mensaje
@Test
void should_throw_when_input_is_null() {
    assertThatThrownBy(() -> service.process(null))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessageContaining("input");
}

// CORRECTO — verificar el resultado concreto, no solo que no lanza
@Test
void should_parse_environment_from_valid_config() {
    var config = ConfigParser.parse("env: prod\ntimeout: 30");
    assertThat(config.getEnv()).isEqualTo("prod");
    assertThat(config.getTimeout()).isEqualTo(30);
}

// CORRECTO — verificar estructura y valores del objeto devuelto
@Test
void should_create_order_with_confirmed_status() {
    var result = service.create("item-1", 2);
    assertThat(result)
        .isNotNull()
        .satisfies(o -> {
            assertThat(o.getStatus()).isEqualTo(CONFIRMED);
            assertThat(o.getItemId()).isEqualTo("item-1");
            assertThat(o.getQuantity()).isEqualTo(2);
        });
}
```

### Detectar tests sin valor real con PIT

Un test assert-free produce mutantes que **sobreviven** en PIT porque ningún
cambio al código hace fallar el test. Si PIT muestra alto % de mutantes
sobrevividos en una clase, revisar si los tests tienen asserts reales.

---

## Checklist combinado

- [ ] ¿No se mockea la clase bajo prueba (el SUT)?
- [ ] ¿No se mockean value objects ni clases del JDK?
- [ ] ¿No se mockean dependencias transitivas?
- [ ] ¿No hay `try/catch` vacíos o que solo loguean?
- [ ] ¿Cada test tiene al menos un `assertThat` o `verify` que puede fallar?
- [ ] ¿Los asserts verifican valores concretos, no solo `isNotNull()`?
- [ ] ¿PIT no muestra mutantes sobrevividos en la lógica principal?
