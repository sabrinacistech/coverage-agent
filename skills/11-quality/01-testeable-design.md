---
name: testeable-design
description: >
  Aplica este skill antes de escribir cualquier clase Java que vaya a ser testeada.
  Cubre los principios de diseño que hacen que el código sea testeable sin necesidad
  de tests de integración: inyección de dependencias, responsabilidad única,
  preferencia por interfaces y ausencia de lógica estática compleja.
---

# Skill: Diseño de código testeable en Java

## Objetivo
Producir clases Java que puedan testearse de forma unitaria pura, sin levantar
contextos, sin bases de datos y sin dependencias de infraestructura.

## Reglas obligatorias

### 1. Inyección por constructor, nunca `new` interno
```java
// INCORRECTO — acoplamiento duro, imposible de mockear
public class OrderService {
    private final EmailSender emailSender = new SmtpEmailSender();
}

// CORRECTO — dependencia inyectable
public class OrderService {
    private final EmailSender emailSender;

    public OrderService(EmailSender emailSender) {
        this.emailSender = emailSender;
    }
}
```

### 2. Responsabilidad única por clase
Cada clase hace una sola cosa. Si el nombre de la clase contiene "And" o "Manager"
que hace múltiples cosas, dividirla.

```java
// INCORRECTO
public class UserServiceAndEmailNotifier { ... }

// CORRECTO
public class UserService { ... }
public class UserEmailNotifier { ... }
```

### 3. Preferir interfaces sobre implementaciones concretas
Las dependencias se declaran como interfaz, no como clase concreta.
Esto permite sustituirlas con mocks en los tests.

```java
// INCORRECTO
private final SmtpEmailSender emailSender;

// CORRECTO
private final EmailSender emailSender;  // EmailSender es interfaz
```

### 4. Evitar métodos estáticos con lógica de negocio
Los métodos estáticos con lógica no pueden ser mockeados con Mockito estándar.
Extraer la lógica a una instancia inyectable.

```java
// INCORRECTO — no se puede mockear
public class TaxCalculator {
    public static double calculate(double amount) { ... }
}

// CORRECTO
public interface TaxCalculator {
    double calculate(double amount);
}
public class StandardTaxCalculator implements TaxCalculator { ... }
```

### 5. Evitar estado mutable estático
Los campos `static` mutables producen tests acoplados que fallan dependiendo
del orden de ejecución.

```java
// INCORRECTO
public class Config {
    public static String environment = "prod";
}

// CORRECTO — inyectar configuración como dependencia
public class Config {
    private final String environment;
    public Config(String environment) { this.environment = environment; }
}
```

### 6. Visibilidad mínima necesaria
No exponer métodos como `public` solo para poder testearlos.
Si un método necesita ser `public` únicamente para el test, el diseño está mal.

```java
// INCORRECTO — public solo para el test
public String buildInternalQuery(String input) { ... }

// CORRECTO — testear comportamiento visible, no internos
// El método permanece package-private o private
```

## Checklist antes de escribir el test

- [ ] ¿Las dependencias se inyectan por constructor?
- [ ] ¿La clase tiene una sola responsabilidad?
- [ ] ¿Las dependencias son interfaces, no clases concretas?
- [ ] ¿No hay `new` de dependencias dentro de métodos de negocio?
- [ ] ¿No hay campos `static` mutables?
- [ ] ¿No hay lógica en métodos estáticos que necesite ser testeada?
