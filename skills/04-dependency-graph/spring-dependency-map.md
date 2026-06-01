# Spring Dependency Map

> **DETERMINISTA — no es un turno del LLM.** Esta fase la ejecuta el pipeline
> Python (`tools/python/dependency_graph_extractor.py`); este skill documenta el comportamiento, el
> LLM no lo corre. Ver `skills/00-runtime/02-phase-contracts.md`.


Aplica solo si `stack-profile.modules[].di.spring == true`.

## Objetivo
Distinguir colaboradores Spring del resto y mapearlos a la estrategia de test correcta.

## Reglas por estereotipo
- `@Service`, `@Component`: mock con `@Mock` o `Mockito.mock`.
- `@Repository` JPA: si el SUT es otra clase ⇒ mock; si el SUT es el repository ⇒ requiere `@DataJpaTest` (solo si slice habilitado).
- `@RestController`: si SUT es controller ⇒ test con `MockMvc` y `@WebMvcTest(Controller.class)` + `@MockBean` para servicios.
- `ApplicationContext`, `Environment`, `BeanFactory`: mock; no levantar contexto real para unit tests.
- `RestTemplate`/`WebClient`: mock; ver `external-client-map.md`.

## Salida
Bloque `springStrategy` en `dependency-graph.json` por SUT: `{ slice: "WebMvcTest|DataJpaTest|none", mockBeans: [...] }`.

## Prohibido
- `@SpringBootTest` en este sistema (no es unit test). Si el usuario lo pide, registrar como riesgo y delegar a otra arquitectura.
