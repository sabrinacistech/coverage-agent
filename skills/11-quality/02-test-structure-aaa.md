---
name: test-structure-aaa
description: >
  Aplica este skill al escribir el cuerpo de cualquier test unitario en Java.
  Define la estructura obligatoria Arrange-Act-Assert (AAA) / Given-When-Then,
  y la regla de un único concepto lógico por test.
---

# Skill: Estructura AAA / Given-When-Then en tests unitarios Java

## Objetivo
Que cada test tenga tres secciones claramente delimitadas y verifique
un único concepto lógico, de forma que el fallo sea autoexplicativo.

## Estructura obligatoria

```java
@Test
void should_send_confirmation_email_when_order_is_placed() {
    // Arrange — preparar estado, instancias y comportamiento de mocks
    var emailSender = mock(EmailSender.class);
    var service = new OrderService(emailSender);
    var order = new Order("item-1", 2);

    // Act — una sola llamada al método bajo prueba
    service.place(order);

    // Assert — verificar el resultado o la interacción esperada
    verify(emailSender).send(argThat(email -> email.contains("item-1")));
}
```

## Reglas

### Un concepto lógico por test
Si necesitas escribir un comentario `// and also verify...` dentro del Assert,
ese test se debe partir en dos.

```java
// INCORRECTO — dos conceptos mezclados
@Test
void should_place_order() {
    // ...
    assertThat(result.getStatus()).isEqualTo(CONFIRMED);
    verify(emailSender).send(any());          // segundo concepto
    verify(inventoryService).reserve(any());  // tercer concepto
}

// CORRECTO — cada concepto en su propio test
@Test
void should_return_confirmed_status_when_order_is_placed() { ... }

@Test
void should_send_email_when_order_is_placed() { ... }

@Test
void should_reserve_inventory_when_order_is_placed() { ... }
```

### El Act nunca es compuesto
La sección Act contiene exactamente una línea: la llamada al método bajo prueba.

```java
// INCORRECTO — Act con setup mezclado
var result = service.place(new Order(repo.findFirst().getId(), 1));

// CORRECTO — el objeto se construye en Arrange
var orderId = repo.findFirst().getId();          // Arrange
var order = new Order(orderId, 1);               // Arrange
var result = service.place(order);               // Act
```

### Separar secciones con línea en blanco
Las tres secciones se separan visualmente con una línea en blanco,
sin necesidad de comentarios si los nombres son expresivos.

```java
@Test
void should_throw_when_order_quantity_is_zero() {
    var service = new OrderService(mock(EmailSender.class));
    var invalidOrder = new Order("item-1", 0);

    assertThatThrownBy(() -> service.place(invalidOrder))
        .isInstanceOf(InvalidOrderException.class)
        .hasMessageContaining("quantity");
}
```

### Agrupar escenarios con `@Nested`
Cuando una clase tiene muchos comportamientos, usar `@Nested` para agrupar
los tests por escenario y mantener la sección Arrange compacta.

```java
class OrderServiceTest {

    @Nested
    class WhenOrderIsValid {
        private OrderService service;
        private EmailSender emailSender;

        @BeforeEach
        void setUp() {
            emailSender = mock(EmailSender.class);
            service = new OrderService(emailSender);
        }

        @Test
        void should_return_confirmed_status() { ... }

        @Test
        void should_send_confirmation_email() { ... }
    }

    @Nested
    class WhenOrderIsInvalid {
        @Test
        void should_throw_when_quantity_is_zero() { ... }
    }
}
```

## Checklist

- [ ] ¿El test tiene exactamente tres secciones (Arrange / Act / Assert)?
- [ ] ¿La sección Act tiene una sola línea?
- [ ] ¿El test verifica un único concepto lógico?
- [ ] ¿Las secciones están separadas con línea en blanco?
- [ ] ¿Si hay múltiples escenarios, están agrupados con `@Nested`?
