# Mutation Agent (modo `mutation-hardening`)

> **STATUS**: standby (mutation-hardening mode opt-in). El agente solo se activa
> cuando el ciclo arranca con `--coverage-mode mutation-hardening` y existe
> plugin PIT en el POM. No participa de los modos `coverage` ni
> `branch-coverage` (que son los caminos por defecto).

## Responsabilidad
Ejecutar PIT, capturar mutantes sobrevivientes y traducirlos en objetivos accionables para generation. Solo se activa en `mode: mutation-hardening`.

## Entradas
- `state/stack-profile.json`
- `state/coverage-summary.json` (cobertura ya alcanzada)
- `pom.xml` con `pitest-maven` (si no existe, el agente lo registra como bloqueo, no lo agrega).

## Procedimiento
1. Verificar plugin: si `org.pitest:pitest-maven` no está, marcar `status: BLOCKED_NO_PIT` y abortar el modo.
2. Ejecutar PIT narrow: `mvn -pl <m> -DtargetClasses=<glob> -DtargetTests=<glob> org.pitest:pitest-maven:mutationCoverage`.
3. Parsear `target/pit-reports/<timestamp>/mutations.xml`.
4. Por cada `<mutation status="SURVIVED">`, registrar: clase, método, línea, mutador, descripción.
5. Agrupar por método; priorizar métodos con `> N` mutantes sobrevivientes.

## Salida: `state/mutation-intelligence.json`

```json
{
  "runId": "pit-2026-05-22T10-00",
  "module": "service-foo",
  "survivors": [
    {
      "class": "com.acme.FooService",
      "method": "calcDiscount(java.math.BigDecimal)",
      "line": 87,
      "mutator": "org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator",
      "description": "changed conditional boundary",
      "suggestedAssertion": "boundary-around-line-87"
    }
  ],
  "blocked": []
}
```

## Reglas
- No agrega dependencias al POM.
- No reescribe tests existentes; solo emite objetivos para planning.
- Si PIT corre > budget, abortar y reportar.
