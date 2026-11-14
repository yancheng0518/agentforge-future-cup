<#
.SYNOPSIS
  AgentForge 完整部署脚本（未来杯赛题一参赛作品）。

.DESCRIPTION
  用于首次部署或重建环境：安装 Python/Node 依赖与前端构建、准备并启动隔离 Docker 环境、
  运行环境预检，可选在完成后直接启动工作台。
  需要管理员权限的 WSL 运行时安装、以及从官方下载 WSL/Ubuntu 文件，脚本会给出明确指引
  （或在指定 -DownloadInputs 时自动下载并校验）。

.PARAMETER NativeDocker
  使用本机原生 Docker Engine（Linux 主机，或已安装 Docker Desktop 且可用的 Windows）。

.PARAMETER SkipDeps
  跳过 Python 依赖安装、npm 安装与前端构建。

.PARAMETER SkipSandbox
  跳过隔离环境准备（只做依赖与预检；适合只浏览历史记录）。

.PARAMETER DownloadInputs
  自动下载并校验 WSL 3.0.3 MSI 与 Canonical Ubuntu 22.04 rootfs 到 .agentforge\sandbox-install。

.PARAMETER Launch
  部署成功后直接调用根目录 `一键启动.ps1` 启动工作台。

.PARAMETER CheckOnly
  只检查当前部署状态，不安装、不启动。

.PARAMETER Port
  -Launch 时使用的工作台端口，默认 8005。
#>
param(
    [switch]$NativeDocker,
    [switch]$SkipDeps,
    [switch]$SkipSandbox,
    [switch]$DownloadInputs,
    [switch]$Launch,
    [switch]$CheckOnly,
    [ValidateRange(1024, 65535)][int]$Port = 8005
)

$ErrorActionPreference = 'Stop'
$script:root = $PSScriptRoot

function Write-Step([string]$text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn([string]$text) { Write-Host "  [WARN] $text" -ForegroundColor Yellow }
function Write-Note([string]$text) { Write-Host "  $text" -ForegroundColor DarkGray }

# ---- locate code root (package layout: 05_项目代码; repository layout: backend/) ----
$codeRoot = $null
foreach ($candidate in @($script:root, (Join-Path $script:root '05_项目代码'))) {
    if (Test-Path -LiteralPath (Join-Path $candidate 'backend\agentforge\app.py')) {
        $codeRoot = $candidate
        break
    }
}
if (-not $codeRoot) { throw "找不到项目代码目录（需要 backend\agentforge\app.py）。" }

Write-Host "AgentForge 部署脚本 · 赛题一+奶龙队+AgentForge" -ForegroundColor White
Write-Host "代码目录：$codeRoot"

function Get-WslDistros {
    try { return @(wsl.exe --list --quiet 2>$null) | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ } }
    catch { return @() }
}

function Get-SandboxState {
    $state = [ordered]@{}
    $state.WslAvailable = $null
    $state.DistroPresent = $false
    $state.RuntimeInstalled = $false
    $state.ImagePresent = $false
    $state.RootfsPresent = Test-Path -LiteralPath (Join-Path $codeRoot '.agentforge\sandbox-install\ubuntu22.04-rootfs.tar.gz')
    $state.MsiPresent = Test-Path -LiteralPath (Join-Path $codeRoot '.agentforge\sandbox-install\wsl.3.0.3.0.x64.msi')
    try { wsl.exe --status 2>$null | Out-Null; $state.RuntimeInstalled = ($LASTEXITCODE -eq 0) } catch { $state.RuntimeInstalled = $false }
    $distros = @(Get-WslDistros)
    $state.WslAvailable = ($distros.Count -gt 0) -or $state.RuntimeInstalled
    $state.DistroPresent = ($distros -contains 'AgentForge')
    if ($state.DistroPresent) {
        & wsl.exe --distribution AgentForge --user root --exec docker image inspect agentforge-test:py313 --format '{{.Id}}' 2>$null | Out-Null
        $state.ImagePresent = ($LASTEXITCODE -eq 0)
    }
    return $state
}

function Show-State($state) {
    Write-Note "WSL 可用            : $(if ($state.WslAvailable) { '是' } else { '否' })"
    Write-Note "AgentForge 发行版   : $(if ($state.DistroPresent) { '已安装' } else { '未安装' })"
    Write-Note "验收镜像 py313      : $(if ($state.ImagePresent) { '已构建' } else { '未构建' })"
    Write-Note "WSL MSI（离线输入） : $(if ($state.MsiPresent) { '已存在' } else { '缺失' })"
    Write-Note "Ubuntu rootfs       : $(if ($state.RootfsPresent) { '已存在' } else { '缺失' })"
    $envFile = Join-Path $codeRoot '.env'
    if (Test-Path -LiteralPath $envFile) {
        $keyLine = Get-Content -LiteralPath $envFile -Encoding utf8 | Where-Object { $_ -match '^AGENTFORGE_LLM_API_KEY=' }
        $hasKey = $keyLine -and ($keyLine -replace '^AGENTFORGE_LLM_API_KEY=', '').Trim().Length -gt 0
        Write-Note "模型 API Key        : $(if ($hasKey) { '已填写（不显示内容）' } else { '未填写' })"
    } else {
        Write-Note "模型 API Key        : 缺少 .env（可由 setup.ps1 从 .env.example 生成）"
    }
}

function Set-EnvBackend([string]$backend) {
    $envPath = Join-Path $codeRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        Copy-Item -LiteralPath (Join-Path $codeRoot '.env.example') -Destination $envPath
    }
    $lines = @(Get-Content -LiteralPath $envPath -Encoding utf8)
    $found = $false
    $updated = foreach ($line in $lines) {
        if ($line -match '^AGENTFORGE_DOCKER_BACKEND=') {
            $found = $true
            "AGENTFORGE_DOCKER_BACKEND=$backend"
        } else {
            $line
        }
    }
    if (-not $found) { $updated = @($updated) + "AGENTFORGE_DOCKER_BACKEND=$backend" }
    Set-Content -LiteralPath $envPath -Value $updated -Encoding utf8
    Write-Ok "已同步 .env：AGENTFORGE_DOCKER_BACKEND=$backend"
}

# ================= 1. CheckOnly =================
if ($CheckOnly) {
    Write-Step '部署状态检查'
    $state = Get-SandboxState
    Write-Note "Python 虚拟环境     : $(if (Test-Path -LiteralPath (Join-Path $codeRoot '.venv\Scripts\python.exe')) { '已存在' } else { '缺失' })"
    Write-Note "前端构建产物        : $(if (Test-Path -LiteralPath (Join-Path $codeRoot 'frontend\dist\index.html')) { '已存在' } else { '缺失' })"
    Write-Note "Node.js             : $(try { (& node --version) } catch { '未安装' })"
    Show-State $state
    Write-Step '结论'
    if ($state.DistroPresent -and $state.ImagePresent) { Write-Ok '隔离环境已就绪；配置 .env 后可运行真实任务' }
    elseif ($state.DistroPresent) { Write-Warn '发行版已存在但验收镜像缺失；运行本脚本（不带 -SkipSandbox）补齐' }
    else { Write-Warn '隔离环境未安装；参照《部署与复现指南》首次部署步骤' }
    Write-Ok '检查完成（未做任何修改）。'
    exit 0
}

# ================= 2. dependencies =================
if (-not $SkipDeps) {
    Write-Step '安装 Python 依赖、前端依赖并构建前端（首次约 2–10 分钟）'
    & (Join-Path $codeRoot 'scripts\setup.ps1')
    if ($LASTEXITCODE -ne 0) { throw '依赖安装或前端构建失败，请查看上方输出。' }
    Write-Ok '依赖与前端构建完成'
} else {
    Write-Warn '已跳过依赖安装（-SkipDeps）'
}

# ================= 3. sandbox =================
if ($SkipSandbox) {
    Write-Warn '已跳过隔离环境准备（-SkipSandbox）'
} else {
    $state = Get-SandboxState
    if ($NativeDocker) {
        Write-Step '使用原生 Docker 构建验收镜像'
        Set-EnvBackend 'native'
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            Write-Warn '未找到 docker CLI；请先安装 Docker Engine（Linux）或 Docker Desktop（Windows）'
            exit 2
        }
        & (Join-Path $codeRoot 'scripts\setup-sandbox.ps1')
        if ($LASTEXITCODE -ne 0) { throw '原生 Docker 验收镜像构建失败。' }
        Write-Ok '原生 Docker 隔离环境就绪'
    } elseif ($state.DistroPresent -and $state.ImagePresent) {
        Write-Step '隔离环境已安装，启动 Docker Engine'
        & (Join-Path $codeRoot 'scripts\start-sandbox.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Docker Engine 启动失败。' }
        Write-Ok 'Docker Engine 与验收镜像均可用'
    } else {
        Write-Step '准备 WSL2 专用隔离环境'
        if ($DownloadInputs -and ((-not $state.RootfsPresent) -or (-not $state.MsiPresent))) {
            Write-Note '下载 WSL 3.0.3 MSI 与 Ubuntu 22.04 rootfs（约 700 MB，可能较慢）...'
            & (Join-Path $codeRoot 'scripts\download-sandbox-inputs.ps1')
            if ($LASTEXITCODE -ne 0) { Write-Warn '下载或校验失败，请按《部署与复现指南》手动下载。'; exit 2 }
            $state = Get-SandboxState
        }
        if (-not $state.RuntimeInstalled) {
            Write-Warn '未检测到可用的 WSL 运行时。首次安装需要管理员权限：'
            Write-Note '1) 以管理员身份打开 PowerShell，执行：'
            Write-Note "   powershell -ExecutionPolicy Bypass -File `"$codeRoot\scripts\install-wsl-runtime.ps1`""
            Write-Note '2) 若提示需要重启，请重启后回来继续。'
            if (-not $state.MsiPresent) { Write-Note '（若尚未下载 WSL MSI，先运行本脚本加 -DownloadInputs）' }
            exit 2
        }
        if (-not $state.RootfsPresent) {
            Write-Warn '缺少 Ubuntu 22.04 rootfs 文件：'
            Write-Note "   需要 .agentforge\sandbox-install\ubuntu22.04-rootfs.tar.gz"
            Write-Note '   运行本脚本加 -DownloadInputs 自动下载并校验，或按《部署与复现指南》手动下载。'
            exit 2
        }
        & (Join-Path $codeRoot 'scripts\setup-wsl-sandbox.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'WSL 发行版导入 / Docker Engine 安装 / 镜像构建失败。' }
        & (Join-Path $codeRoot 'scripts\start-sandbox.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Docker Engine 启动检查失败。' }
        Write-Ok 'WSL2 隔离环境与验收镜像就绪'
    }
}

# ================= 4. preflight =================
Write-Step '运行环境预检（不发送模型请求）'
$pyExe = Join-Path $codeRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $pyExe) {
    & $pyExe (Join-Path $codeRoot 'scripts\preflight.py')
    $preflightExit = $LASTEXITCODE
} else {
    $preflightExit = 127
    Write-Warn '缺少 Python 虚拟环境，未运行预检（先运行不带 -SkipDeps 的部署）'
}

Write-Step '部署结果'
if ($preflightExit -eq 0) {
    Write-Ok '模型配置与隔离环境均可用，可以运行真实任务'
} elseif ($preflightExit -eq 2) {
    Write-Warn '预检未完全通过：通常是 .env 未填写模型 Key 或 Docker 未就绪'
    Write-Note '界面与演示数据仍可浏览；真实运行请按《模型接入文档》填写 .env 后重跑 preflight.py'
} else {
    Write-Warn "预检异常退出码 $preflightExit"
}

if ($Launch) {
    Write-Step '启动工作台'
    & (Join-Path $script:root '一键启动.ps1') -Port $Port
    if ($LASTEXITCODE -ne 0) { exit 1 }
}
Write-Note "下一步：powershell -ExecutionPolicy Bypass -File `"$(Join-Path $script:root '一键启动.ps1')`""
if ($preflightExit -eq 0) { exit 0 }
elseif ($preflightExit -eq 2) { exit 2 }
else { exit 1 }
