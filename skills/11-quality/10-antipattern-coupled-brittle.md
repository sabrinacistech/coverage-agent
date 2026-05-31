---
name: antipattern-coupled-and-brittle-tests
description: >
  Detecta y corrige dos antipatrones que hacen los tests no confiables:
  tests acoplados (dependen del orden de ejecución o estado compartido)
  y tests frágiles (fallan con cambios de implementación, no de contrato).
---

# Antipatrón: Tests acoplados y tests frágiles

---

## Antipatrón 1: Tests acoplados

### Problema
Los tests dependen del estado dejado por otro test o de variables estáticas mutables.
Fallan si se corren en orden distinto o en paralelo.

```java
// INCORRECTO — estado estático compartido entre tests
class UserServiceTest {
    private static UserService service = new UserService(new InMemoryRepo());
    // El mismo service se comparte entre todos los tests

    @Test
    void should_create_user() {
        service.create("Ana");
        assertThat(service.count()).isEqualTo(1);
    }

    @Test
    void should_count_zero_when_no_users() {
        assertThat(service.count()).isEqualTo(0); // FALLA si el test anterior corrió primero
    }
}
```

### Fix: `@BeforeEach` inicializa estado fresco

```java
// CORRECTO — cada test empieza con estado limpio
class UserServiceTest {
    private UserService service;
    private UserRepository repository;

    @BeforeEach
    void setUp() {
        repository = mock(UserRepository.class);
        service = new UserService(repository);
        // Estado completamente nuevo para cada test
    }

    @Test
    void should_create_user() {
        service.create("Ana");
        verify(repository).save(argThat(u -> u.getName().equals("Ana")));
    }

    @Test
    void should_count_zero_when_repository_is_empty() {
        when(repository.count()).thenReturn(0L);
        assertThat(service.count()).isZero();
    }
}
```

### Detectar acoplamiento con ejecución aleatoria

```xml
<!-- Maven Surefire: correr tests en orden aleatorio para detectar acoplamientos -->
<plugin>
    <groupId>org.apache.maven.plugins</groupId>
    <artifactId>maven-surefire-plugin</artifactId>
    <configuration>
        <runOrder>random</runOrder>
    </configuration>
</plugin>
```

---

## Antipatrón 2: Tests frágiles

### Problema
El test verifica cómo se implementa algo internamente, no qué contrato cumple.
Cuando se refactoriza la implementación (sin cambiar el comportamiento),
el test falla aunque el código siga siendo correcto.

```java
// INCORRECTO — over-specification con verify innecesario
@Test
void should_return_user_full_name() {
    when(repository.findById("u-1")).thenReturn(Optional.of(new User("u-1", "Ana", "García")));

    var result = service.getFullName("u-1");

    assertThat(result).isEqualTo("Ana García");
    verify(repository, times(1)).findById("u-1"); // frágil — ¿qué importa cuántas veces?
    verify(repository, never()).findAll();         // frágil — implementación interna
}
```

```java
// INCORRECTO — testear método privado (señal de diseño incorrecto)
@Test
void should_format_name_correctly() throws Exception {
    var method = UserService.class.getDeclaredMethod("formatName", String.class, String.class);
    method.setAccessible(true);
    var result = method.invoke(service, "Ana", "García");
    assertThat(result).isEqualTo("Ana García");
}
```

### Fix: testear comportamiento observable, no implementación

```java
// CORRECTO — verificar el contrato, no los pasos internos
@Test
void should_return_full_name_when_user_exists() {
    when(repository.findById("u-1")).thenReturn(Optional.of(new User("u-1", "Ana", "García")));

    var result = service.getFullName("u-1");

    assertThat(result).isEqualTo("Ana García");
    // No se verifica cómo el service obtiene el usuario internamente
}

// CORRECTO — si el método privado necesita test, extraerlo a clase propia
// En lugar de testear UserService.formatName() privado:
class NameFormatterTest {
    @Test
    void should_format_first_and_last_name() {
        assertThat(new NameFormatter().format("Ana", "García")).isEqualTo("Ana García");
    }
}
```

### Cuándo sí es correcto usar `verify()`

```java
// CORRECTO — verificar que se envió una notificación ES el comportamiento observable
@Test
void should_send_welcome_email_when_user_is_created() {
    service.create("Ana", "ana@email.com");
    verify(emailSender).send(argThat(e -> e.getTo().equals("ana@email.com")));
}

// CORRECTO — verificar que NO ocurrió un efecto secundario no deseado
@Test
void should_not_charge_when_cart_is_empty() {
    service.checkout(emptyCart);
    verify(paymentGateway, never()).charge(any());
}
```

---

## Regla general para `verify()`

Usar `verify()` solo cuando la interacción con el colaborador **es** el comportamiento
observable del sistema (enviar email, publicar evento, escribir en log de auditoría).
No usar `verify()` para confirmar que el SUT hizo las llamadas internas correctas.

---

## Checklist

- [ ] ¿No hay campos `static` mutables en las clases de test?
- [ ] ¿El `@BeforeEach` inicializa todo el estado necesario para cada test?
- [ ] ¿Los tests pasan al correr con `runOrder=random`?
- [ ] ¿Los `verify()` están justificados porque la interacción ES el comportamiento?
- [ ] ¿No hay acceso a métodos privados con reflection?
- [ ] ¿Si falla un test al refactorizar sin cambiar comportamiento, es señal de fragilidad?
