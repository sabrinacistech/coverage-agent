// Deterministic template — JUnit 5 + Mockito
// Phase 5 of optimization roadmap.
// Placeholders: PACKAGE, SUT_SIMPLE, SUT_FQN, COLLABORATORS, ASSERT_IMPORTS, TEST_BODY
package ${PACKAGE};

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

${ASSERT_IMPORTS}
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class ${SUT_SIMPLE}Test {

    // ${COLLABORATORS} — emitted by deterministic patcher:
    //   @Mock private <Type> <name>;

    @InjectMocks
    private ${SUT_SIMPLE} sut;

    @BeforeEach
    void setUp() {
        // Deterministic init only. Fixtures injected by ast-patcher.
    }

    // ${TEST_BODY} — LLM completes per-method @Test blocks only.
}
