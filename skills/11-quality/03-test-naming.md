---
name: test-naming
description: >
  Aplica este skill al nombrar cualquier método de test en Java.
  Define los patrones de nombrado que convierten el nombre del test
  en su propia especificación, de forma que el reporte de fallos sea
  autoexplicativo sin necesidad de abrir el código.
---

# Skill: Naming de tests como especificación

## Objetivo
Que el nombre de cada test describa el comportamiento esperado con suficiente
precisión para que un fallo en CI explique qué contrato se rompió,
sin necesidad de leer el cuerpo del test.

## Patrón principal: `should_[resultado]_when_[condición]`

```java
@Test
void should_return_zero_when_cart_is_empty() { ... }

@Test
void should_throw_invalid_order_exception_when_quantity_is_negative() { ... }

@Test
void should_send_confirmation_email_when_payment_succeeds() { ... }
```

## Patrón alternativo con `@DisplayName` (JUnit 5)

Usar cuando la descripción necesita lenguaje natural más rico o
cuando los tests los leen personas no técnicas.

```java
@Test
@DisplayName("Dado un carrito vacío, cuando se calcula el total, entonces devuelve cero")
void total_of_empty_cart_is_zero() { ... }

@Test
@DisplayName("Cuando la cantidad es negativa, lanza InvalidOrderException con mensaje descriptivo")
void throws_when_quantity_is_negative() { ... }
```

## Reglas

### El nombre describe el contrato, no la implementación
```java
// INCORRECTO — describe qué hace internamente
void calls_repository_save_and_returns_entity() { ... }

// CORRECTO — describe el contrato observable
void should_persist_order_and_return_it_with_assigned_id() { ... }
```

### Sin abreviaciones crípticas
```java
// INCORRECTO
void tstUsrSvcCreate_nullNm() { ... }

// CORRECTO
void should_throw_when_user_name_is_null() { ... }
```

### Los nombres de la clase de test siguen el patrón `[ClaseUnderTest]Test`
```java
// Clase bajo prueba: OrderService
// Clase de test:     OrderServiceTest

// Clase bajo prueba: PaymentProcessor
// Clase de test:     PaymentProcessorTest
```

### Con `@Nested`, el contexto complementa el nombre del test
```java
class OrderServiceTest {

    @Nested
    @DisplayName("When the order is valid")
    class WhenOrderIsValid {

        @Test
        @DisplayName("returns CONFIRMED status")
        void returns_confirmed_status() { ... }
        // Lectura completa: "When the order is valid — returns CONFIRMED status"
    }

    @Nested
    @DisplayName("When the quantity is zero")
    class WhenQuantityIsZero {

        @Test
        @DisplayName("throws InvalidOrderException")
        void throws_invalid_order_exception() { ... }
        // Lectura completa: "When the quantity is zero — throws InvalidOrderException"
    }
}
```

### El nombre del test fallido en el reporte debe ser suficiente
Ante un fallo, preguntarse: ¿con solo leer el nombre del test sé qué
comportamiento se rompió? Si la respuesta es no, renombrar el test.

## Ejemplos completos por tipo de comportamiento

| Tipo | Ejemplo de nombre |
|------|-------------------|
| Valor de retorno | `should_return_discounted_price_when_coupon_is_valid` |
| Excepción lanzada | `should_throw_when_user_email_is_null` |
| Interacción con dependencia | `should_notify_warehouse_when_stock_is_low` |
| Sin efecto (caso negativo) | `should_not_send_email_when_order_is_cancelled` |
| Estado cambiado | `should_mark_order_as_shipped_when_tracking_is_confirmed` |

## Checklist

- [ ] ¿El nombre sigue el patrón `should_X_when_Y` o tiene `@DisplayName` descriptivo?
- [ ] ¿El nombre describe el contrato observable, no la implementación?
- [ ] ¿Si hay `@Nested`, el nombre del método complementa el nombre del contexto?
- [ ] ¿El nombre del fallo en CI es suficiente sin abrir el código?
- [ ] ¿La clase de test se llama `[ClaseUnderTest]Test`?
