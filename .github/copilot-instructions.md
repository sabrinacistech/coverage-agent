# GitHub Copilot Workspace Instructions
# Java Test Coverage Agent — workspace rules

> **CANON**: las prohibiciones G1-G9 y los patrones correctivos viven en
> [`docs/canonical-prohibitions.md`](../docs/canonical-prohibitions.md).
> Esta sección **no las repite** — solo agrega reglas específicas de Copilot
> en este workspace.

---

## Reglas operativas de Copilot

1. Aplican **todas** las prohibiciones y gates de `docs/canonical-prohibitions.md`.
2. La única entrada permitida al LLM es `state/context-packs/<fqcn>.json`.
3. Si una sugerencia viola G1/G2/G5, el linter (`tools/python/test_linter.py`) la rechazará — **rechazá vos primero**, no esperés al gate.
4. No editás archivos `state/`, `tools/python/`, ni `src/main/java/**`. Solo tests en `src/test/java/**`.
5. Copilot completa **solo `@Test` method bodies + assertions**. La estructura (imports, mocks, `@ExtendWith`) viene del template.

---

## Antes de aceptar una sugerencia

```bash
python tools/python/test_linter.py \
  --test-file     <path/to/TestFile.java> \
  --whitelist     state/import-whitelist.json \
  --contracts     state/symbol-contracts/ \
  --stack-profile state/stack-profile.json \
  --index         state/index \
  --context-pack  state/context-packs/<fqcn>.json
```

Exit code != 0 ⇒ **descartar la sugerencia entera**, no parchear.

---

## Selección de template (Phase 8)

| SUT (de `state/classification-index.json`) | Template |
|--------------------------------------------|----------|
| `@RestController` / `@Controller` | `templates/webmvc-test.java` |
| `@Service` / `@Component` / `@Repository` | `templates/junit5-mockito.java` |
| Reactive (`Mono`, `Flux`) | `templates/reactive-test.java` |
| `@SpringBootTest` integración | `templates/springboot-test.java` |

---

## Evidence-id obligatorio

Cada `@Test` termina con un comentario citando los `evidenceId` consumidos:

```java
// evidence: sym:com.acme.FooService#processName:e7a1b2c3, ctor:com.acme.FooService:b3c1d2e0
```

Si no podés citar evidencia → el símbolo no está verificado → **no escribas esa línea**.

---

## Token minimization (workspace-level)

Inputs autorizados al LLM (ver `docs/token-minimization-strategy.md`):

| Necesidad | Fuente | Max ~tok |
|-----------|--------|----------|
| Contrato del SUT | `state/context-packs/<fqcn>.json` | 1500 |
| Imports válidos | `allowedImports[]` del context pack | pre-filtrado |
| Stack | `stack` del context pack | 50 |
| DI map | `collaborators[]` del context pack | 200 |
| Errores a reparar | `state/compile-error-index.json` | 150 |

**Prohibido cargar**: `pom.xml`, `jacoco.xml`, logs de Maven, `.java` fuente del SUT, `state/import-whitelist.json` completo, `state/symbol-contracts.json` (manifest no autoritativo).

---

## Output format

Copilot emite **JSON patch descriptor** (ver `docs/agent-json-protocol.md`), no Java crudo.
El patch se aplica vía `tools/python/test_patch_applier.py` (único escritor autorizado).

```bash
python tools/python/test_patch_applier.py \
  --patch        state/_patches/<FQCNTest>.patch.json \
  --repo         <ruta-al-repo-java> \
  --state        state \
  --templates    templates \
  --context-pack state/context-packs/<fqcn>.json \
  --whitelist    state/import-whitelist.json \
  --out          state/generated-tests.json
```

*Estas reglas son enforced por el pipeline. Sugerencias que las violan son
rechazadas por el linter (G6) y no se commitean.*
