---
name: test-first-principles
description: >
  Aplica este skill para evaluar y corregir cualquier test unitario Java.
  Define los cinco principios FIRST que garantizan que un test es confiable,
  mantenible y útil: Fast, Isolated, Repeatable, Self-validating, Timely.
---

# Skill: Principios FIRST para tests unitarios

## Objetivo
Que cada test unitario cumpla los cinco principios que lo hacen confiable
y libre de dependencias de entorno o infraestructura.

---

## F — Fast (rápido)

Los tests unitarios deben completarse en **milisegundos**. Un test lento
indica que hay I/O, red, base de datos o `Thread.sleep()` ocultos.

```java
// INCORRECTO — acceso a sistema de archivos
@Test
void should_parse_config() {
    var config = ConfigLoader.load("/etc/app/config.yaml"); // I/O real
    assertThat(config.getEnv()).isEqualTo("prod");
}

// CORRECTO — el contenido se inyecta como String
@Test
void should_parse_config_from_content() {
    var content = "env: prod\ntimeout: 30";
    var config = ConfigParser.parse(content);
    assertThat(config.getEnv()).isEqualTo("prod");
}
```

---

## I — Isolated (aislado)

Cada test es independiente. No depende de que otro test haya corrido antes.
No modifica estado global. Se puede correr solo o en cualquier orden.

```java
// INCORRECTO — estado estático compartido entre tests
class UserServiceTest {
    static List<User> users = new ArrayList<>(); // estado compartido

    @Test
    void should_add_user() { users.add(new User("Ana")); ... }

    @Test
    void should_list_one_user() {
        assertThat(users).hasSize(1); // depende del test anterior
    }
}

// CORRECTO — cada test crea su propio estado
class UserServiceTest {
    private UserService service;

    @BeforeEach
    void setUp() {
        service = new UserService(mock(UserRepository.class));
    }

    @Test
    void should_add_user() { ... }

    @Test
    void should_list_users() { ... }
}
```

---

## R — Repeatable (repetible)

El test produce el mismo resultado en cualquier máquina, en cualquier momento,
sin importar el orden de ejecución o el estado previo.

```java
// INCORRECTO — depende del reloj del sistema
@Test
void should_create_order_with_today_date() {
    var order = service.create(item);
    assertThat(order.getCreatedAt()).isEqualTo(LocalDate.now()); // no determinista
}

// CORRECTO — el reloj se inyecta y se controla en el test
public class OrderService {
    private final Clock clock;
    public OrderService(Clock clock, ...) { this.clock = clock; }
    public Order create(Item item) {
        return new Order(item, LocalDate.now(clock));
    }
}

@Test
void should_create_order_with_current_date() {
    var fixedClock = Clock.fixed(Instant.parse("2024-06-01T00:00:00Z"), ZoneOffset.UTC);
    var service = new OrderService(fixedClock, mock(OrderRepository.class));
    var order = service.create(item);
    assertThat(order.getCreatedAt()).isEqualTo(LocalDate.of(2024, 6, 1));
}
```

---

## S — Self-validating (autoverificable)

El test indica verde o rojo sin necesidad de inspeccionar logs, consola
ni archivos externos. El assert es la única fuente de verdad.

```java
// INCORRECTO — el resultado se imprime, no se verifica
@Test
void should_calculate_discount() {
    double result = calculator.apply(100.0, coupon);
    System.out.println("Result: " + result); // no es una verificación
}

// CORRECTO — el assert verifica automáticamente
@Test
void should_return_20_percent_discount_when_coupon_is_valid() {
    double result = calculator.apply(100.0, validCoupon);
    assertThat(result).isEqualTo(80.0);
}
```

---

## T — Timely (oportuno)

El test se escribe junto con (o antes de) el código que verifica.
Un test escrito meses después tiende a adaptarse al código existente
en lugar de verificar el contrato real.

Práctica recomendada: escribir el test antes o inmediatamente después
de escribir el método. Nunca acumular tests como deuda técnica.

---

## Checklist FIRST

- [ ] **F** — ¿El test corre en < 100ms sin I/O ni red?
- [ ] **I** — ¿El test puede correr solo sin que otro haya corrido antes?
- [ ] **I** — ¿El `@BeforeEach` inicializa todo el estado necesario?
- [ ] **R** — ¿Si hay fechas/UUIDs/random, están controlados por inyección?
- [ ] **S** — ¿Hay al menos un `assertThat` / `verify` que hace fallar el test si algo va mal?
- [ ] **T** — ¿El test se escribió en la misma iteración que el código?
