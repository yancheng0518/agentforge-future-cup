# AgentForge 部署与复现指南

> 面向评审专家与复现者。项目：**AgentForge 可验证的多智能体开发编排工作台**（赛题一+奶龙队+AgentForge）。
> 仓库：<https://github.com/yancheng0518/agentforge-future-cup>（公开，`main`，tag `v1.2-submission-20261112`）
> 版本：2026-11-12

## 0.1 交付的脚本与文档一览

| 文件 | 作用 |
|---|---|
| `一键启动.ps1`（根目录） | **一键启动**：载入演示数据 → 装依赖/构建前端 → 启动 Docker → 预检 → 起服务 → 开浏览器（无 Key 可浏览） |
| `部署.ps1`（根目录） | **完整部署**：依赖 + 隔离环境安装/启动 + 预检，支持 `-NativeDocker`、`-DownloadInputs`、`-Launch`、`-CheckOnly` |
| `docs/model-access.md` | **模型接入文档**：自备 API Key、模型 ID、`.env` 字段、探针与错误分类、费用与安全 |
| `docs/deploy-and-reproduce.md` | 本文件：环境要求、部署与复现步骤、常见问题 |
| `scripts/setup.ps1` | 创建虚拟环境、安装锁定依赖、npm 安装与前端构建、生成 `.env` |
| `scripts/install-wsl-runtime.ps1` | 安装并校验 Microsoft WSL 运行时（**需要管理员**，`/norestart`，不主动重启） |
| `scripts/download-sandbox-inputs.ps1` | 下载并校验 WSL MSI 与 Ubuntu rootfs（约 700 MB） |
| `scripts/setup-wsl-sandbox.ps1` | 导入专用 `AgentForge` WSL 发行版、安装 Docker Engine、构建 `agentforge-test:py313` |
| `scripts/setup-sandbox.ps1` | 原生 Docker 环境下构建验收镜像 |
| `scripts/start-sandbox.ps1` | 启动已安装的 WSL Docker Engine 并检查镜像（不安装、不修改防火墙） |
| `scripts/preflight.py` | 只读预检：模型配置 + 隔离环境，不发送模型请求 |
| `scripts/probe_model.py` | 模型协议探针：验证工具调用与 JSON 输出（≤2 次请求，可能计费） |
| `scripts/workbench.ps1` / `scripts/backend.ps1` | 构建前端并启动单端口工作台 |

## 0. 两种复现目标

| 目标 | 需要什么 | 能验证什么 | 推荐入口 |
|---|---|---|---|
| **A. 界面与流程体验**（推荐先做） | 只需 Windows + 本压缩包 | 历史任务列表、主 Agent 决策、协作图回放、时间轴、Docker 验收记录、项目版本与交付页、关键截图 | 压缩包根目录 `一键启动.ps1`（自动载入演示数据） |
| **B. 完整闭环验证** | 模型 API Key + WSL2/Docker | 新建真实模型任务、真实 Docker 隔离验收、语义审查、反馈修正、平台测试 872+278 | 按第 3 节手动步骤；或同一脚本配置 `.env` 后运行 |

目标 A 不需要模型 Key 与 Docker：脚本会载入随包的演示数据，直接浏览已保存的真实运行证据。**注意**：没有 Key 时不能新建真实模型任务；没有 Docker 时新的候选验收会显示 blocked，这是设计中的明确阻塞而不是静默降级。

## 1. 环境要求

| 项 | 要求 | 本机实测版本 |
|---|---|---|
| 操作系统 | Windows 10/11 x64（或用 Linux + 原生 Docker） | Windows 11 |
| Python | 3.11+（推荐 3.13） | 3.13.5 |
| Node.js | 20+（推荐 24） | 24.14.1 |
| 隔离执行 | WSL2 + 专用 `AgentForge` 发行版中的 Docker Engine，或原生 Docker Engine | WSL 3.0.3 / Ubuntu 22.04 / Docker 29.9.3 |
| 磁盘 | ≥ 3 GB（依赖、镜像与演示数据） | — |
| 网络 | 首次安装依赖与构建镜像需要 | — |

## 2. 快速体验（压缩包，无需模型 Key）

1. 解压 `赛题一+奶龙队+AgentForge.zip` 到任意本地目录（路径可含中文）。
2. 在解压目录打开 PowerShell，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\一键启动.ps1
```

3. 脚本自动完成：**载入演示数据 → 创建 Python 虚拟环境并安装锁定依赖（首次约 2–10 分钟）→ `npm ci` + 构建前端 → 尝试启动 WSL Docker → 预检 → 启动工作台 → 打开浏览器**（默认 <http://127.0.0.1:8005>）。

4. 浏览器中可浏览：左侧「任务记录」10 条模型驱动运行 + 17 条旧工作台运行、「项目」4 个不可变版本项目；点开任一条运行可看主 Agent 决策、任务分工、Docker 验收、协作图与时间轴回放。

常用参数：

```powershell
# 只检查环境，不启动服务
powershell -ExecutionPolicy Bypass -File .\一键启动.ps1 -SelfTest
# 指定端口 / 跳过 Docker / 不自动开浏览器
powershell -ExecutionPolicy Bypass -File .\一键启动.ps1 -Port 8010 -SkipSandbox -NoBrowser
```

> 演示数据位于压缩包 `09_演示数据/.agentforge/`，首次启动时复制到 `05_项目代码/.agentforge/`；不会覆盖已有数据。

## 3. 完整复现（真实模型 + 隔离验收）

### 3.1 获取源码

```powershell
git clone https://github.com/yancheng0518/agentforge-future-cup.git
cd agentforge-future-cup
git checkout v1.2-submission-20261112
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

### 3.2 配置模型与隔离环境

复制 `.env.example` 为 `.env`（**不要提交 .env**）：

```dotenv
AGENTFORGE_LLM_BASE_URL=https://aigw.omniedu.com/v1
AGENTFORGE_LLM_MODEL=deepseek-v4.1-flash
AGENTFORGE_LLM_API_KEY=在本地填写
AGENTFORGE_DOCKER_BACKEND=wsl
AGENTFORGE_WSL_DISTRO=AgentForge
AGENTFORGE_TEST_IMAGE=agentforge-test:py313
```

首次在一台新机器上构建隔离环境（需要网络与管理员权限，约 10–30 分钟）：

```powershell
# 1) 下载并校验官方输入文件（WSL MSI + Ubuntu rootfs，约 700 MB）
powershell -ExecutionPolicy Bypass -File .\scripts\download-sandbox-inputs.ps1

# 2) 以管理员身份安装 WSL 运行时（会检查 MSI 哈希与 Microsoft 签名；不主动重启）
#    注意：必须以管理员身份打开 PowerShell 后执行
powershell -ExecutionPolicy Bypass -File .\scripts\install-wsl-runtime.ps1

# 3) 导入专用发行版、安装 Docker Engine、构建验收镜像
powershell -ExecutionPolicy Bypass -File .\scripts\setup-wsl-sandbox.ps1

# 或直接使用部署脚本（自动按顺序执行 1–3 与预检；-DownloadInputs 才会自动下载）
powershell -ExecutionPolicy Bypass -File .\部署.ps1 -DownloadInputs
```

> 已有原生 Linux Docker 的机器改用：`powershell -ExecutionPolicy Bypass -File .\部署.ps1 -NativeDocker`。
> 模型 Key 的获取、填写的变量、探针与错误分类见 **[模型接入文档](model-access.md)**。

之后每次启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-sandbox.ps1
.\.venv\Scripts\python.exe scripts\preflight.py      # 退出码 0 = 模型配置与 Docker 均可用
powershell -ExecutionPolicy Bypass -File .\scripts\workbench.ps1 -Port 8005
```

### 3.3 平台测试（不调用模型）

```powershell
.\.venv\Scripts\python.exe -m pytest -q      # 872 passed + 278 subtests
cd frontend; npm test; npm run build; cd ..
```

干净克隆复现记录（含 2026-11-12 实测结果）见 `submission/复现验证记录-20261112.md`。

## 4. 目录与数据

| 路径 | 说明 |
|---|---|
| `backend/agentforge/` | FastAPI、编排内核、调度、SQLite、沙箱、项目/Web 模块 |
| `frontend/` | React 工作台源码（构建产物在 `frontend/dist/`） |
| `tests/` | 平台回归测试（Stub/假传输，不调用真实模型） |
| `acceptance/`、`benchmarks/` | 独立验收与冻结任务 |
| `scripts/` | 安装、启动、预检、复现、对照实验脚本 |
| `.agentforge/`（运行时生成） | 运行数据库、事件、候选副本、日志（`.gitignore`，不入库） |
| `docs/` | 架构、验证、评测、环境与依赖说明 |
| `submission/` | 初赛材料（白皮书、方案书、PPT、证据索引等） |

## 5. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `preflight.py` 退出码 2 | 缺模型 Key（`.env`）或 Docker 不可用；按 3.2 修复。浏览历史记录不要求退出码 0 |
| WSL 报 `Stopped` | 运行 `scripts\start-sandbox.ps1` 启动发行版与 Docker |
| 端口被占用 | 使用 `-Port 8006` 等其他端口 |
| 启动后页面空白 | 前端未构建：`cd frontend; npm ci; npm run build` |
| 新建任务报「隔离环境不可用」 | Docker/镜像缺失，属明确阻塞；按 3.2 构建 `agentforge-test:py313` |
| `npm ci` 失败 | 检查网络与 Node 版本；项目锁定文件为 `frontend/package-lock.json` |
| 中文路径/换行 | 已按 UTF-8 处理；Windows 换行差异在 `.gitattributes` 中冻结证据文件 |

## 6. 安全与边界（如实声明）

- 仅监听 `127.0.0.1`，是**单机本地工具**：无多用户认证、无公网部署，请勿直接暴露公网。
- 候选 Python 只在 Docker 中执行（禁网、只读根文件系统、非 root、capabilities 丢弃、资源限额）；父子进程同容器同 UID，**不是面向任意恶意代码的强对抗沙箱**。
- 模型密钥只存服务端 `.env`，不进入前端、日志、归档或本压缩包。
- 真实模型运行可能产生费用；平台测试使用测试替身，不代表模型效果。

## 7. 一键启动脚本做了什么

| 步骤 | 行为 | 失败时 |
|---|---|---|
| 载入演示数据 | 压缩包布局下把 `09_演示数据/.agentforge` 复制到代码目录（仅当不存在运行库） | 跳过并继续 |
| 依赖检查 | 检查 `.venv`、Node、npm；缺失则执行 `scripts/setup.ps1` | 提示安装 Node/Python 后退出 |
| 前端 | 缺失 `node_modules` 执行 `npm ci`；缺失 `dist` 执行 `npm run build` | 报错并退出 |
| 隔离环境 | 尝试启动 WSL `AgentForge` Docker（可选） | 警告并继续（浏览模式） |
| 预检 | 运行 `scripts/preflight.py`（不发模型请求） | 退出码 2 时给出提示并继续 |
| 启动 | 新窗口运行 `scripts/backend.ps1`，轮询 `/api/health`，成功后打开浏览器 | 提示查看后端窗口错误 |
