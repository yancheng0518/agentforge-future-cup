param([ValidateRange(10, 600)][int]$ModelTimeoutSeconds = 120, [ValidateRange(1024, 65535)][int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath (Join-Path $projectRoot 'frontend')
try {
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed. The workbench was not started.' }
} finally { Pop-Location }
Write-Host "Open http://127.0.0.1:$Port ; press Ctrl+C to stop."
& (Join-Path $PSScriptRoot 'backend.ps1') -ModelTimeoutSeconds $ModelTimeoutSeconds -Port $Port
