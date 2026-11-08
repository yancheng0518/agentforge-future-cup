# Install only the verified Microsoft WSL runtime. Run elevated; never reboot.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$installRoot = Join-Path $projectRoot '.agentforge\sandbox-install'
$resultPath = Join-Path $installRoot 'wsl-install-result.json'
$result = [ordered]@{ started_at = (Get-Date).ToString('o'); success = $false; restart_required = $false }
try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator permission is required for the Microsoft WSL runtime installer.' }
    $features = @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform') | ForEach-Object {
        $feature = Get-WindowsOptionalFeature -Online -FeatureName $_
        [PSCustomObject]@{ name = $_; state = $feature.State.ToString() }
    }
    $result.features = $features
    if (@($features | Where-Object state -ne 'Enabled').Count -ne 0) {
        throw 'Required Windows features are not fully enabled. No feature or reboot change has been performed by this script.'
    }
    $msi = Join-Path $installRoot 'wsl.3.0.3.0.x64.msi'
    $expected = 'AE977286974255BD26BF925F00F40F5692C6B2084155CBC0563C09AEA091F211'
    if ((Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash -ne $expected) { throw 'WSL installer checksum mismatch.' }
    $signature = Get-AuthenticodeSignature -LiteralPath $msi
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'Microsoft installer signature verification failed.' }
    $log = Join-Path $installRoot 'wsl-install.log'
    $installProcess = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i', ('"' + $msi + '"'), '/qn', '/norestart', '/L*v', ('"' + $log + '"')) -Wait -PassThru -WindowStyle Hidden
    $result.exit_code = $installProcess.ExitCode
    $result.restart_required = $installProcess.ExitCode -in @(1641, 3010)
    $result.success = $installProcess.ExitCode -in @(0, 1641, 3010)
    if (-not $result.success) { throw ('WSL MSI returned ' + $installProcess.ExitCode + '; inspect wsl-install.log.') }
} catch {
    $result.error = $_.Exception.Message
} finally {
    $result.finished_at = (Get-Date).ToString('o')
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $resultPath -Encoding UTF8
}
if (-not $result.success) { exit 1 }
if ($result.restart_required) { exit 3010 }
