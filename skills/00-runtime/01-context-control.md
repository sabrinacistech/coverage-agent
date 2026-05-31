# Context Control

## Objetivo
Mantener el contexto del LLM enfocado en la fase actual. Evita ruido y reduce alucinación.

## Reglas
- Cargar **solo** los skills de la fase activa más los contratos vigentes (`stack-profile`, `import-whitelist`, `symbol-contracts/<sut>` actual).
- Estados históricos > 1 ciclo ⇒ comprimir a resumen (`state/_summaries/cycle-<n>.json`).
- No cargar JaCoCo XML completo en contexto; pasar el delta computado.
- No cargar código productivo completo; pasar solo los fragmentos referenciados por `evidence-id`.
- Cada agente declara su presupuesto máximo (tokens) y rechaza cargar más.

## Antipatrones
- "Cargar todo el repo por las dudas".
- "Reincluir el contrato global en cada paso".
- Repetir el MASTER_PROMPT entero en cada subagente (se referencia, no se copia).

## Determinismo vs LLM (Phase 2)

El context budget se respeta porque el trabajo pesado **no llega al LLM**. Antes de armar un prompt, validar contra `skills/00-runtime/deterministic-analysis-policy.md`:

- Imports, framework, dependencias, compile errors, stack traces ⇒ **fuera del prompt** (vienen ya resueltos vía `state/index/` y `state/compile-error-index.json`).
- Solo entran al prompt: target method, colaboradores necesarios, líneas fallantes (no el archivo entero), contratos mínimos, fixtures mínimas.

## Surgical inputs (Phase 4)

Para generación y reparación, preferir **inputs quirúrgicos**:

- método objetivo + firma de colaboradores (no la clase completa),
- líneas con error (no el archivo de test entero),
- fragmentos citados por `evidence-id` (no el contrato completo).

Ver `skills/07-generation/ast-patch-generation.md`.
