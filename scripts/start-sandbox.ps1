# Start the already installed Docker Engine in the project's dedicated WSL distro.
# This script does not install packages, change firewall rules, or expose TCP.
$ErrorActionPreference = 'Stop'
$distribution = 'AgentForge'
$installed = @(wsl.exe --list --quiet) | ForEach-Object { ($_ -replace "`0", '').Trim() }
if ($distribution -notin $installed) { throw 'The AgentForge WSL distribution is not installed. Run setup-wsl-sandbox.ps1 after the verified WSL runtime installation.' }
$initProcess = ((wsl.exe --distribution $distribution --user root --exec ps -p 1 -o comm=) -replace "`0", '').Trim()
if ($initProcess -eq 'systemd') {
    wsl.exe --distribution $distribution --user root --exec systemctl start docker
} else {
    wsl.exe --distribution $distribution --user root --exec service docker start
}
if ($LASTEXITCODE -ne 0) { throw 'Docker Engine service could not start in AgentForge WSL.' }
wsl.exe --distribution $distribution --user root --exec docker info --format '{{.OSType}}'
if ($LASTEXITCODE -ne 0) { throw 'Docker Engine is not responding in AgentForge WSL.' }
wsl.exe --distribution $distribution --user root --exec docker image inspect agentforge-test:py313 --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Engine started, but the AgentForge acceptance image is missing. Run setup-wsl-sandbox.ps1 to build it.' }
