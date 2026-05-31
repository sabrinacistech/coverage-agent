// Deterministic template — Spring WebFlux / Reactor (Mono/Flux)
// Phase 5 of optimization roadmap.
// Placeholders: ${PACKAGE}, ${SUT_SIMPLE}, ${COLLABORATORS}, ${TEST_BODY}
package ${PACKAGE};

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import reactor.core.publisher.Mono;
import reactor.core.publisher.Flux;
import reactor.test.StepVerifier;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class ${SUT_SIMPLE}Test {

    // ${COLLABORATORS}
    //   @Mock private <ReactiveCollaborator> <name>;

    @InjectMocks
    private ${SUT_SIMPLE} sut;

    // ${TEST_BODY} — LLM completes StepVerifier scenarios only.
    // Reactive contract: every test MUST end with StepVerifier#verifyComplete()
    // or StepVerifier#verifyError(...). Never call .block() in tests.
}
