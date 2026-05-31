# Canonical Prohibitions — single source of truth

> Este documento es la **fuente canónica** de las prohibiciones G1-G9 y los
> patrones correctivos. `MASTER_PROMPT.md`, `.github/copilot-instructions.md`,
> los agentes (`agents/*.md`) y los skills **deben referenciar este archivo**
> en lugar de re-declarar las reglas. Cualquier divergencia es bug.

## Gates anti-alucinación (resumen)

| Gate | Qué valida | Bloqueado por |
|------|------------|---------------|
| G1   | Import ∈ `state/import-whitelist.json` | `gate_runner.py` (`gate_g1`) + `test_patch_applier.py` + `test_linter.py` |
| G2   | Cada `methods[].evidenceIds` del patch existe en `state/symbol-contracts/<fqcn>.json` | `gate_runner.py` (`gate_g2`) + `test_patch_applier.py` |
| G3   | Bytecode primero — AST solo fallback (política de precedencia, no gate en runtime) | `bytecode_scanner.py` |
| G4   | Annotation processors → `target/generated-sources` indexado | **NOT_IMPLEMENTED** (pendiente; `gate_runner.py` lo reporta `NOT_IMPLEMENTED`) |
| G5   | Framework/versión declarado en `state/stack-profile.json` (sin valores `unknown`/`blocked`) | `gate_runner.py` (`gate_g5`) + `test_patch_applier.py` |
| G6   | Linter pre-compile pasa antes de `mvn` | `gate_runner.py` (`gate_g6` → `test_linter.py`) |
| G7   | `hash(errorCode, symbolFQN, fixId)` no marcado FAILED previamente | `gate_runner.py` (`_G7_MAX_FAILED_ATTEMPTS=2`, `_G7_MAX_TESTCASE_ATTEMPTS=3`) |
| G8   | 2 ciclos sin delta o `compileFailRate>0.5` ⇒ abortar | `gate_runner.py` (`gate_g8`); backstop en `test_patch_applier.py` + dueño único del loop `cycle_loop.py` (tickea budget + escribe los campos que G8 lee + evalúa G8) |
| G9   | Diagnósticos JDT normalizados, no inferencia libre (normalización, no gate bloqueante) | `compile_error_parser.py` |

## Prohibiciones absolutas (aplica a todo agente LLM)

1. **NUNCA** leer `.java`, `pom.xml`, `build.gradle`, classpath crudo, `jacoco.xml`, bytecode.
2. **NUNCA** inventar imports, clases, métodos, campos, constructores, builders, setters, factories, fixtures.
3. **NUNCA** usar un import fuera de `contextPack.allowedImports` (o `imp` en compact).
4. **NUNCA** instanciar interface / abstract / FreeBuilder sin estrategia confirmada en el contrato.
5. **NUNCA** devolver Java crudo, fences markdown, prosa fuera del JSON estructurado.
6. **NUNCA** insertar `import`, `package`, `class`, `interface`, `enum` dentro de `methods[].body`.
7. **NUNCA** repetir un fix marcado FAILED en `failureMemory` para el mismo `(errorCode, symbolFQN, fixId)`.
8. **NUNCA** mezclar APIs entre frameworks (JUnit 4↔5, `javax`↔`jakarta`) — usar lo declarado en `stack`.
9. **NUNCA** modificar `src/main/java/**` — el patcher lanza `PermissionError` (exit 3).
10. **NUNCA** usar `Thread.sleep`, `new Random()`, `Instant.now()` sin Clock mock o seed fijo.
11. **NUNCA** silenciar tests con `@Ignore`/`@Disabled`/`assume*` o tragar excepciones.
12. **NUNCA** usar `new Type_Builder()` directo — solo `new Type.Builder()` si el contrato lo confirma.

## Patrones correctivos (un solo ejemplo por regla)

```java
// G1 — Import no whitelisted → omitir + TODO
// TODO: verify import in state/import-whitelist.json
// import com.acme.SomeClass;

// G2 — Símbolo no en contrato → mock si Mockito disponible
SomeType dep = Mockito.mock(SomeType.class);
// TODO: verify symbol in state/symbol-contracts/com.acme.SomeType.json

// G2 (FreeBuilder) — solo .Builder() si el contrato lo lista
NaturalPerson p = new NaturalPerson.Builder()
    .setName("John")   // setName ∈ builders[].setters[]
    .build();

// G5 — Framework no en stack → leer stack-profile.json, usar el declarado
// (Si JUnit 4 declarado, no @ExtendWith(MockitoExtension.class))

// Evidence comment al final de cada @Test
// evidence: sym:com.acme.FooService#processName:e7a1b2c3, ctor:com.acme.FooService:b3c1d2e0
```

## Dónde buscar evidencia

| Qué necesitás | Dónde |
|---------------|-------|
| Imports válidos | `state/import-whitelist.json` → `packages[]`, `classes[]` |
| Frameworks disponibles | `state/stack-profile.json` |
| Clase existe? | `state/index/classes.json` |
| Constructor signature | `state/symbol-contracts/<fqcn>.json` → `constructors[]` |
| Método existe? | `state/index/methods.json` |
| Builder strategy | `state/symbol-contracts/<fqcn>.json` → `instantiation` |
| Builder setters | `state/symbol-contracts/<fqcn>.json` → `builders[].setters[]` |
| Test dependencies | `state/dependency-graph.json` |
| Fixtures | `state/fixture-catalog.json` |
| **Único input LLM** | `state/context-packs/<fqcn>.json` |

## Salida del agente

Cada `@Test` generado termina con un comentario de evidencia citando los
`evidenceId` consumidos. Si no podés citar evidencia → el símbolo no está
verificado → **no emitas la línea**.
