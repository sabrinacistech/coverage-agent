---
name: test-coverage-quality
description: >
  Aplica este skill para definir y medir la cobertura de tests unitarios en Java
  sin tests de integración. Cubre la estrategia de tres caminos (happy path,
  edge cases, error paths), branch coverage con JaCoCo y mutation testing con PIT.
---

# Skill: Cobertura de calidad en tests unitarios Java

## Objetivo
Conseguir una cobertura que detecte regresiones reales, no simplemente
un porcentaje alto de líneas ejecutadas. Sin tests de integración —
todo con mocks y tests unitarios puros.

---

## Los tres caminos obligatorios

Todo método con lógica debe tener tests para los tres caminos:

```java
// Clase bajo prueba
public class DiscountCalculator {
    public double apply(double amount, Coupon coupon) {
        if (coupon == null) throw new IllegalArgumentException("coupon required");
        if (amount <= 0) return 0.0;
        return amount * (1 - coupon.getRate());
    }
}

// 1. HAPPY PATH — caso normal que debería funcionar
@Test
void should_return_discounted_amount_when_coupon_is_valid() {
    var coupon = new Coupon(0.20);
    assertThat(calculator.apply(100.0, coupon)).isEqualTo(80.0);
}

// 2. EDGE CASES — valores borde
@ParameterizedTest
@CsvSource({ "0.0", "-1.0", "-999.99" })
void should_return_zero_when_amount_is_not_positive(double amount) {
    assertThat(calculator.apply(amount, new Coupon(0.10))).isEqualTo(0.0);
}

// 3. ERROR PATH — excepciones y estados inválidos
@Test
void should_throw_when_coupon_is_null() {
    assertThatThrownBy(() -> calculator.apply(100.0, null))
        .isInstanceOf(IllegalArgumentException.class)
        .hasMessage("coupon required");
}
```

---

## Branch coverage con JaCoCo

La cobertura de ramas (branch coverage) es más valiosa que la de líneas.
Un `if/else` sin cubrir ambas ramas da false security.

```xml
<!-- pom.xml: configurar JaCoCo con umbral de branch coverage -->
<plugin>
    <groupId>org.jacoco</groupId>
    <artifactId>jacoco-maven-plugin</artifactId>
    <version>0.8.11</version>
    <executions>
        <execution>
            <id>prepare-agent</id>
            <goals><goal>prepare-agent</goal></goals>
        </execution>
        <execution>
            <id>check</id>
            <goals><goal>check</goal></goals>
            <configuration>
                <rules>
                    <rule>
                        <limits>
                            <limit>
                                <counter>BRANCH</counter>
                                <value>COVEREDRATIO</value>
                                <minimum>0.80</minimum>
                            </limit>
                        </limits>
                    </rule>
                </rules>
            </configuration>
        </execution>
    </executions>
</plugin>
```

```bash
mvn verify          # genera reporte + aplica umbral
# Reporte en: target/site/jacoco/index.html
```

---

## Mutation testing con PIT

JaCoCo dice si el código se ejecutó. PIT dice si tus asserts detectarían
un bug real. Un mutante que **sobrevive** = assert débil o código innecesario.

```bash
mvn test-compile org.pitest:pitest-maven:mutationCoverage
# Reporte en: target/pit-reports/index.html
```

### Interpretar el reporte de PIT

| Estado del mutante | Significado |
|--------------------|-------------|
| **Killed** | El test detectó el cambio — assert sólido |
| **Survived** | El test pasó con el código mutado — assert débil |
| **No coverage** | Ningún test ejecutó esa línea |

### Ejemplo: mutante que sobrevive revela assert débil

```java
// Código original
public boolean isEligible(int age) {
    return age >= 18;
}

// Test con assert débil
@Test
void should_be_eligible() {
    assertThat(service.isEligible(20)).isTrue(); // pasa con age > 18 también
}

// PIT muta a: return age > 18  →  el test sigue pasando → mutante sobrevive

// Fix: cubrir el límite exacto
@ParameterizedTest
@CsvSource({ "18, true", "17, false", "19, true" })
void should_return_eligibility_for_age(int age, boolean expected) {
    assertThat(service.isEligible(age)).isEqualTo(expected);
}
```

---

## Estrategia de cobertura sin integración

| Capa | Estrategia |
|------|-----------|
| Servicios / casos de uso | Mockear repositorios y clientes externos; cubrir los tres caminos |
| Validators / reglas de negocio | `@ParameterizedTest` exhaustivo con nulos, vacíos y límites |
| Transformadores / mappers | Tests con objetos completamente poblados vs parcialmente nulos |
| Manejo de excepciones | `assertThatThrownBy` para cada rama de error |
| Código con fechas / UUIDs | Inyectar `Clock` / `UUIDGenerator` para control total |

---

## Checklist

- [ ] ¿Hay test para el happy path?
- [ ] ¿Hay test para valores borde (cero, nulo, vacío, máximo)?
- [ ] ¿Hay test para cada rama de error / excepción?
- [ ] ¿JaCoCo tiene umbral de branch coverage ≥ 80%?
- [ ] ¿PIT está configurado y no hay mutantes que sobreviven en lógica crítica?
- [ ] ¿Los asserts verifican el valor exacto, no solo que no lanza excepción?
