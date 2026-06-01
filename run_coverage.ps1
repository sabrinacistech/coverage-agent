<#
.SYNOPSIS
  Corre el flujo de cobertura (etapa 1, LLM por handoff) de punta a punta:
  JaCoCo baseline -> Fase 0 -> arranca el ciclo y espera el handoff.

.EXAMPLE
  .\run_coverage.ps1
#>
[CmdletBinding()]
param(
  [string]$Repo     = "C:\repo\multi-clusters\cluster-status-service",
  [string]$StateDir = "C:\repo\agent-state-multiclusters"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$AgentRoot = $PSScriptRoot
$Py = Join-Path $AgentRoot ".venv\Scripts\python.exe"
$Jacoco = Join-Path $Repo "target\site\jacoco\jacoco.xml"

Write-Host ""
Write-Host "==== [1/3] JaCoCo baseline (sin tocar el pom) ====" -ForegroundColor Cyan
Push-Location $Repo
& mvn -q -DfailIfNoTests=false `
    org.jacoco:jacoco-maven-plugin:0.8.13:prepare-agent `
    test `
    org.jacoco:jacoco-maven-plugin:0.8.13:report
$mvnExit = $LASTEXITCODE
Pop-Location
if ($mvnExit -ne 0 -or -not (Test-Path $Jacoco)) {
    Write-Host "[FAIL] No se generó jacoco.xml. Revisá el build de Maven." -ForegroundColor Red
    exit 1
}
Write-Host "[OK] jacoco.xml en $Jacoco" -ForegroundColor Green

Write-Host ""
Write-Host "==== [2/3] Fase 0 (analisis) ====" -ForegroundColor Cyan
if (Test-Path $StateDir) { Remove-Item -Recurse -Force $StateDir }
& $Py (Join-Path $AgentRoot "tools\python\run_pipeline.py") `
    --repo $Repo `
    --out  $StateDir `
    --module . `
    --jacoco-xml $Jacoco `
    --coverage-mode coverage
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] Fase 0 no quedó READY (handoff BLOCKED). No se arranca el ciclo." -ForegroundColor Red
    exit 1
}
Write-Host "[OK] Fase 0 READY. State en $StateDir" -ForegroundColor Green

Write-Host ""
Write-Host "==== [3/3] Arranca el ciclo (se va a PAUSAR en el handoff) ====" -ForegroundColor Cyan
Write-Host "Cuando veas '[IDE-HANDOFF] ... Esperando', avisale a Claude Code para que" -ForegroundColor Yellow
Write-Host "genere el patch y lo escriba en el response-*.json. NO cierres esta terminal." -ForegroundColor Yellow
Write-Host ""
$env:COVAGENT_LLM_PROVIDER = "ide"
& $Py (Join-Path $AgentRoot "tools\python\cycle_loop.py") `
    --state     (Join-Path $StateDir "execution-state.json") `
    --state-dir $StateDir `
    -- $Py -m orchestrator.one_cycle --state-dir $StateDir --repo $Repo
