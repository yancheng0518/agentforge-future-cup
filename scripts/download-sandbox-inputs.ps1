<#
.SYNOPSIS
  下载并校验 WSL2 专用隔离环境所需的官方文件。

.DESCRIPTION
  下载以下两个固定版本文件到 .agentforge\sandbox-install\ 并校验 SHA256：
  1. Microsoft WSL 3.0.3 x64 MSI（约 350 MB）
  2. Canonical Ubuntu 22.04 LTS WSL rootfs（约 325 MB）
  校验通过后，运行 scripts\setup-wsl-sandbox.ps1 导入发行版并安装 Docker Engine。
  若官方 "current" 地址的文件已更新导致哈希不匹配，脚本会保留文件并如实报告，不会静默通过。
#>
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$installRoot = Join-Path $projectRoot '.agentforge\sandbox-install'
New-Item -ItemType Directory -Force -Path $installRoot | Out-Null

$targets = @(
    [ordered]@{
        Name = 'wsl.3.0.3.0.x64.msi'
        Url = 'https://github.com/microsoft/WSL/releases/download/3.0.3/wsl.3.0.3.0.x64.msi'
        Sha256 = 'AE977286974255BD26BF925F00F40F5692C6B2084155CBC0563C09AEA091F211'
        Explanation = 'Microsoft WSL 3.0.3 官方发布（GitHub Releases）'
    },
    [ordered]@{
        Name = 'ubuntu22.04-rootfs.tar.gz'
        Url = 'https://cloud-images.ubuntu.com/wsl/jammy/current/ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz'
        Sha256 = '1483CC5C1DCE13064F774834CBFFDFF226559FD522A67A381A8EA77D63FB4109'
        Explanation = 'Canonical Ubuntu 22.04 LTS WSL rootfs（2025-03 构建，见 docs/sandbox-setup.md）'
    }
)

$failed = 0
foreach ($target in $targets) {
    $path = Join-Path $installRoot $target.Name
    Write-Host "`n=== $($target.Name) ===" -ForegroundColor Cyan
    Write-Host "  来源：$($target.Explanation)"
    Write-Host "  URL ：$($target.Url)"
    if (Test-Path -LiteralPath $path) {
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if ($hash -eq $target.Sha256) {
            Write-Host "  [OK] 已存在且校验通过" -ForegroundColor Green
            continue
        }
        Write-Host "  [WARN] 已存在但哈希不匹配，将重新下载" -ForegroundColor Yellow
        Remove-Item -LiteralPath $path -Force
    }
    try {
        Write-Host '  下载中（大文件，可能需要数分钟）...'
        Invoke-WebRequest -Uri $target.Url -OutFile $path -UseBasicParsing -TimeoutSec 1800
    } catch {
        Write-Host "  [ERROR] 下载失败：$($_.Exception.Message)" -ForegroundColor Red
        $failed++
        continue
    }
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if ($hash -eq $target.Sha256) {
        Write-Host "  [OK] 下载并校验通过：$path" -ForegroundColor Green
    } else {
        Write-Host "  [ERROR] SHA256 不匹配（期望 $($target.Sha256)，实际 $hash）" -ForegroundColor Red
        Write-Host "  文件已保留在 $path；若官方 current 地址已更新，请参考 docs/sandbox-setup.md 使用匹配的版本。" -ForegroundColor Yellow
        $failed++
    }
}

if ($failed -gt 0) { exit 1 }
Write-Host "`n全部输入文件已就绪。下一步：powershell -ExecutionPolicy Bypass -File .\scripts\setup-wsl-sandbox.ps1" -ForegroundColor Green
exit 0
