$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker is missing. Install a supported Linux-container environment first; no host fallback is permitted.' }
docker info --format '{{.OSType}}'
if ($LASTEXITCODE -ne 0) { throw 'Docker engine is unavailable.' }
docker build --pull -t agentforge-test:py313 (Join-Path $projectRoot 'sandbox')
if ($LASTEXITCODE -ne 0) { throw 'Sandbox image build failed.' }
docker image inspect agentforge-test:py313 --format '{{.Id}}'
