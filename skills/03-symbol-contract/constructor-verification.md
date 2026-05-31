# Constructor Verification

## Objetivo
Registrar constructores reales de cada tipo usado por el test. Soporta gate **G2**.

## Procedimiento (precedencia)
1. **Bytecode**: `javap -p -s <FQCN>` sobre `target/classes` o el jar del classpath.
   - Parsear líneas `public/protected/private <Type>(...)` y `descriptor: (...)V`.
2. **AST** (fallback si no hay `.class`): JavaParser sobre `src/main/java`; resolver tipos con SymbolSolver.
3. Si el tipo es `interface`, `abstract`, `enum`, o `@FreeBuilder` interface ⇒ `instantiation: false` con `reason`.

## Salida (por SUT, dentro de `state/symbol-contracts/<fqcn>.json`)

```json
{
  "constructors": [
    {
      "evidenceId": "ctor:com.acme.Foo:3f1a2b8c",
      "visibility": "public",
      "params": [
        { "type": "java.lang.String", "name": "name" },
        { "type": "int", "name": "size" }
      ],
      "throws": [],
      "source": "bytecode:target/classes/com/acme/Foo.class"
    }
  ],
  "instantiation": {
    "allowed": true,
    "strategy": "constructor",
    "preferred": "ctor:com.acme.Foo:3f1a2b8c"
  }
}
```

## Reglas
- No declarar constructor `()` si no aparece en bytecode/AST.
- Si todos los constructores son `private` ⇒ buscar `static factory` antes de marcar no instanciable.
- Para clases anidadas no estáticas: registrar dependencia del enclosing instance.
- `evidenceId` debe ser determinístico y matchear la gramática canónica
  `ctor:<fqcn>:<hash8>` (sin la firma de parámetros en el string — el `<hash8>`
  desambigua overloads). Patrón: `state/_schemas/symbol-contract.schema.json#/definitions/evidenceId`.
