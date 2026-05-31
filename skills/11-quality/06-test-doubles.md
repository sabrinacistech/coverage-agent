---
name: test-doubles
description: >
  Aplica este skill al decidir qué tipo de objeto ficticio usar en un test unitario Java.
  Cubre la diferencia práctica entre Stub, Mock, Fake y Spy con Mockito,
  y las reglas para no mezclarlos incorrectamente.
---

# Skill: Test doubles — Stub, Mock, Fake y Spy

## Objetivo
Usar el tipo de doble correcto según el propósito del test, evitando
over-mocking y tests frágiles por verificaciones innecesarias.

---

## Mapa de decisión

| Tipo | Propósito | Cuándo usarlo |
|------|-----------|---------------|
| **Stub** | Devolver datos controlados | La dependencia provee datos que el SUT necesita |
| **Mock** | Verificar que se llamó a algo | El test verifica una interacción (efecto secundario) |
| **Fake** | Implementación simplificada real | Cuando el stub se vuelve complejo de configurar |
| **Spy** | Wrapper sobre la instancia real | Solo cuando no puedes refactorizar y necesitas verificar parcialmente |

---

## Stub — controlar datos de entrada

```java
// El test necesita que el repositorio devuelva un usuario específico
@Test
void should_return_full_name_when_user_exists() {
    var repository = mock(UserRepository.class);
    when(repository.findById("u-1")).thenReturn(Optional.of(new User("u-1", "Ana", "García")));

    var service = new UserService(repository);
    var fullName = service.getFullName("u-1");

    assertThat(fullName).isEqualTo("Ana García");
    // No se verifica cuántas veces se llamó al repository — es un stub, no un mock
}
```

---

## Mock — verificar interacciones

```java
// El test verifica que se envió una notificación (efecto secundario)
@Test
void should_notify_admin_when_user_is_blocked() {
    var notifier = mock(AdminNotifier.class);
    var service = new UserService(mock(UserRepository.class), notifier);

    service.block("u-1");

    verify(notifier).notifyBlock(argThat(n -> n.getUserId().equals("u-1")));
}
```

**Regla:** usar `verify()` solo cuando la interacción es el comportamiento
observable del test. No agregar `verify()` por defecto a todos los mocks.

---

## Fake — implementación simplificada

Útil cuando el stub requiere demasiada configuración o cuando la lógica
de retorno depende de los argumentos recibidos.

```java
// Fake de repositorio con mapa en memoria
class InMemoryUserRepository implements UserRepository {
    private final Map<String, User> store = new HashMap<>();

    @Override
    public Optional<User> findById(String id) {
        return Optional.ofNullable(store.get(id));
    }

    @Override
    public User save(User user) {
        store.put(user.getId(), user);
        return user;
    }
}

@Test
void should_find_saved_user() {
    var repo = new InMemoryUserRepository();
    var service = new UserService(repo);

    service.create("u-1", "Ana");
    var found = service.getFullName("u-1");

    assertThat(found).isEqualTo("Ana");
}
```

---

## Spy — usar con precaución

El `@Spy` envuelve una instancia real. Usar solo cuando no es posible
refactorizar y se necesita verificar parte del comportamiento de la clase real.

```java
// Caso excepcional: verificar un método de la propia clase bajo prueba
// es señal de diseño incorrecto — considerar refactorizar primero

@Spy
private OrderService orderService = new OrderService(mock(EmailSender.class));

@Test
void should_call_validate_before_placing() {
    doNothing().when(orderService).validate(any());
    orderService.place(order);
    verify(orderService).validate(order);
}
```

> **Aviso:** necesitar un `@Spy` sobre la clase bajo prueba casi siempre
> indica que la clase tiene demasiadas responsabilidades. Evaluar si se
> puede extraer la lógica de validación a una clase separada.

---

## Reglas de uso

### No agregar `verify()` a stubs
```java
// INCORRECTO — el repository es un stub, verificarlo lo convierte en mock innecesario
when(repository.findById("u-1")).thenReturn(Optional.of(user));
// ... lógica del test ...
verify(repository).findById("u-1"); // frágil e innecesario

// CORRECTO — solo verificar el resultado
assertThat(result.getFullName()).isEqualTo("Ana García");
```

### No mockear la clase bajo prueba
```java
// INCORRECTO
var service = mock(OrderService.class); // estás mockeando lo que quieres probar
when(service.place(any())).thenCallRealMethod();

// CORRECTO — instanciar la clase real con sus dependencias mockeadas
var service = new OrderService(mock(EmailSender.class), mock(OrderRepository.class));
```

### Mockear solo las dependencias directas
```java
// INCORRECTO — mockear dependencias de las dependencias (Law of Demeter)
when(gateway.getClient().connect()).thenReturn(connection);

// CORRECTO — la dependencia directa devuelve lo que el SUT necesita
when(gateway.isAvailable()).thenReturn(true);
```

## Checklist

- [ ] ¿Usas stub cuando solo necesitas datos de entrada?
- [ ] ¿Usas mock + `verify()` solo cuando la interacción es el comportamiento a probar?
- [ ] ¿Consideraste un Fake antes de configurar un stub muy complejo?
- [ ] ¿Evitas `verify()` en stubs?
- [ ] ¿No estás mockeando la clase bajo prueba?
- [ ] ¿No mockeas dependencias transitivas (dependencias de dependencias)?
