---
name: test-toolstack
description: >
  Aplica este skill al configurar un proyecto Java para testing unitario puro.
  Cubre el setup de JUnit 5, Mockito, AssertJ y PIT (mutation testing),
  sin dependencias de infraestructura ni tests de integración.
---

# Skill: Stack de herramientas para testing unitario Java

## Dependencias Maven

```xml
<dependencies>
    <!-- JUnit 5 -->
    <dependency>
        <groupId>org.junit.jupiter</groupId>
        <artifactId>junit-jupiter</artifactId>
        <version>5.10.2</version>
        <scope>test</scope>
    </dependency>

    <!-- Mockito -->
    <dependency>
        <groupId>org.mockito</groupId>
        <artifactId>mockito-junit-jupiter</artifactId>
        <version>5.11.0</version>
        <scope>test</scope>
    </dependency>

    <!-- AssertJ -->
    <dependency>
        <groupId>org.assertj</groupId>
        <artifactId>assertj-core</artifactId>
        <version>3.25.3</version>
        <scope>test</scope>
    </dependency>
</dependencies>

<build>
    <plugins>
        <!-- Surefire para JUnit 5 -->
        <plugin>
            <groupId>org.apache.maven.plugins</groupId>
            <artifactId>maven-surefire-plugin</artifactId>
            <version>3.2.5</version>
        </plugin>

        <!-- PIT Mutation Testing -->
        <plugin>
            <groupId>org.pitest</groupId>
            <artifactId>pitest-maven</artifactId>
            <version>1.15.3</version>
            <dependencies>
                <dependency>
                    <groupId>org.pitest</groupId>
                    <artifactId>pitest-junit5-plugin</artifactId>
                    <version>1.2.1</version>
                </dependency>
            </dependencies>
            <configuration>
                <targetClasses>
                    <param>com.miempresa.miproyecto.*</param>
                </targetClasses>
                <targetTests>
                    <param>com.miempresa.miproyecto.*Test</param>
                </targetTests>
                <mutationThreshold>80</mutationThreshold>
            </configuration>
        </plugin>
    </plugins>
</build>
```

## Dependencias Gradle

```groovy
dependencies {
    testImplementation 'org.junit.jupiter:junit-jupiter:5.10.2'
    testImplementation 'org.mockito:mockito-junit-jupiter:5.11.0'
    testImplementation 'org.assertj:assertj-core:3.25.3'
}

test { useJUnitPlatform() }

// PIT
plugins { id 'info.solidsoft.pitest' version '1.15.0' }
pitest {
    junit5PluginVersion = '1.2.1'
    targetClasses = ['com.miempresa.miproyecto.*']
    mutationThreshold = 80
}
```

## Uso de JUnit 5: anotaciones esenciales

```java
@ExtendWith(MockitoExtension.class)  // habilita @Mock y @InjectMocks
class PaymentProcessorTest {

    @Mock
    private PaymentGateway gateway;

    @InjectMocks
    private PaymentProcessor processor;

    @Test
    void should_approve_when_payment_is_valid() { ... }

    @ParameterizedTest
    @CsvSource({ "100.0, APPROVED", "0.0, REJECTED", "-1.0, REJECTED" })
    void should_return_expected_status(double amount, String expectedStatus) { ... }

    @Nested
    class WhenGatewayFails { ... }
}
```

## Uso de Mockito: patrones esenciales

```java
// Stub: definir respuesta
when(gateway.charge(any())).thenReturn(PaymentResult.approved());

// Stub con argumento específico
when(gateway.charge(argThat(p -> p.getAmount() > 0)))
    .thenReturn(PaymentResult.approved());

// Verificar interacción
verify(gateway).charge(argThat(p -> p.getCurrency().equals("USD")));
verify(gateway, never()).refund(any());

// Capturar argumento para assertions más ricas
var captor = ArgumentCaptor.forClass(Payment.class);
verify(gateway).charge(captor.capture());
assertThat(captor.getValue().getAmount()).isEqualTo(99.99);

// Simular excepción
when(gateway.charge(any())).thenThrow(new GatewayTimeoutException());
```

## Uso de AssertJ: assertions esenciales

```java
// Valor simple
assertThat(result.getStatus()).isEqualTo(PaymentStatus.APPROVED);

// Colecciones
assertThat(items).hasSize(3).extracting(Item::getName).contains("book");

// Excepción esperada
assertThatThrownBy(() -> processor.process(null))
    .isInstanceOf(IllegalArgumentException.class)
    .hasMessageContaining("payment");

// Código que NO debe lanzar excepción
assertThatCode(() -> processor.process(validPayment)).doesNotThrowAnyException();

// Objeto con múltiples campos
assertThat(result)
    .satisfies(r -> {
        assertThat(r.getStatus()).isEqualTo(APPROVED);
        assertThat(r.getTransactionId()).isNotBlank();
    });
```

## Ejecutar PIT (mutation testing)

```bash
# Maven
mvn test-compile org.pitest:pitest-maven:mutationCoverage

# Gradle
./gradlew pitest
```

El reporte HTML queda en `target/pit-reports/` o `build/reports/pitest/`.
Revisar los mutantes que **sobrevivieron** — son asserts débiles o código no cubierto.

## Checklist de setup

- [ ] ¿JUnit 5, Mockito y AssertJ están en scope `test`?
- [ ] ¿`maven-surefire-plugin` ≥ 3.x o `useJUnitPlatform()` en Gradle?
- [ ] ¿PIT configurado con `targetClasses` y `mutationThreshold`?
- [ ] ¿Las clases de test están anotadas con `@ExtendWith(MockitoExtension.class)`?
- [ ] ¿No hay `JUnit 4` mezclado con `JUnit 5` en el mismo módulo?
