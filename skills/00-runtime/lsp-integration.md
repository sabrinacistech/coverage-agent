# LSP Integration (Runtime Skill — Phase 8)

## Objetivo
Reutilizar la infraestructura semántica que VS Code ya mantiene (JDT Language Server)
en lugar de re-resolver símbolos por nuestra cuenta.

## Fuentes de verdad LSP

VS Code expone, mediante el JDT.LS:

| Capacidad LSP                         | Reemplaza                                       |
|---------------------------------------|-------------------------------------------------|
| `textDocument/documentSymbol`         | Re-escaneo de clases/métodos por archivo abierto |
| `workspace/symbol`                    | Búsqueda de FQCNs por nombre                    |
| `textDocument/references`             | Cálculo manual de aristas de dependencia        |
| `textDocument/definition`             | Resolución de símbolos (`javap`/SymbolSolver) puntual |
| `textDocument/publishDiagnostics`     | `compile-error-parser` para el archivo abierto  |
| `textDocument/hover`                  | Lectura de firmas / tipos                       |
| `textDocument/codeAction`             | Repair rules ya implementadas por JDT (quick-fixes) |

## Estrategia

1. **Si la sesión es interactiva (VS Code activo)**:
   - Para el archivo en foco, **preferir LSP** sobre el índice cuando hay solapamiento.
   - Para diagnostics del archivo abierto, leer `publishDiagnostics` en vez de
     correr `mvn compile` (cuando JDT ya marcó errores).
   - Usar `textDocument/references` para hidratar `affectedTests` del incremental-map
     en tiempo real al editar.
2. **Si la sesión es batch (CI o full mode)**:
   - El índice `state/index/` es la única fuente; LSP no está disponible.

## Reglas

- LSP **complementa** al índice; no lo reemplaza. El índice sigue siendo
  fuente de verdad determinística para reproducibilidad en CI.
- Cualquier dato derivado de LSP que entre a `state/` debe ser **idempotente**
  y reproducible offline (no congelar handles efímeros del cliente).
- Diagnostics LSP se **cruzan** con `state/compile-error-index.json`; si difieren,
  prevalece el output de `mvn` (autoridad del build tool).
- `textDocument/codeAction` puede sugerir fixes; estos se aplican solo si matchean
  una regla en `repair-rules/*.rules` (no se aplican quick-fixes a ciegas).

## Beneficios

- **Reactividad**: el agente puede actualizar `affectedTests` al guardar un archivo
  sin invocar el pre-stage entero.
- **Tokens**: los símbolos del archivo abierto no se incluyen en prompts; el LLM
  consulta vía referencias indirectas (`evidence-id`).
- **Compile narrowing**: si JDT ya considera al archivo compilable, evitar invocar
  `mvn compile` antes de tests.

## Backward compatibility

- Sin LSP (CI, terminal pura), el sistema funciona idénticamente vía índice.
- El orquestador detecta `process.env.VSCODE_PID` (o equivalente) para activar
  la ruta LSP. Es un fast-path, no un requisito.

## Antipatrones

- Reparsear el archivo abierto cuando JDT ya tiene su AST.
- Correr `mvn compile` full antes de cada test cuando JDT ya reporta verde.
- Aplicar quick-fixes de JDT sin pasar por `repair-rules/*.rules` (rompe G7).
- Depender exclusivamente de LSP (no hay reproducibilidad en CI).
