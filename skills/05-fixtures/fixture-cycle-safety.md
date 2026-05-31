# Fixture Cycle Safety (Skill — Phase 5)

## Objetivo

Detectar y resolver dependencias circulares en la construcción de fixtures antes
de que el LLM intente generarlas, evitando bucles infinitos y tests que no compilan.

## ¿Qué es un ciclo de fixture?

Un ciclo ocurre cuando la construcción de `A` requiere una instancia de `B`,
y la construcción de `B` requiere una instancia de `A`:

```
A(B dep) → B(A dep) → A(B dep) → ...  ∞
```

Esto produce `StackOverflowError` en inicialización o código de test no compilable.

---

## Detección (determinística — no LLM)

### Paso 1: Construir el grafo de fixture

Para cada SUT en el batch, leer `state/dependency-graph.json → classes[fqcn] → injects[]`:

```json
{
  "com.acme.OrderService": { "injects": ["com.acme.PaymentService"] },
  "com.acme.PaymentService": { "injects": ["com.acme.OrderService"] }
}
```

### Paso 2: Detectar ciclos con DFS

```python
# Algoritmo: DFS con coloración (BLANCO/GRIS/NEGRO)
def has_cycle(graph: dict, node: str, visited: set, stack: set) -> bool:
    visited.add(node)
    stack.add(node)
    for neighbor in graph.get(node, {}).get("injects", []):
        if neighbor not in visited:
            if has_cycle(graph, neighbor, visited, stack):
                return True
        elif neighbor in stack:
            return True  # ciclo detectado
    stack.remove(node)
    return False
```

### Paso 3: Marcar en el catálogo

Si se detecta un ciclo, marcar **todos** los nodos involucrados en `fixture-catalog.json`:

```json
{
  "fqcn": "com.acme.OrderService",
  "cycleSafe": false,
  "cycleWith": ["com.acme.PaymentService"],
  "strategy": "mock"
}
```

---

## Resolución de ciclos (en orden de preferencia)

### Opción 1: Mock pasivo para el colaborador cíclico (recomendado)

Si `A` es el SUT y `B` crea el ciclo, usar `Mockito.mock(B.class)` para `B`:

```java
// ✅ CORRECTO — B es el colaborador cíclico, no el SUT
@Mock PaymentService paymentService;  // rompe el ciclo
@InjectMocks OrderService sut;        // SUT real
```

Este es el fallback correcto según la política:
> "Mock pasivo solo como fallback cuando la fixture no puede construirse sin ciclo."

### Opción 2: Inyección manual via constructor

Si el ciclo es resoluble vía lazy-init o setter injection:

```java
// Solo si el contrato documenta setter injection (no constructor injection)
OrderService sut = new OrderService();
sut.setPaymentService(Mockito.mock(PaymentService.class));
```

Verificar que `state/dependency-graph.json → injectStyle == "setter"` antes de usar.

### Opción 3: Builder con ciclo-seguro

Si ambas clases tienen builders (FreeBuilder/Lombok), usar `null` para la dep cíclica:

```java
OrderService.Builder()
    .paymentService(null)  // ← null para la dep cíclica
    .build();
```

Solo si el contrato indica que el campo es `optional: true` o tiene `defaults`.

---

## Registro en fixture-catalog.json

Para cada tipo resuelto, persistir la resolución para evitar re-calcularla:

```json
{
  "fqcn": "com.acme.OrderService",
  "cycleSafe": false,
  "cycleWith": ["com.acme.PaymentService"],
  "strategy": "mock",
  "resolution": "mock_cyclic_dep",
  "code": "@InjectMocks OrderService sut;\n@Mock PaymentService paymentService;",
  "evidenceId": "ctor:com.acme.OrderService:a1b2c3d4"
}
```

---

## Reglas de cobertura con fixtures cíclicas

Cuando un SUT tiene dependencias cíclicas:

1. Los métodos del SUT que **no invocan** la dependencia cíclica → tests normales.
2. Los métodos que **sí invocan** la dependencia cíclica → verificar con `when(...).thenReturn(...)`.
3. Nunca testear el ciclo completo en un unit test; eso es territorio de integration tests.

---

## Antipatrones de ciclos

| Anti-patrón                                           | Problema                                          |
|-------------------------------------------------------|---------------------------------------------------|
| `new OrderService(new PaymentService(new OrderService(...)))` | StackOverflow en construcción del test  |
| `@Spy` sobre el SUT que también es colaborador cíclico| Spy no puede espiar sobre sí mismo               |
| `@SpringBootTest` para resolver un ciclo unit test    | Carga contexto completo, no es unit test          |
| Hardcodear `null` sin registrar en catálogo           | Generación inconsistente entre ciclos             |

---

## Integración con el flujo principal

El Fixture Catalog Agent debe:

1. **Antes** de emitir fixtures al Generation Agent, correr la detección de ciclos.
2. Si hay ciclos → resolver y marcar `cycleSafe: false` en el catálogo.
3. Generation Agent lee el catálogo y usa la estrategia pre-resuelta.
4. El LLM **nunca** ve el grafo de dependencias completo; solo la entrada del catálogo
   para el SUT actual (input quirúrgico — ver `skills/00-runtime/01-context-control.md`).
