# Unit Test Generation

## Objetivo
Emitir tests JUnit que compilen y citen evidencia. Cero invención de símbolos.

## Precondiciones
- `state/stack-profile.json` válido (gate G5).
- `state/import-whitelist.json` actualizado (gate G1).
- `state/symbol-contracts/<sut>.json` y contratos de colaboradores presentes (gate G2).
- `state/fixture-catalog.json` poblado para los tipos requeridos.

## Procedimiento
1. Tomar objetivo de `state/batch-plan.json`: `{sut, method, branchId?, mutationId?}`.
2. Resolver firma del método desde el contrato y colaboradores desde `dependency-graph.json`.
3. Seleccionar fixtures de `fixture-catalog.json`. Si falta fixture obligatorio ⇒ abortar el objetivo (no improvisar).
4. Construir test con plantilla AAA:
   - **Arrange**: declarar mocks (`@Mock`), fixtures (`Type.builder()...build()`), inyección (`@InjectMocks` o constructor explícito según `dependency-graph.json`).
   - **Act**: una sola invocación al método objetivo.
   - **Assert**: usar lib del `stack-profile` (`AssertJ` si presente, `JUnit assertions` si no). Incluir asserts de retorno y verificación de interacciones relevantes.
5. Emitir el archivo en la misma estructura de paquete bajo `src/test/java`.
6. Anexar bloque de cita al final del método de test:
   ```java
   // evidence-ids:
   //   sym:com.acme.FooService#calc:e7a1b2c3
   //   ctor:com.acme.FooService:2b3d4e5f
   //   builder:com.acme.Order:a91c2d3e
   ```

## Reglas
- Un test por escenario (happy / branch / exception) → skill [11-quality/08](../11-quality/08-test-coverage-quality.md).
- Nombrado: `should<Behavior>_when<Condition>` o `methodName_condition_expected` → skill [11-quality/03](../11-quality/03-test-naming.md).
- Prohibido `Thread.sleep`, `System.out`, fechas no fijas, aleatorios sin seed → skill [11-quality/11](../11-quality/11-antipattern-eager-sleeping.md).
- Prohibido `@Ignore`/`@Disabled` salvo decisión registrada en `state/batch-plan.json`.
- Sin imports wildcard salvo los del preset emitido por stack-profile.
- Stubs solo para métodos que el SUT realmente invoca según `dependency-graph.json` → skill [11-quality/06](../11-quality/06-test-doubles.md).

## Contrato normativo

Todo test emitido por este procedimiento DEBE pasar
[test-quality-gate.md](test-quality-gate.md), que enforza los 14 checks
derivados de [skills/11-quality/](../11-quality/README.md). Las violaciones
se devuelven al `repair-agent` para resolución determinística vía
[repair-rules/quality.rules](../../repair-rules/quality.rules) o, si no son
reparables, el test se descarta sin compilar.
