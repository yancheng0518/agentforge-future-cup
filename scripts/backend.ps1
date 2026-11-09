param([ValidateRange(10, 600)][int]$ModelTimeoutSeconds = 120)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$previousModelTimeout = $env:AGENTFORGE_LLM_TIMEOUT
Push-Location -LiteralPath $projectRoot
try {
    $env:AGENTFORGE_LLM_TIMEOUT = [string]$ModelTimeoutSeconds
    & '.\.venv\Scripts\python.exe' -m uvicorn agentforge.app:app --app-dir backend --host 127.0.0.1 --port 8000
} finally {
    $env:AGENTFORGE_LLM_TIMEOUT = $previousModelTimeout
    Pop-Location
}
