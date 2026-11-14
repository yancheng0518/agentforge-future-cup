# AgentForge 部署与复现指南

> 面向评审专家与复现者。项目：**AgentForge 可验证的多智能体开发编排工作台**（赛题一+奶龙队+AgentForge）。
> 仓库：<https://github.com/yancheng0518/agentforge-future-cup>（公开，`main`，tag `v1.3-submission-20261114`）
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

## 0.2 复现范围与前置条件（评委须知）

「评委自备 API Key 后能复现到什么程度」的如实说明：

| 复现等级 | 前置条件 | 可复现内容 | 不可复现 / 不确定 |
|---|---|---|---|
| **L1 浏览演示数据**（零成本） | 只需解压 | 历史运行、主 Agent 决策、协作图回放、时间轴、Docker 验收记录、项目版本与交付页、截图与视频 | 不能新建真实模型任务 |
| **L2 平台测试**（零模型成本） | Python 3.13 + 依赖 | 后端 872+278 测试、前端 110 测试、构建 | 不验证模型效果 |
| **L3 真实模型 + Docker 闭环**（需 Key + Docker） | 模型 Key（OpenAI 兼容） + Docker 隔离环境 | 新建任务、动态编排、真实 Docker 隔离验收、语义审查、失败反馈与有限修正、项目持续开发 | 具体 tokens、耗时、修正轮次随模型与随机性变化，不能保证与历史运行逐字节一致 |
| **L4 对照/复现包实验**（可选、成本高） | 同上 + 任务控制证据 | 单 Agent / 固定串行 / 动态并行对照，预设缺陷实验 | 需要较多 token 与时间；历史批次不重跑 |

**模型兼容性（关键）**：Key 对应的服务必须兼容 OpenAI `POST /chat/completions`，并支持 **tools（Function Calling）** 与 **`response_format=json_object`**。本项目实测 `deepseek-v4.1-flash`；换用其他模型/供应商时，先用 `scripts/probe_model.py` 验证——不通过时平台会明确报错，不会把不兼容当作成功。

**隔离环境**：Windows 首选 WSL2 专用发行版（`部署.ps1 -DownloadInputs` 引导安装）；已有 Docker Desktop（Linux 容器）或其他原生 Docker 的机器可用 `部署.ps1 -NativeDocker`（需 `docker` CLI 可用）。macOS 与未安装 Docker 的环境仅能完成 L1/L2，不能完成真实候选的隔离验收。

**首次成本与时间**：依赖安装约 2–10 分钟；WSL 运行时 + 发行版 + 验收镜像约 10–30 分钟（需管理员与网络，首次下载约 700MB）；镜像构建需联网拉取固定 base digest `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`（2026-11-14 已从浮动 tag 改为 digest 锁定；重建后的验收镜像 ID `sha256:a6bd11ad555ce7120a31432d37209ae7050d0079f8a15f8e7bbd2ae1cac1f296`）。真实运行按模型供应商计费。

**已知不确定项（如实标注）**：换机从零复现未实测；WSL 预览生命周期历史上出现过退出失败（已修复并保留记录）；模型输出具有随机性；本项目不保证与历史批次在 tokens/耗时/修正轮次上逐项一致。

**建议验证顺序**：L1 → L2 → L3；只在需要验证真实闭环时配置 Key。完整证据索引见 `07_其他说明与证据/证据索引.md`。

## 1. 环境要求

| 项 | 要求 | 本机实测版本 |
|---|---|---|
| 操作系统 | Windows 10/11 x64（或用 Linux + 原生 Docker） | Windows 11 |
| Python | 3.11+（推荐 3.13） | 3.13.5 |
| Node.js | 20+（推荐 24） | 24.14.1 |
| 隔离执行 | WSL2 + 专用 `AgentForge` 发行版 Docker Engine（Windows 首选）；或 Docker Desktop / 原生 Docker Engine（`-NativeDocker`） | WSL 3.0.3 / Ubuntu 22.04 / Docker 29.9.3 |
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
git checkout v1.3-submission-20261114
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
