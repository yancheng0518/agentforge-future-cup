$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & '.\.venv\Scripts\python.exe' -m uvicorn agentforge.app:app --app-dir backend --host 127.0.0.1 --port 8000
} finally { Pop-Location }
