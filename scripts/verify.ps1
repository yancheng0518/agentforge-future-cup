$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & '.\.venv\Scripts\python.exe' -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Platform tests failed.' }
    Push-Location -LiteralPath frontend
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
    Write-Output 'Platform checks passed. These checks do NOT prove live LLM or isolated candidate acceptance.'
} finally { Pop-Location }
