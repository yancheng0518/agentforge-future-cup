# Provision the project's dedicated WSL2 distribution after WSL runtime installation.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$distribution = 'AgentForge'
$installRoot = Join-Path $projectRoot '.agentforge\sandbox-install'
$distroRoot = Join-Path $projectRoot '.agentforge\wsl\AgentForge'
$rootfs = Join-Path $installRoot 'ubuntu22.04-rootfs.tar.gz'
$expectedRootfsHash = '1483CC5C1DCE13064F774834CBFFDFF226559FD522A67A381A8EA77D63FB4109'
if (-not (Test-Path -LiteralPath $rootfs)) { throw 'Download the documented Canonical Ubuntu 22.04 rootfs first.' }
if ((Get-FileHash -LiteralPath $rootfs -Algorithm SHA256).Hash -ne $expectedRootfsHash) { throw 'Ubuntu rootfs checksum mismatch.' }
$installed = @(wsl.exe --list --quiet) | ForEach-Object { ($_ -replace "`0", '').Trim() }
if ($distribution -in $installed) {
    $registration = Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' -ErrorAction SilentlyContinue | ForEach-Object { Get-ItemProperty -LiteralPath $_.PSPath } | Where-Object DistributionName -eq $distribution
    $registeredPath = if ($registration) { ($registration.BasePath -replace '^\\\\\?\\', '').TrimEnd('\') } else { '' }
    if ($registeredPath -ne $distroRoot.TrimEnd('\')) { throw 'An AgentForge distribution already exists outside this project. Refusing to change it.' }
}
if ($distribution -notin $installed) {
    New-Item -ItemType Directory -Force -Path $distroRoot | Out-Null
    wsl.exe --import $distribution $distroRoot $rootfs --version 2
    if ($LASTEXITCODE -ne 0) { throw 'WSL distribution import failed. No host-code fallback is allowed.' }
}
$linuxRoot = ((wsl.exe --distribution $distribution --user root --exec wslpath -a $projectRoot) -replace "`0", '').Trim()
if ($LASTEXITCODE -ne 0 -or $linuxRoot -notmatch '^/mnt/[a-z]/') { throw 'WSL project path mapping failed.' }
wsl.exe --distribution $distribution --user root --exec bash ($linuxRoot + '/scripts/provision-wsl-docker.sh')
if ($LASTEXITCODE -ne 0) { throw 'Docker Engine provisioning failed.' }
wsl.exe --distribution $distribution --user root --exec docker build --pull -t agentforge-test:py313 ($linuxRoot + '/sandbox')
if ($LASTEXITCODE -ne 0) { throw 'Acceptance image build failed.' }
wsl.exe --distribution $distribution --user root --exec docker image inspect agentforge-test:py313 --format '{{.Id}}'
