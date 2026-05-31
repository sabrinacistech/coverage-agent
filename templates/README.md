# templates/ — Deterministic Test Skeletons (Phase 5)

Plantillas determinísticas que el agente de generación instancia mediante el
AST patcher. El LLM **no** escribe el esqueleto: solo completa cuerpos de
`@Test`, asserts y edge cases.

| Archivo                  | Uso                                                         |
|--------------------------|-------------------------------------------------------------|
| `junit5-mockito.java`    | SUT plano + colaboradores mockeables.                       |
| `springboot-test.java`   | Smoke / integration con `@SpringBootTest`.                  |
| `webmvc-test.java`       | Controllers con `@WebMvcTest` + `MockMvc`.                  |
| `reactive-test.java`     | WebFlux / Reactor, validado con `StepVerifier`.             |

## Placeholders

`${PACKAGE}`, `${SUT_SIMPLE}`, `${SUT_FQN}`, `${COLLABORATORS}`, `${CONTROLLER_SIMPLE}`,
`${CONTROLLER_FQN}`, `${PROFILES}`, `${TEST_BODY}` son sustituidos por el patcher
determinístico a partir de `state/index/`, `state/symbol-contracts/<fqcn>.json` y
`state/classification-index.json`.

## Reglas

- Selección de plantilla = función pura de `classification-index.json`
  (controller webmvc → `webmvc-test.java`, reactive → `reactive-test.java`, etc.).
- Los imports están fijados; nuevos imports pasan por `AddImport` + G1.
- El LLM nunca recibe la plantilla completa: recibe solo los placeholders
  relevantes a su unidad de trabajo.

Ver `skills/07-generation/ast-patch-generation.md` y
`skills/00-runtime/deterministic-analysis-policy.md`.
