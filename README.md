# AgentForge 多智能体开发工作台

未来杯赛题一的本地原型。按已确认的《项目策划案.md》实施，React + FastAPI + SQLite。暂定项目名称，不代表最终参赛名称。

**2026-11-09 项目持续开发**：新增“项目”入口，导入现有标准库 Python 源码后，在同一项目继续提出需求、保留回归验收和不可变版本。主 Agent 可直接编辑、读文件、搜索及在 Docker 运行应用，也可按需要委派子 Agent。配置与项目资料按版本冻结并真实进入模型请求。机制与边界见 [项目工作区](docs/project-workspaces.md)，逐阶段上传与验证见 [提交记录](docs/github-progress-20261109.md)。

本轮同一记账项目经两次真实模型迭代保存到版本 3：13/13、20/20 Docker 验收通过，18 次请求、136,769 tokens、1 次格式重试、0 次自然代码失败或修复。第二轮主 Agent 自行运行 5 次 Docker 应用工具。详细证据与复现见 [连续开发验证](docs/project-workspaces-verification.md) 和 [归档恢复](docs/project-workspaces-restore.md)。

**模型驱动编排已接入浅色对话界面（2026-11-08）**：直接输入需求，主 Agent 准备验收、动态拆任务、分配子 Agent，并按执行反馈调整计划。新任务不再选择固定角色或四类 Todo 任务。入口与边界见 [动态编排内核与复现](docs/autonomous-orchestration.md)，界面实测及全部失败见 [对话闭环验证](docs/autonomous-chat-verification.md)。旧实验与旧任务记录保留，旧“动态并行”对照不是新内核效果证据。

**现已加入“查看协作图”与事件回放**：点击节点查看分工、指令、文件和真实 Docker 回执；时间轴支持逐步、播放、暂停和回到实时。已用新的文本统计任务完成真实界面闭环：2 个动态子任务、9 次请求、12/12 生成验收及 8/8 预声明独立 Docker 检查通过。首轮成功，无自然代码修正。使用、失败分类和完整证据见 [协作图验证与复现](docs/collaboration-graph-verification.md)。

## 保留的旧工作台与实验功能

- 在工作台用一句需求启动开发或审查，查看实际进度与结果；按需打开候选差异、验证、后台事件、知识引用与快照。
- 创建、编辑、启停 Agent；每次保存产生版本，历史运行固定使用当时版本。
- 按能力、工具权限、优先级、依赖与负载调度，测试和初步审查并行，失败反馈最多修正两轮。
- 在已验证隔离条件下调用独立验收工具；没有 Docker 时明确阻塞。
- 保存节点输出，恢复失败/阻塞运行；候选变化会使旧验证失效。
- 选择完成状态、原子批量编辑、优先级查询、JSON 合并四项任务，冻结需求、基线和各自十项独立验收，保留每轮候选与完整运行导出。
- 仅在隔离验收及审查通过后允许用户明确确认应用；检查目标基线和已验证候选哈希。

**四项真实模型开发任务已通过 Docker 验收与审查。** 2026-11-08，平台模型 `deepseek-v4.1-flash` 在修复后的独立批次完成 43 次请求、313,562 tokens，四项各 10/10 验收通过。其中 3 次 JSON 格式重试成功，代码修正轮次均为 0；格式恢复不能算代码自修正。此前八次运行的全部失败和成功仍保留。任务、预设缺陷修正实验与完整复现命令见 [多任务评测](docs/multi-task-evaluation.md)，历史运行见 [运行证据](docs/runtime-evidence.md)，整体状态见 [进度记录](docs/implementation-status.md)。专用 WSL2 Docker 环境的安装证据见 [隔离环境配置](docs/sandbox-setup.md)。

## 本机环境与安装

已验证开发环境：Windows 11、Python 3.13.5、Node 24.14.1、npm 11.11.0、Git 2.51.0。首次依赖安装锁定在 `backend/requirements-lock.txt` 和 `frontend/package-lock.json`。没有全局改动 npm 缓存，前端缓存使用项目 `.npm-cache`。

PowerShell 在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

这里只对该脚本进程指定执行策略，不修改系统策略；也可按脚本内容逐条执行。脚本创建本地虚拟环境、安装锁定依赖并构建前端；不安装 Docker、WSL，不覆盖已有 `.env`。

## 启动

本机使用 WSL Docker 后端，先按下方“隔离验收”配置 `.env`。Windows 重启或发行版停止后，先启动已安装的专用 Docker 服务并检查环境：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-sandbox.ps1
.\.venv\Scripts\python.exe scripts\preflight.py
```

推荐在一个终端中启动构建后的工作台：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\workbench.ps1
```

访问 <http://127.0.0.1:8000>，界面与 API 使用同一端口，API 文档在 <http://127.0.0.1:8000/docs>；按 Ctrl+C 停止。仅监听本机，没有部署公网，也不是多人服务。该方式避开 Windows/WSL 可能保留的 `5173` 端口，不修改系统端口保留或防火墙策略。

**本轮已启动的最终服务是 <http://127.0.0.1:8002>**，旧端口服务仍保留。需要自行重新启动同一端口时，先在该服务所属终端按 Ctrl+C，再执行 `powershell -ExecutionPolicy Bypass -File .\scripts\workbench.ps1 -Port 8002`。默认脚本仍使用 8000；不要重复启动占用同一端口。

前端开发时仍可分别运行 `scripts/backend.ps1` 和 `scripts/frontend.ps1`，使用 `http://127.0.0.1:5173` 的热更新界面；若该端口报告 EACCES，请使用上方单端口方式。UI 代码更新后重新运行 `npm run build` 即可更新构建界面。

Linux 复现可用 `python3 -m venv .venv`、`.venv/bin/python -m pip install -r backend/requirements-lock.txt`、`cd frontend && npm ci && npm run build`，然后运行 `uvicorn agentforge.app:app --app-dir backend --host 127.0.0.1 --port 8000` 和前端 `npm run dev`。**Linux 新机复现尚未实测。**

## 在界面里使用

继续已有软件：打开左侧“项目”→“导入项目”→选择 Python 文件或文件夹→输入改进需求。运行结束后回到项目查看新版本、代码、测试与运行历史。“运行程序”选择入口和逐行参数，在 Docker 中使用该项目的独立持久数据。当前运行限制 15 秒、32 个数据文件/64 KiB；不支持常驻服务。版本和应用回执均可检查，不把模型总结当作测试结果。

1. 打开工作台，直接描述需要的标准库 Python 功能、输入输出和边界要求。例如“制作 CSV 支出解析与分类汇总库，金额精确到分，非法数据报错”。
2. 点击发送或按 Ctrl+Enter；Enter 换行。新入口使用真实模型与 Docker，缺少配置或隔离环境时会明确阻塞。
3. 主 Agent 先拟定并审查验收方案，再根据需要直接工作或动态分配子 Agent。已有项目支持主 Agent 直接编辑和应用工具；独立“新任务”仍走动态委派路径。页面显示实际任务、执行结果、Docker 测试、审查及修正记录；离开页面不会取消后台任务。
4. 点击“查看协作图”查看真实分工与反馈；点击节点看详情，使用时间轴回放运行。播放仅浏览已保存事件，不产生新模型调用；初始脚手架失败、AI 验收失败和格式重试分别标注。
5. 完成后查看项目文件、验证结果，下载项目 ZIP 或完整运行记录。结果需要补充时点击“补充需求并新建任务”，会保留原运行并创建独立任务。

左侧“新任务”开始独立需求，“搜索任务”或 Ctrl+K 查找新旧历史。“历史 Agent 设置”仅供旧工作流使用，不决定新任务的子 Agent。原有恢复、导出及应用功能保留在旧记录中。

当前支持标准库 Python 多模块项目和 JSON 可传递的函数输入输出，不是通用网页或任意仓库开发器。范围不清或超出能力时会请求补充说明。AI 生成验收、语义审查均可能出错，测试通过不等于全部业务正确；界面标注验收来源及待复核原因。“新任务”使用独立工作区；“项目 → 继续开发”继承选定基线源码和原有回归验收。旧界面实测留存在 [历史交互验证](docs/conversation-ui-verification.md)。

## 模型配置与实测

复制 `.env.example` 到 `.env`，仅在本地填写：

```dotenv
AGENTFORGE_LLM_BASE_URL=https://aigw.omniedu.com/v1
AGENTFORGE_LLM_MODEL=deepseek-v4.1-flash
AGENTFORGE_LLM_API_KEY=在本地填写
```

准确模型 ID 来自平台“模型说明”，不是展示名称。不要将密钥发到聊天、前端或仓库。项目不会自动使用其他应用的 DeepSeek 环境变量。修改配置后重启后端。

```powershell
.\.venv\Scripts\python.exe scripts\preflight.py
.\.venv\Scripts\python.exe scripts\probe_model.py
```

`preflight.py` 只检查配置和 Docker，不发模型请求；条件不足退出码 2。`probe_model.py` 会发送最多两次小请求，先验证只读函数工具调用，再验证 JSON 输出。可能计费；不假定页面上的 0 元或资源包状态就是免费承诺。输出脱敏证据到 `.agentforge/model-probe.json`，退出码 0 才是该次探测通过。费用、账户授权和限流实际表现仍需平台确认。

当前配置的 `deepseek-v4.1-flash` 已通过真实协议探针：2 次请求、564 tokens，只读工具调用与 JSON 输出均成功，证据在 `.agentforge/model-probe-flash.json`。工作台环境详情中的“已读取本地配置”仅表示配置存在；是否实际调用成功应查看对应运行或探针证据。

历史上旧 Key 和新建 `AgentForge-dev` Key 都曾返回 403，且可访问模型列表为空。用户充值后，同一账户的可访问模型列表返回 8 条；原 `deepseek-v4-pro` 不在其中，调用变为 503“没有可用模型渠道”。因此已按可访问列表中的准确 ID 切换到 `deepseek-v4.1-flash` 并实测成功。上述变化是本账户的观测结果，不代表平台所有模型都必须购买资源包或都已可用。历史记录及错误分类见 [模型诊断](docs/model-diagnosis.md)。

## 隔离验收

候选 Python **不会在宿主机执行**。本机已安装 WSL 3.0.3，专用 `AgentForge` Ubuntu22.04 发行版中的 Docker Engine 29.9.3 已通过实际运行检查。发行版磁盘在 `D:\未来杯\.agentforge\wsl\AgentForge`，项目映射为 `/mnt/d/未来杯`；没有升级或重启 Windows，也没有开放 Docker TCP 控制端口。

本机 `.env` 使用以下配置；修改后重启后端：

```dotenv
AGENTFORGE_DOCKER_BACKEND=wsl
AGENTFORGE_WSL_DISTRO=AgentForge
AGENTFORGE_TEST_IMAGE=agentforge-test:py313
```

日常启动和实测：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-sandbox.ps1
.\.venv\Scripts\python.exe scripts\preflight.py
.\.venv\Scripts\python.exe scripts\probe_sandbox.py --include-timeout
```

首次安装需按 [隔离环境配置](docs/sandbox-setup.md) 校验官方 WSL MSI 和 Ubuntu rootfs，再执行 `scripts/setup-wsl-sandbox.ps1` 导入专用发行版、安装官方 Docker Engine 并构建镜像。已有原生 Linux 容器 Docker 的其他机器可设置 `AGENTFORGE_DOCKER_BACKEND=native`，使用 `scripts/setup-sandbox.ps1` 构建。本机 Win11 build 22000 不满足当前 Docker Desktop 要求，本次已实际验证的是 WSL 内 Engine 路径。

当前本地验收镜像 ID 为 `sha256:1edc3cba75dd1917bb78226e897fdb801a0272ea2e02319345de0bd3b8cae225`。本次构建实际解析的基础镜像为 `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`；`sandbox/Dockerfile` 的 `FROM` **仍使用浮动标签** `python:3.13-slim`，记录 digest 不代表已锁定构建。正式提交前仍须固定基础镜像 digest 并在新环境复现。

运行容器关闭网络、只读根目录和代码/验收挂载、非 root、丢弃 capabilities、限制内存/CPU/进程数，外层 25 秒超时并清理容器。独立验收在容器父进程中执行，候选只在子进程中通过受限 JSON 协议提供 TodoStore 行为；候选标准输出不会作为测试结果直接转发。返回引用变更通过代理转发，避免拷贝语义被序列化掩盖。

2026-11-08 已真实执行六项合成候选探针：正确参考实现通过十项独立验收，缺失筛选、跨实例共享状态、返回可变引用、伪造成功标准输出、忙循环超时均被拒绝；探针后残留测试容器为0。原始记录在 `.agentforge/sandbox-probe.json`，说明见 [运行证据](docs/runtime-evidence.md)。这些探针的模型调用数为0，不能作为真实模型开发或自我修正的证据。

父子进程仍处于同一容器、同一 UID；这不是面向任意恶意代码的公开沙箱或强防作弊方案，仅支持受控示例项目。

## 主演示复现

保持 `examples/todo/todo.py` 为初始示例，完成模型及 Docker 检查并启动后端后，在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe scripts\run_live_demo.py --workflow development
```

脚本通过本地 API 新建真实模式工作流，默认需求为“增加任务完成状态，并支持按照完成状态筛选任务。”；持续读取真实后台状态并导出 `.agentforge/evidence/live-<运行ID>.json`。退出码 0 表示工作流成功且隔离验收通过。它不会植入缺陷，也不会应用候选代码。2026-11-08 实测为首轮成功，`repair_observed=false`；可以加 `--require-repair` 检查是否确实出现过修正，首轮成功而没有修正时退出码为 3，不会为了演示伪造失败。也可在工作台输入相同需求，在“真实模型”模式发送。

第二模板可用以下命令复现：

```powershell
.\.venv\Scripts\python.exe scripts\run_live_demo.py --workflow review
```

“代码审查”只读取原始目标副本并生成检查结果，不生成或应用候选修改。当前初始 Todo 示例不满足新需求，因此 2026-11-08 的真实审查运行经过 7 次模型请求后返回 `needs_human`，脚本退出码为 2；这是发现问题的结果，不能标为验收通过。隔离条件缺失时，两种工作流都应阻塞。

独立验证“真实测试失败 → 真实模型修复 → 容器复验 → 真实审查”的命令为：

```powershell
.\.venv\Scripts\python.exe scripts\probe_live_repair.py --max-repairs 2 --review
```

该脚本在独立运行副本中植入明确标注的缺失筛选实现，先确认两项筛选验收失败，再把真实失败反馈交给真实修复 Agent，最多修正两轮，复验通过后调用真实审查 Agent。2026-11-08 已实测通过：4 次模型请求、16,883 tokens，合成缺陷的两项失败经真实模型修复后，十项容器验收全部通过，真实审查也通过。证据在 `.agentforge/live-repair-probes/20261108T080014Z-d3e46e/evidence.json`；重跑会创建新的带时间目录。**这是合成缺陷的真实修复实验，不是原始开发自然出错，也不是主工作流自动修正的证据。** 脚本不改动初始示例，不自动应用结果。

工作台仍保留“模拟演示”用于确定性演示：首轮故意遗漏筛选、固定修正输出，模型调用计数为 0；文件、差异、SQLite 记录和 Docker 验收是真实生成的。模拟模式的结果必须与上述真实运行分开记录，具体证据见 [运行证据](docs/runtime-evidence.md)。

开发工作流全部验收与审查通过后，可在工作台查看差异，点击应用并明确确认。平台不自动提交或推送 Git。

## 平台测试

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1
```

`pytest.ini` 仅收集 `tests/`。其中 StubRuntime/模拟网络用于测试编排、权限、失败、循环与恢复；不是模型效果评测，不执行候选代码。`acceptance/` 是另外维护的十项独立验收测试，仅由 Docker 运行。

四项真实任务评测、预设缺陷的独立对照及主工作流修正实验见 [多任务评测与复现](docs/multi-task-evaluation.md)。真实开发批次、格式重试、自然代码失败和预设缺陷分别记录，不能混为自修正成绩。

同模型、同任务、同预算上限的三组对照由 `scripts/evaluate_comparison.py` 执行：单 Agent 直接开发、固定串行多 Agent、动态并行多 Agent。统一功能终点为冻结 Docker 验收，原生审查门控另记；所有计划试次和失败均保留。设计、实际结果与命令见 [同期对照实验](docs/comparison-evaluation.md)。该脚本直接使用独立运行数据库，不要求启动 API 或前端。

已完成 24 个原计划槽位：功能通过分别 6/8、6/8、8/8，完整流程分别 6/8、6/8、7/8；保留 4 次 HTTP 429、13 次格式重试及 1 次本地记录中断。追加阶段只补原先未提交的 9 次，没有重测失败。复现包为 `output/agentforge-comparison-reproduction-20261108.zip`，历史多任务包保持原样。

换机复现、正式材料与视频尚未完成，不能从平台单元测试或小样本对照推导普遍效果提升。

## 目录与数据

- `backend/agentforge`：API、SQLite、模板、调度与工具/模型运行时。
- `frontend`：React 工作台，全部执行数据来自后端。
- `examples/todo`：目标示例；`acceptance`：独立验收；`knowledge`：本地文本资料。
- `benchmarks/tasks`：三项扩展任务的冻结契约与验收；`benchmarks/reference` 和 `benchmarks/synthetic` 仅用于维护者对照，不提供给真实开发模型。
- `.agentforge`：SQLite、运行副本、探测证据（Git 忽略）。
- `docs`：架构、环境、实现状态与依赖说明。

知识导入方式：由维护者向 `knowledge/` 加入 UTF-8 Markdown 文件，再在界面刷新/检索。当前关键词检索最多返回三条，不是向量数据库。工作流通过 `definitions.py` 代码配置；规划 Agent 输出提供给后续 Agent，但不会任意重写执行图。没有完整拖拽编排器。

本轮开发由 AI 协助生成实现、测试和文档；团队需要理解、自行复核，并在比赛材料中如实披露。第三方依赖及自定义实现见 [依赖说明](docs/third-party.md) 与 [架构说明](docs/architecture.md)。
