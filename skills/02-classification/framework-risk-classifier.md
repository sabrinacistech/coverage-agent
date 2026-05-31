# Framework Risk Classifier

## Objetivo
Etiquetar clases que requieren estrategia especial por framework, antes de generar.

## Etiquetas
- `spring.controller` (`@RestController`, `@Controller`): requiere `@WebMvcTest` + `MockMvc` si Spring Test presente; si no, test puro con mocks de servicios.
- `spring.service` (`@Service`): unit test con mocks.
- `spring.repository` (`@Repository`, `JpaRepository`): solo testeable con `@DataJpaTest` o slice equivalente; si no presente ⇒ excluir.
- `spring.config` (`@Configuration`): excluido salvo lógica explícita.
- `jpa.entity` (`@Entity`): excluida salvo lógica de negocio embebida.
- `async` (uso de `@Async`, `CompletableFuture`): requiere sincronización en test (no `sleep`).
- `reflection`: clases que invocan reflection ⇒ marcar `risk.high`.

## Salida
Añade `tags[]` por clase en `classification-index.json` y eleva `risk` según presencia de tags.
