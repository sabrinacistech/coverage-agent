# Mockito Strategy

## Selección de API según `stack-profile.json`

| Versión Mockito | Habilita |
|-----------------|----------|
| 2.x | `@Mock`, `MockitoJUnitRunner` (JUnit 4), `Answers`, sin static mocks |
| 3.x | + `MockitoExtension` (JUnit 5), `lenient()` |
| 4.x / 5.x | + `MockedStatic`, `MockedConstruction` (requiere `mockito-inline` o por defecto en 5.x) |

## Reglas
- Si `stack-profile.test.framework == junit4` ⇒ `@RunWith(MockitoJUnitRunner.Silent.class)` o `MockitoAnnotations.openMocks(this)` en `@Before`.
- Si `junit5` ⇒ `@ExtendWith(MockitoExtension.class)`.
- `@InjectMocks` permitido solo si `dependency-graph.json` confirma constructor injection consistente; en caso contrario, instanciar SUT explícitamente con los mocks.
- Stubs estrictos por defecto; `lenient()` solo cuando hay justificación documentada en `batch-plan.json`.
- `ArgumentCaptor` para validar argumentos no triviales.
- `any()` permitido solo si el tipo es inequívoco; preferir `any(Type.class)` para evitar NPE en autoboxing.
- Para `void`: `doThrow()`, `doNothing()`, `doAnswer()`. Nunca `when(mock.voidMethod()).thenThrow(...)`.

## Prohibiciones
- Mockear tipos del JDK (`String`, `List`, `Map`) salvo justificación explícita.
- Mockear DTOs/value objects con builder verificado disponible (usar fixture real).
- `spy()` sobre el SUT salvo decisión explícita en `batch-plan.json`.
