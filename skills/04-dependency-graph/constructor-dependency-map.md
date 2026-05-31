# Constructor Dependency Map

## Objetivo
Mapear inyecciones reales por SUT en `state/dependency-graph.json`.

## Procedimiento
1. AST del SUT con SymbolSolver.
2. Detectar:
   - Constructor injection: un único constructor público o anotado `@Autowired`/`@Inject` ⇒ todos sus parámetros son dependencias.
   - Field injection: campos `@Autowired`/`@Inject` (registrar como `injection: field`).
   - Setter injection: setters `@Autowired`/`@Inject`.
3. Para cada dependencia: registrar `name`, `type` (FQCN), `injection`, y si es `final`.

## Salida (extracto)

```json
{
  "sut": "com.acme.FooService",
  "dependencies": [
    { "name": "orderRepo", "type": "com.acme.OrderRepository", "injection": "constructor", "final": true },
    { "name": "clock", "type": "java.time.Clock", "injection": "constructor", "final": true }
  ],
  "instantiationHint": "new FooService(orderRepo, clock)"
}
```

## Reglas
- Si hay varios constructores y ninguno anotado, preferir el de mayor aridad anotado por Lombok `@RequiredArgsConstructor` (solo si Lombok detectado).
- Nunca usar `@InjectMocks` si la inyección es mixta (constructor + field); instanciar SUT explícito.
