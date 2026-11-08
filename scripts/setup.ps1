$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & '.\.venv\Scripts\python.exe' -m pip install -r backend\requirements-lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency install failed.' }
    Push-Location -LiteralPath frontend
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency install failed.' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
} finally { Pop-Location }
