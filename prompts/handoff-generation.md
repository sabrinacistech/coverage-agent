───────────── COPIÁ DESDE ACÁ (pegar en Claude Code / Codex) ─────────────
# 🧪 Generación de pruebas — handoff batch de coverage-agent

Actúa como un **Motor de Generación de Pruebas de Software (Harness Engineering)** y
**Especialista en Automatización de Cobertura en Java**. Tu tarea es procesar un lote
de solicitudes de generación de pruebas (*handoff batch*) y devolver un archivo de
respuesta estrictamente estructurado.

> Run `${RUN_ID}` · Batch `${BATCH_ID}`

---

## 📋 ENTRADA Y SALIDA
1. **Leer la solicitud (Request):** Analizá el JSON de origen en:
   `${REQUEST_PATH}`

2. **Escribir la respuesta (Response):** Generá el contenido que se guardará
   EXACTAMENTE en:
   `${RESPONSE_PATH}`

El request es una **entidad aislada y autocontenida**: todo lo que necesitás para
escribir el test ya está dentro de ese JSON. Cada target trae `sutSourceCode`
(cuerpos de métodos/constructores), `allowedImports`, `evidenceRefs`,
`dependencySignatures`, `fixturePlan`, `allowedEvidenceIds` y `canonicalTestClass`.

---

## 🛠️ REGLAS ESTRICTAS DE EJECUCIÓN (GUARDRAILS)
* **Formato único:** La salida debe ser EXCLUSIVAMENTE un objeto JSON válido. No
  incluyas explicaciones, introducciones ni bloques Markdown (sin ```` ```json ````).
  Solo el JSON crudo.
* **Aislamiento total:** NO leas, abras, indexes ni infieras desde el repositorio,
  código productivo, `pom.xml`, working tree de Git, jacoco ni ninguna ruta fuera de
  este request. Si un símbolo necesario no está en el JSON, **no existe para vos**.
* **Integridad del código:** No modifiques bajo ninguna circunstancia el código
  productivo. No inventes imports, métodos, constructores ni clases que no existan en
  el contexto del request.
* **Sin estructura redundante:** NO devuelvas las propiedades `patchDescriptor` ni
  `testSource`. El runner las construye de forma canónica. Por cada target devolvé
  ÚNICAMENTE: `targetId`, `status`, `methods`, `reason`, `missingSymbols`.
* **Clase de test canónica:** Usá `target.canonicalTestClass` tal cual; nunca crees
  variantes (`*CtorTest`, `*UnitTest`, …) ni la derives del nombre del método.
* **Trazabilidad de evidencia:** Para cada método, debe cumplirse estrictamente
  `method.evidenceIds ⊆ target.allowedEvidenceIds`. Usá SOLO símbolos cuyos imports
  estén en `target.allowedImports`.
* **Calidad del test:** Usá Arrange / Act / Assert (given/when/then), tests unitarios
  pequeños y deterministas, el framework JUnit/Mockito/aserciones que ya usa el
  proyecto, y construí el fixture según `target.fixturePlan` (no referencies variables
  no declaradas). No inventes outputs esperados: derivalos del `sutSourceCode`.

---

## 📐 ESQUEMA DE SALIDA (OUTPUT SCHEMA)
La respuesta debe cumplir `schemaVersion` **"${SCHEMA_VERSION}"** y contener un array
de resultados mapeado **uno a uno** con los `targets` del request.

### Matriz de estados por target
Asigná a cada target uno de estos 4 estados:

1. **`generated`** — la prueba se crea con éxito.
   * Campos requeridos: `status`, `methods` (array de objetos de método).
2. **`skipped`** — saltás el target por una razón válida de arquitectura.
   * Campos requeridos: `status`, `reason`.
3. **`failed`** — error irrecuperable en la generación.
   * Campos requeridos: `status`, `reason`.
4. **`NEED_MORE_CONTEXT`** — faltan firmas o metadatos clave para compilar.
   * Campos requeridos: `status`, `missingSymbols` (array de strings con los símbolos
     faltantes, p. ej. FQCNs).

---

## 🗂️ PLANTILLA DEL FORMATO DE SALIDA REQUERIDO
```json
{
  "schemaVersion": "${SCHEMA_VERSION}",
  "runId": "${RUN_ID}",
  "batchId": "${BATCH_ID}",
  "role": "generation",
  "targets": [
    {
      "targetId": "ID_DEL_TARGET_1",
      "status": "generated",
      "methods": [
        {
          "name": "metodo_condicion_resultadoEsperado",
          "annotations": ["@Test"],
          "body": "// given\n...\n// when\n...\n// then\n...",
          "evidenceIds": ["EV-001"]
        }
      ]
    },
    {
      "targetId": "ID_DEL_TARGET_2",
      "status": "skipped",
      "reason": "Explicación detallada de por qué se saltó"
    },
    {
      "targetId": "ID_DEL_TARGET_3",
      "status": "NEED_MORE_CONTEXT",
      "missingSymbols": ["com.sabrinacistech.multiclusters.model.MissingClass"]
    }
  ]
}
```
───────────── COPIÁ HASTA ACÁ ─────────────
