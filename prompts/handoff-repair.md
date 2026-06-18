───────────── COPIÁ DESDE ACÁ (pegar en Claude Code / Codex) ─────────────
# 🛠️ Reparación de pruebas — handoff repair de coverage-agent (round ${REPAIR_ROUND})

Actúa como un **Especialista en Reparación de Pruebas Java**. Tu tarea es reparar los
tests que fallaron al compilar o ejecutar en este batch, devolviendo un archivo de
respuesta estrictamente estructurado.

> Run `${RUN_ID}` · Batch `${BATCH_ID}` · Repair round ${REPAIR_ROUND}

---

## 📋 ENTRADA Y SALIDA
1. **Leer la solicitud (Request):** Analizá el JSON de origen en:
   `${REQUEST_PATH}`

2. **Escribir la respuesta (Response):** Generá el contenido que se guardará
   EXACTAMENTE en:
   `${RESPONSE_PATH}`

Cada `failedItem` trae todo lo necesario y aislado: `currentTestSource` (el test que
falló), `compilerErrorDetails` (salida exacta de javac/Maven), `patcherErrorDetails`
(rechazos del patcher, p. ej. G2), `allowedImports`, `allowedEvidenceIds` y
`canonicalTestClass`.

---

## 🛠️ REGLAS ESTRICTAS DE EJECUCIÓN (GUARDRAILS)
* **Formato único:** La salida debe ser EXCLUSIVAMENTE un objeto JSON válido. Sin
  explicaciones ni bloques Markdown. Solo el JSON crudo.
* **Aislamiento total:** Operá SOLO con la información de este request. No leas el
  repositorio, código productivo, `pom.xml` ni el working tree de Git.
* **Reparar solo tests:** Reparás SOLO los tests generados, NUNCA `src/main`.
* **Evidencia e imports:** Usá exclusivamente `failedItem.allowedImports` y
  `failedItem.allowedEvidenceIds`.
* **Clase de test canónica:** `patchDescriptor.testClass` debe ser EXACTAMENTE
  `failedItem.canonicalTestClass`.
* **Trazabilidad del patch:** En repair, `patchId` debe empezar con `"repair:"`.

---

## 📐 ESQUEMA DE SALIDA (OUTPUT SCHEMA)
La respuesta debe cumplir `schemaVersion` **"${SCHEMA_VERSION}"** y contener un item
por cada `failedItem` del request.

### Matriz de estados por item
1. **`repaired`** — el test se reparó con éxito.
   * Campos requeridos: `status`, `patchDescriptor` válido.
2. **`abandoned`** / **`skipped`** / **`failed`** — no se pudo reparar.
   * Campos requeridos: `status`, `reason`.

---

## 🗂️ PLANTILLA DEL FORMATO DE SALIDA REQUERIDO
```json
{
  "schemaVersion": "${SCHEMA_VERSION}",
  "runId": "${RUN_ID}",
  "batchId": "${BATCH_ID}",
  "role": "repair",
  "items": [
    {
      "targetId": "ID_DEL_TARGET_1",
      "status": "repaired",
      "patchDescriptor": { "patchId": "repair:...", "testClass": "...", "methods": [] }
    },
    {
      "targetId": "ID_DEL_TARGET_2",
      "status": "abandoned",
      "reason": "Explicación detallada de por qué no se pudo reparar"
    }
  ]
}
```
───────────── COPIÁ HASTA ACÁ ─────────────
