# Failure Memory

## Objetivo
Evitar reintento de fixes ya fallidos. Implementa el gate **G7**.

## Estructura: `state/failure-memory.json`

```json
{
  "schemaVersion": 1,
  "entries": [
    {
      "hash": "sha256:f3a0...",
      "errorCode": "E_METHOD_UNRESOLVED",
      "symbolFQN": "com.acme.Order#setFoo(java.lang.String)",
      "fixId": "FIX_USE_CONTRACT_METHOD",
      "attempts": 1,
      "lastResult": "FAILED",
      "firstSeenCycle": 2,
      "lastSeenCycle": 3,
      "notes": "method not in contract; setter does not exist"
    }
  ]
}
```

## Reglas
- `hash = sha256(errorCode + "|" + symbolFQN + "|" + fixId)`.
- Antes de aplicar un fix, consultar memoria; si `lastResult == FAILED` ⇒ G7 bloquea.
- Tras éxito de un fix, marcar `lastResult: SUCCESS` (sin bloquear).
- Entradas > 30 días o > 50 ciclos ⇒ expirables solo por instrucción explícita.
- Escritura atómica (tmp + rename) para no corromper en fallos parciales.
