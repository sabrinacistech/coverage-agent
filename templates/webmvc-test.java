// Deterministic template — Spring WebMvc slice
// Phase 5 of optimization roadmap.
// Placeholders: ${PACKAGE}, ${CONTROLLER_SIMPLE}, ${CONTROLLER_FQN}, ${COLLABORATORS}, ${TEST_BODY}
package ${PACKAGE};

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@WebMvcTest(${CONTROLLER_SIMPLE}.class)
class ${CONTROLLER_SIMPLE}Test {

    @Autowired
    private MockMvc mockMvc;

    // ${COLLABORATORS}
    //   @MockBean private <Service> <name>;

    // ${TEST_BODY} — LLM completes request scenarios only.
}
