param([ValidateRange(10, 600)][int]$ModelTimeoutSeconds = 120, [ValidateRange(1024, 65535)][int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$previousModelTimeout = $env:AGENTFORGE_LLM_TIMEOUT
$previousUiPort = $env:AGENTFORGE_UI_PORT
Push-Location -LiteralPath $projectRoot
try {
    $env:AGENTFORGE_LLM_TIMEOUT = [string]$ModelTimeoutSeconds
    $env:AGENTFORGE_UI_PORT = [string]$Port
    & '.\.venv\Scripts\python.exe' -m uvicorn agentforge.app:app --app-dir backend --host 127.0.0.1 --port $Port
} finally {
    $env:AGENTFORGE_LLM_TIMEOUT = $previousModelTimeout
    $env:AGENTFORGE_UI_PORT = $previousUiPort
    Pop-Location
}
