---
name: antipattern-eager-and-sleeping
description: >
  Detecta y corrige dos antipatrones que degradan la calidad y velocidad
  de los tests Java: Eager Test (un test verifica demasiadas cosas) y
  Sleeping Test (Thread.sleep para sincronizar código asíncrono).
---

# Antipatrón: Eager Test y Sleeping Test

---

## Antipatrón 1: Eager Test

### Problema
Un solo test verifica múltiples comportamientos no relacionados. Cuando falla,
no es claro qué contrato se rompió, y el mensaje de error es ambiguo.

```java
// INCORRECTO — tres conceptos mezclados en un test
@Test
void should_process_order() {
    var order = new Order("item-1", 2, 50.0);

    var result = service.process(order);

    assertThat(result.getStatus()).isEqualTo(CONFIRMED);        // concepto 1
    assertThat(result.getTotalPrice()).isEqualTo(100.0);        // concepto 2
    verify(emailSender).send(any());                            // concepto 3
    verify(inventoryService).reserve("item-1", 2);             // concepto 4
    assertThat(result.getCreatedAt()).isNotNull();              // concepto 5
}
// Si falla: "expected CONFIRMED but was PENDING" — ¿qué parte está rota?
```

### Fix: un concepto por test, agrupados con `@Nested`

```java
// CORRECTO — cada comportamiento observable tiene su propio test
@ExtendWith(MockitoExtension.class)
class OrderServiceTest {

    @Mock private EmailSender emailSender;
    @Mock private InventoryService inventoryService;
    @InjectMocks private OrderService service;

    @Nested
    @DisplayName("When order is valid")
    class WhenOrderIsValid {

        private Order validOrder;
        private OrderResult result;

        @BeforeEach
        void processOrder() {
            validOrder = new Order("item-1", 2, 50.0);
            result = service.process(validOrder);
        }

        @Test
        @DisplayName("returns CONFIRMED status")
        void returns_confirmed_status() {
            assertThat(result.getStatus()).isEqualTo(CONFIRMED);
        }

        @Test
        @DisplayName("calculates total price correctly")
        void calculates_total_price() {
            assertThat(result.getTotalPrice()).isEqualTo(100.0);
        }

        @Test
        @DisplayName("sends confirmation email")
        void sends_confirmation_email() {
            verify(emailSender).send(argThat(e -> e.contains("item-1")));
        }

        @Test
        @DisplayName("reserves inventory")
        void reserves_inventory() {
            verify(inventoryService).reserve("item-1", 2);
        }
    }
}
```

### Cuándo sí es aceptable tener múltiples asserts

Está bien cuando todos los asserts verifican **el mismo concepto** sobre el mismo objeto:

```java
// CORRECTO — múltiples asserts sobre el mismo objeto/concepto
@Test
void should_create_order_with_all_required_fields() {
    var result = service.create("item-1", 2);

    assertThat(result)
        .satisfies(o -> {
            assertThat(o.getId()).isNotBlank();
            assertThat(o.getStatus()).isEqualTo(DRAFT);
            assertThat(o.getCreatedAt()).isNotNull();
        });
}
```

---

## Antipatrón 2: Sleeping Test

### Problema
`Thread.sleep()` se usa para esperar que termine una operación asíncrona.
El test se vuelve lento, no determinista y falla en máquinas lentas de CI.

```java
// INCORRECTO — espera arbitraria
@Test
void should_process_notification_async() throws InterruptedException {
    service.sendAsync(notification);
    Thread.sleep(2000); // ¿y si tarda 2001ms en CI?
    verify(notificationGateway).deliver(any());
}
```

### Fix opción A: Awaitility (código genuinamente asíncrono)

```xml
<dependency>
    <groupId>org.awaitility</groupId>
    <artifactId>awaitility</artifactId>
    <version>4.2.1</version>
    <scope>test</scope>
</dependency>
```

```java
// CORRECTO — espera hasta que la condición se cumpla, con timeout máximo
@Test
void should_deliver_notification_async() {
    service.sendAsync(notification);

    await()
        .atMost(Duration.ofSeconds(3))
        .untilAsserted(() -> verify(notificationGateway).deliver(any()));
}
```

### Fix opción B: hacer la unidad bajo prueba síncrona en el test

Esta es la solución preferida cuando el diseño lo permite. Inyectar el
`Executor` como dependencia y reemplazarlo con un executor síncrono en tests.

```java
// Clase bajo prueba
public class NotificationService {
    private final Executor executor;
    private final NotificationGateway gateway;

    public NotificationService(Executor executor, NotificationGateway gateway) {
        this.executor = executor;
        this.gateway = gateway;
    }

    public void sendAsync(Notification notification) {
        executor.execute(() -> gateway.deliver(notification));
    }
}

// En producción: Executors.newCachedThreadPool()
// En test: Runnable::run (síncrono, ejecuta en el mismo hilo)
@Test
void should_deliver_notification() {
    var gateway = mock(NotificationGateway.class);
    var service = new NotificationService(Runnable::run, gateway); // síncrono

    service.sendAsync(notification);

    verify(gateway).deliver(notification); // se ejecuta síncronamente
}
```

---

## Checklist combinado

- [ ] ¿Cada test verifica un único concepto lógico?
- [ ] ¿Los tests relacionados están agrupados con `@Nested`?
- [ ] ¿No hay `Thread.sleep()` en ningún test?
- [ ] ¿Si hay código asíncrono, el `Executor` se inyecta como dependencia?
- [ ] ¿Si es imprescindible async, se usa Awaitility en lugar de sleep?
