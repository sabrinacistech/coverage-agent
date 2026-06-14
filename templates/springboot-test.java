// Deterministic template — Spring Boot slice / integration smoke
// Phase 5 of optimization roadmap.
// Placeholders: PACKAGE, SUT_SIMPLE, PROFILES, ASSERT_IMPORTS, ASSERT_NOT_NULL, TEST_BODY
package ${PACKAGE};

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

${ASSERT_IMPORTS}

@SpringBootTest
@ActiveProfiles({ ${PROFILES} })
class ${SUT_SIMPLE}IT {

    @Autowired
    private ${SUT_SIMPLE} sut;

    @Test
    void contextLoads() {
        ${ASSERT_NOT_NULL}
    }

    // ${TEST_BODY} — LLM completes scenario @Test methods only.
}
