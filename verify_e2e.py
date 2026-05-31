"""Verificación end-to-end del driver v2 contra estado y repo Java REALES,
en todo lo que NO requiere la API key. La llamada real al modelo (A5) se prueba
aparte cuando hay ANTHROPIC_API_KEY.

Uso:
  python verify_e2e.py <state_dir> <java_repo>
"""
import json
import os
import sys
import types
from pathlib import Path

from orchestrator import config, generation, llm_gateway, one_cycle, prompts

_REAL_COMPLETE = llm_gateway.complete  # capturado antes de los mocks de A4

state_dir = Path(sys.argv[1]).resolve()
repo = Path(sys.argv[2]).resolve()
ok = []
fail = []


def check(name, cond, detail=""):
    (ok if cond else fail).append(f"{name} {detail}")
    print(f"  [{'OK ' if cond else 'FAIL'}] {name} {detail}")


print("=== A1. Selección de target contra batch-plan REAL ===")
target = one_cycle.select_next_target(state_dir)
check("select_next_target devuelve un target", target is not None, f"-> {target and target.get('targetId')}")
sut = target["sut"]
pack_full = one_cycle.load_context_pack(state_dir, sut)
pack_compact = one_cycle.load_context_pack_compact(state_dir, sut)
check("carga context-pack completo (para el patcher)", pack_full.get("sut") == sut, f"-> {sut}")
check("context-pack trae allowedImports", len(pack_full.get("allowedImports", [])) > 0,
      f"({len(pack_full.get('allowedImports', []))} imports)")
full_bytes = len(json.dumps(pack_full)); compact_bytes = len(json.dumps(pack_compact))
check("compact MUCHO menor que el completo", compact_bytes * 10 < full_bytes,
      f"({compact_bytes} vs {full_bytes} bytes · {full_bytes // max(compact_bytes,1)}x)")

print("\n=== A2. Ensamblado de prompt con datos REALES (pack COMPACTO al modelo) ===")
msgs = prompts.build_messages("generation", {"contextPack": pack_compact, "testCase": one_cycle.testcase_from_target(target)})
check("2 mensajes (system+user)", len(msgs) == 2)
check("system = agente test-body", "test-body-agent" in msgs[0]["content"] or "patch descriptor" in msgs[0]["content"].lower())
check("user lleva el SUT", sut in msgs[1]["content"])
check("prompt de tamaño razonable (<20K chars)", len(msgs[1]["content"]) < 20000)
print(f"       system={len(msgs[0]['content'])} chars · user={len(msgs[1]['content'])} chars · modelo={config.model_for_role('generation')}")

print("\n=== A3. Budget hook (gateway) contra llm-budget REAL ===")
try:
    llm_gateway._assert_within_token_budget(state_dir)
    check("presupuesto OK (entries vacías) no bloquea", True)
except llm_gateway.BudgetExceeded:
    check("presupuesto OK no bloquea", False)

# Simula un SUT sobre techo y confirma que BLOQUEA antes de cualquier llamada.
budget_path = state_dir / "_summaries" / "llm-budget.json"
orig = budget_path.read_text(encoding="utf-8")
budget_path.write_text(json.dumps({"schemaVersion": 1, "entries": [
    {"sut": sut, "estimatedTokensIn": 999999, "maxTokensIn": 10, "overBudget": True}]}), encoding="utf-8")
try:
    llm_gateway._assert_within_token_budget(state_dir)
    check("over-budget BLOQUEA", False)
except llm_gateway.BudgetExceeded:
    check("over-budget BLOQUEA (BudgetExceeded)", True)
finally:
    budget_path.write_text(orig, encoding="utf-8")

print("\n=== A4. one_cycle completo con modelo MOCK (sin key) — flujo + patcher ===")
# A4a: el modelo devuelve BLOCKED -> el ciclo termina, marca procesado, rc 0, sin tocar Java.
llm_gateway.complete = lambda *a, **k: json.dumps(
    {"schemaVersion": 1, "status": "BLOCKED", "blockReason": "verificación: sin método"})
generation.llm_gateway.complete = llm_gateway.complete  # asegura el binding usado por generation
rc = one_cycle.run_one_cycle(state_dir, repo)
check("BLOCKED del modelo -> ciclo OK (rc 0)", rc == 0, f"(rc={rc})")
check("target marcado como procesado", target["targetId"] in one_cycle._processed_ids(state_dir))

# A4b: el modelo devuelve un patch con import fuera del perímetro -> patcher lo bloquea (rc 3 interno).
target2 = one_cycle.select_next_target(state_dir)
if target2:
    sut2 = target2["sut"]
    bad_patch = {"schemaVersion": 1, "patchId": "patch:deadbe", "sut": sut2,
                 "testClass": sut2 + "Test", "allowedImports": ["com.evil.Backdoor"]}
    llm_gateway.complete = lambda *a, **k: json.dumps(bad_patch)
    generation.llm_gateway.complete = llm_gateway.complete
    pack2_path = state_dir / "context-packs" / f"{sut2}.json"
    rc_apply = one_cycle.apply_patch(bad_patch, state_dir=state_dir, repo=repo, context_pack_path=pack2_path)
    check("import fuera del perímetro -> patcher rc 3", rc_apply == 3, f"(rc={rc_apply})")

print("\n=== A5. Llamada REAL al modelo (solo si hay ANTHROPIC_API_KEY) ===")
if not os.environ.get("ANTHROPIC_API_KEY"):
    print("  [SKIP] ANTHROPIC_API_KEY no seteada — saltando la llamada real al modelo.")
else:
    # Restaura el gateway real (A4 lo había mockeado) y fuerza el proveedor
    # autónomo (litellm), ya que el default de etapa 1 es el handoff IDE.
    llm_gateway.complete = _REAL_COMPLETE
    generation.llm_gateway.complete = _REAL_COMPLETE
    os.environ["COVAGENT_LLM_PROVIDER"] = "litellm"
    tgt = one_cycle.select_next_target(state_dir)
    if tgt is None:
        print("  [SKIP] no quedan targets sin procesar para la llamada real.")
    else:
        sut_r = tgt["sut"]
        pack_r = one_cycle.load_context_pack_compact(state_dir, sut_r)
        try:
            patch = generation.generate_patch(
                state_dir=state_dir, context_pack=pack_r,
                test_case=one_cycle.testcase_from_target(tgt))
            status = str(patch.get("status", "PATCH")).upper()
            check("el modelo devolvió un patch-descriptor VÁLIDO (schema)", True,
                  f"-> {status} · patchId={patch.get('patchId', 'n/a')}")
            check("contrato esperado (PATCH o BLOCKED)", status in ("BLOCKED",) or "patchId" in patch,
                  f"({len(patch.get('methods', []))} métodos)" if "patchId" in patch else "")
        except generation.PatchSchemaError as exc:
            check("el modelo devolvió un patch-descriptor VÁLIDO (schema)", False, f"-> {exc}")
        except llm_gateway.BudgetExceeded as exc:
            check("llamada real dentro de presupuesto", False, f"-> {exc}")

print("\n=== RESUMEN ===")
print(f"  OK:   {len(ok)}")
print(f"  FAIL: {len(fail)}")
if fail:
    for f in fail:
        print("   - FALLA:", f)
    sys.exit(1)
if os.environ.get("ANTHROPIC_API_KEY"):
    print("\n  Verificación COMPLETA, incluida la llamada real al modelo (A5).")
else:
    print("\n  Verificación sin-key COMPLETA. Falta A5 (llamada real) -> setear ANTHROPIC_API_KEY y re-correr.")
