// Deterministic template — Spring Boot slice / integration smoke
// Phase 5 of optimization roadmap.
// Placeholders: ${PACKAGE}, ${SUT_SIMPLE}, ${PROFILES}, ${TEST_BODY}
package ${PACKAGE};

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
@ActiveProfiles({ ${PROFILES} })
class ${SUT_SIMPLE}IT {

    @Autowired
    private ${SUT_SIMPLE} sut;

    @Test
    void contextLoads() {
        assertThat(sut).isNotNull();
    }

    // ${TEST_BODY} — LLM completes scenario @Test methods only.
}
