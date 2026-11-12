<#
.SYNOPSIS
  AgentForge 一键启动脚本（未来杯赛题一参赛作品）。

.DESCRIPTION
  自动完成：载入演示数据（压缩包布局时）→ 检查/安装依赖 → 构建前端 → 启动 WSL Docker
  → 预检 → 启动本地工作台 → 打开浏览器。
  没有模型 API Key 时仍可浏览历史运行、协作图与项目版本；真实模型闭环需在 .env 配置 Key。
  本脚本不会安装 Docker/WSL，也不会修改系统防火墙或端口策略；仅监听 127.0.0.1。

.PARAMETER Port
  工作台端口，默认 8005。

.PARAMETER SkipSetup
  跳过依赖安装（已执行过 setup 时使用）。

.PARAMETER SkipSandbox
  跳过 WSL Docker 启动（只浏览历史记录时使用）。

.PARAMETER NoBrowser
  不自动打开浏览器。

.PARAMETER SelfTest
  只检查环境并打印结论，不启动服务。
#>
param(
    [ValidateRange(1024, 65535)][int]$Port = 8005,
    [switch]$SkipSetup,
    [switch]$SkipSandbox,
    [switch]$NoBrowser,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$script:root = $PSScriptRoot

function Write-Step([string]$text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn([string]$text) { Write-Host "  [WARN] $text" -ForegroundColor Yellow }

# ---- locate code root (package layout: 05_项目代码; repository layout: backend/) ----
$codeRoot = $null
foreach ($candidate in @($script:root, (Join-Path $script:root '05_项目代码'))) {
    if (Test-Path -LiteralPath (Join-Path $candidate 'backend\agentforge\app.py')) {
        $codeRoot = $candidate
        break
    }
}
if (-not $codeRoot) {
    throw "找不到项目代码目录（需要 backend\agentforge\app.py）。请将本脚本放在压缩包根目录或仓库根目录运行。"
}

Write-Host "AgentForge 多智能体开发编排工作台 · 一键启动" -ForegroundColor White
Write-Host "赛题一+奶龙队+AgentForge · 2026 第五届未来杯创新大赛" -ForegroundColor DarkGray
Write-Host "代码目录：$codeRoot"

# ---- 1. demo data (single-file package layout) ----
$demoSrc = Join-Path $script:root '09_演示数据\.agentforge'
$dataDir = Join-Path $codeRoot '.agentforge'
if ((Test-Path -LiteralPath $demoSrc) -and -not (Test-Path -LiteralPath (Join-Path $dataDir 'agentforge.sqlite3'))) {
    Write-Step '载入演示数据'
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    Copy-Item -Path (Join-Path $demoSrc '*') -Destination $dataDir -Recurse -Force
    Write-Ok "已载入历史运行/项目演示数据到 $dataDir"
} elseif (Test-Path -LiteralPath (Join-Path $dataDir 'agentforge.sqlite3')) {
    Write-Ok '已存在运行数据，跳过演示数据载入'
}

# ---- 2. prerequisites ----
Write-Step '检查运行环境'
$pyExe = Join-Path $codeRoot '.venv\Scripts\python.exe'
$needSetup = -not (Test-Path -LiteralPath $pyExe)
if (-not $needSetup) { Write-Ok 'Python 虚拟环境已存在' } else { Write-Warn '缺少 Python 虚拟环境（将执行 scripts\setup.ps1）' }
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
$npmCmd = Get-Command npm -ErrorAction SilentlyContinue
if ($nodeCmd) { Write-Ok "Node $(& node --version)" } else { Write-Warn '未找到 Node.js（前端构建需要）' }
if ($npmCmd) { Write-Ok 'npm 可用' } else { Write-Warn '未找到 npm' }

if ($needSetup -and -not $SkipSetup) {
    if (-not $nodeCmd -or -not $npmCmd) { throw '需要先安装 Node.js 24（含 npm），再重新运行本脚本。' }
    Write-Step '安装依赖并构建前端（首次约 2–10 分钟）'
    & (Join-Path $codeRoot 'scripts\setup.ps1')
    if ($LASTEXITCODE -ne 0) { throw '依赖安装或前端构建失败，请查看上方输出。' }
}

if (-not $SkipSetup) {
    if (-not (Test-Path -LiteralPath (Join-Path $codeRoot 'frontend\node_modules'))) {
        Write-Step '安装前端依赖'
        Push-Location (Join-Path $codeRoot 'frontend')
        try { & npm ci; if ($LASTEXITCODE -ne 0) { throw 'npm ci 失败。' } } finally { Pop-Location }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $codeRoot 'frontend\dist\index.html'))) {
        Write-Step '构建前端'
        Push-Location (Join-Path $codeRoot 'frontend')
        try { & npm run build; if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' } } finally { Pop-Location }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $codeRoot '.env'))) {
        Copy-Item -LiteralPath (Join-Path $codeRoot '.env.example') -Destination (Join-Path $codeRoot '.env')
        Write-Warn '已从 .env.example 创建 .env；如需真实模型运行，请填写 AGENTFORGE_LLM_API_KEY'
    }
}

# ---- 3. sandbox (best effort) ----
$sandboxReady = $false
if (-not $SkipSandbox) {
    Write-Step '启动 WSL Docker 隔离环境（可选，失败不影响浏览历史记录）'
    try {
        $distros = @(wsl.exe --list --quiet 2>$null) | ForEach-Object { ($_ -replace "`0", '').Trim() }
        if ($distros -contains 'AgentForge') {
            & (Join-Path $codeRoot 'scripts\start-sandbox.ps1')
            if ($LASTEXITCODE -eq 0) { $sandboxReady = $true; Write-Ok 'Docker 隔离环境已就绪' }
            else { Write-Warn 'Docker 启动失败；可浏览演示数据，真实验收将显示阻塞' }
        } else {
            Write-Warn '未安装专用 WSL 发行版 AgentForge；请参考 08_部署与复现指南 的“完整复现”一节'
        }
    } catch {
        Write-Warn "Docker 检查失败：$($_.Exception.Message)"
    }
}

# ---- 4. preflight ----
Write-Step '运行环境预检（不发送模型请求）'
& $pyExe (Join-Path $codeRoot 'scripts\preflight.py')
$preflightExit = $LASTEXITCODE
if ($preflightExit -eq 0) { Write-Ok '模型配置与隔离环境均可用' }
elseif ($preflightExit -eq 2) { Write-Warn '预检未完全通过（缺模型 Key 或 Docker）；仍可浏览已有运行记录' }
else { Write-Warn "预检退出码 $preflightExit" }

if ($SelfTest) {
    Write-Step '自检结果'
    Write-Host "  代码目录        : $codeRoot"
    Write-Host "  演示数据        : $(if (Test-Path -LiteralPath (Join-Path $dataDir 'agentforge.sqlite3')) { '已载入' } else { '无' })"
    Write-Host "  Python 虚拟环境 : $(if (Test-Path -LiteralPath $pyExe) { '就绪' } else { '缺失' })"
    Write-Host "  前端构建        : $(if (Test-Path -LiteralPath (Join-Path $codeRoot 'frontend\dist\index.html')) { '就绪' } else { '缺失' })"
    Write-Host "  Docker 隔离     : $(if ($sandboxReady) { '就绪' } else { '不可用/跳过' })"
    Write-Host "  预检退出码      : $preflightExit"
    Write-Host '  自检完成，未启动服务。' -ForegroundColor Green
    exit 0
}

# ---- 5. already running? ----
$url = "http://127.0.0.1:$Port"
try {
    $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 3
    if ($health.status -eq 'ok') {
        Write-Ok "工作台已在运行：$url"
        if (-not $NoBrowser) { Start-Process $url }
        exit 0
    }
} catch { }

# ---- 6. start backend in a new window ----
Write-Step '启动本地工作台'
$backendScript = Join-Path $codeRoot 'scripts\backend.ps1'
$command = "& '$backendScript' -Port $Port"
Start-Process -FilePath 'powershell.exe' -WorkingDirectory $codeRoot `
    -ArgumentList '-NoProfile', '-NoExit', '-ExecutionPolicy', 'Bypass', '-Command', $command | Out-Null
Write-Host "  等待服务就绪（$url）..."
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 800
    try {
        $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 2
        if ($health.status -eq 'ok') { $ready = $true; break }
    } catch { }
}
if (-not $ready) {
    Write-Warn "服务未在预期时间内就绪；请查看新打开的 PowerShell 窗口中的错误输出。"
    exit 1
}
Write-Ok "工作台已启动：$url"
if (-not $NoBrowser) { Start-Process $url }

Write-Host ""
Write-Host "使用说明：" -ForegroundColor White
Write-Host "  · 左侧「任务记录 / 项目」可浏览已保存的真实运行、协作图与项目版本（无需模型 Key）。"
Write-Host "  · 新建真实模型任务需要 .env 中 AGENTFORGE_LLM_API_KEY，并需要 Docker 隔离环境。"
Write-Host "  · 关闭新打开的 PowerShell 窗口或按 Ctrl+C 即可停止服务。"
Write-Host "  · 仅监听 127.0.0.1，不是公网服务。"
