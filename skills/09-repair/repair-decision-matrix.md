# Repair Decision Matrix

> **DETERMINISTA — primero Python, después LLM.**
> El matching `errorCode → fixId` se compila en
> [`tools/python/repair_rules_compiler.py`](../../tools/python/repair_rules_compiler.py)
> desde `repair-rules/*.rules`. El driver Python aplica los matches por AST
> ([`tools/python/ast_patcher.py`](../../tools/python/ast_patcher.py)) **antes** de invocar
> al LLM. Solo los errores no cubiertos por reglas escalan al `repair-agent`.
>
> Esta tabla es la **referencia conceptual** del mapping — el código vive en
> los `.rules` y se carga por `repair_dispatch.py`.

## Mapping `errorCode → fix`

| `errorCode` | `fixId` | Acción |
|-------------|---------|--------|
| `E_IMPORT_UNRESOLVED` | `FIX_DROP_IMPORT` | Quitar la línea `import`. Re-correr G1. |
| `E_IMPORT_UNRESOLVED` | `FIX_REPLACE_IMPORT_WHITELIST` | Si hay clase de mismo simple-name en whitelist ⇒ reemplazar FQCN. |
| `E_PACKAGE_UNRESOLVED` | `FIX_DROP_IMPORT` | Igual; marcar paquete prohibido en whitelist temporal. |
| `E_METHOD_UNRESOLVED` | `FIX_USE_CONTRACT_METHOD` | Buscar método en contrato del tipo; reemplazar invocación. Si no existe ⇒ `FIX_DROP_STATEMENT`. |
| `E_CONSTRUCTOR_UNRESOLVED` | `FIX_USE_CONTRACT_CTOR` | Reemplazar por constructor listado. Si solo privados ⇒ `FIX_USE_FACTORY_OR_BUILDER`. |
| `E_INTERFACE_INSTANTIATION` | `FIX_USE_MOCK_OR_BUILDER` | Aplicar `interface-instantiation-rules.md`. |
| `E_TYPE_MISMATCH` | `FIX_ADJUST_FIXTURE_TYPE` | Ajustar fixture al tipo declarado; si no es viable ⇒ descartar test. |
| `E_GENERIC_INFERENCE` | `FIX_EXPLICIT_GENERICS` | Añadir parámetros de tipo explícitos según contrato. |
| `E_VARARGS` | `FIX_CAST_FIRST_VARARG` | Castear el primer argumento. |
| `E_OVERRIDE` | `FIX_REMOVE_OVERRIDE` | Quitar anotación o alinear firma. |
| `E_ACCESS` | `FIX_USE_PUBLIC_API` | Reemplazar por API pública/builder. |

## Compile Error Repair: Invalid Java String Literals

> Determinístico vía [`repair-rules/string-literals.rules`](../../repair-rules/string-literals.rules)
> — no escala al LLM salvo casos residuales.

Si la compilación falla por un literal `String` Java inválido — `unclosed string
literal`, `illegal line end in string literal`, escape malformado, o el guard
previo a la escritura `INVALID_JAVA_STRING_LITERAL` — tratarlo como **bug de
generación, no de la aplicación**:

- **No** tocar código productivo.
- Reemplazar newline/CR/tab/comilla/backslash **crudos** por sus escapes Java
  (`\n`, `\r`, `\t`, `\"`, `\\`) dentro del literal afectado.
- Mantener intacta la intención del test (mismos datos, mismo assert).
- Re-correr el test tras la corrección.

Mensaje accionable que recibe el agente reparador (cuando escala):

```
Generated Java test contains a raw newline inside a normal string literal.
Regenerate the affected Java string using escaped sequences.
Invalid: String value = "a⏎b";   Valid: String value = "a\nb";
```

## Reglas
- Cada intento registra en `state/failure-memory.json` `{hash, errorCode, symbolFQN, fixId, result}`.
- Gate G7: si `hash` ya tiene `result: FAILED`, ese `fixId` queda prohibido para ese símbolo.
- Máximo 2 intentos de reparación por test; al tercer fallo ⇒ descartar y registrar `discardedTests[]`.
- Prohibido `FIX_DROP_TEST_FILE` salvo decisión final tras agotar matriz.
- Nunca aplicar fix que requiera inventar símbolo nuevo.
