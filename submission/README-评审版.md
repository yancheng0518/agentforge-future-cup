# AgentForge 多智能体软件开发工作台 · 评审版说明

| 项目 | 内容 |
| --- | --- |
| 项目名称 | AgentForge（暂定名，最终提交名称以官方报名信息为准） |
| 参赛赛题 | 未来杯赛题一：多智能体软件开发工作台 |
| 队伍名称 | 奶龙队 |
| 项目负责人 | 王拙诚 |
| 参赛学校 | 湖北工业大学工程技术学院 |
| 参赛成员 | 王拙诚、吴捍卫、黄浩 |
| 指导教师 | 刘浪 |
| 队伍编号 | 2117173210695860225 |
| 代码版本 | `v1.2-submission-20261112`（tag），`main` 分支 |
| 文档日期 | 2026-11-12 |

## 1. 三句话定位

1. AgentForge 是一个**可复用的多智能体编排平台**，第一套行业应用场景是“用 AI 开发并持续改进小型软件项目”，而不是一个写死流程的单示例演示。
2. 用户只输入自然语言需求：主 Agent 先拟定并审查验收方案，再动态拆分任务，由调度器按能力、工具权限、依赖、优先级与负载分派子 Agent；候选代码只在受控 Docker 中执行**独立验收**，真实失败会反馈给修正 Agent 并最多循环两轮。
3. 规划、分工、工具调用、Docker 回执、修正与审查全过程持久化并可在界面与协作图中回放；验收未通过、审查否决或资源不足的运行明确标为待处理，不以“生成了代码”冒充成功。

## 2. 五分钟快速体验 / 复现

已验证环境：Windows 11、Python 3.13.5、Node 24.14.1、npm 11.11.0、Git 2.51.0。首次安装依赖需要额外时间（约 3–10 分钟，取决于网络与镜像源），之后按以下五步即可体验。

0. **一键启动（压缩包，最简）**：解压后在该目录执行

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\一键启动.ps1
   ```

   自动载入演示数据、安装依赖、构建前端、尝试启动 Docker 并打开浏览器；无模型 Key 时可浏览已保存的历史运行、协作图与项目版本。完整部署用 `部署.ps1`，模型 Key 接入见 `08_部署与复现指南/模型接入文档.md`。

1. **手动安装依赖（项目根目录，PowerShell）**

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
   ```

   脚本创建本地虚拟环境、安装锁定依赖并构建前端；不安装 Docker/WSL，不覆盖已有 `.env`。

2. **配置 `.env`**（由 `.env.example` 复制，仅本地填写，不要提交）

   ```dotenv
   AGENTFORGE_LLM_BASE_URL=https://aigw.omniedu.com/v1
   AGENTFORGE_LLM_MODEL=deepseek-v4.1-flash
   AGENTFORGE_LLM_API_KEY=在本地填写
   AGENTFORGE_DOCKER_BACKEND=wsl
   AGENTFORGE_WSL_DISTRO=AgentForge
   AGENTFORGE_TEST_IMAGE=agentforge-test:py313
   ```

   **模型 Key 说明：** 真实模型闭环需要可用 Key，评委若无 Key 则无法发送真实请求。此时可查看**已有运行记录与示例项目**（若拿到的是包含 `.agentforge/` 运行数据库的本机工作区；代码仓库本身不包含该目录），或直接阅读 `docs/`、`output/` 中的脱敏证据与 `submission/证据索引.md`。旧工作台的“模拟演示”只是**旧实验**，不属于本次真实模型闭环证据，不能当作成功案例。

3. **启动隔离环境并预检**

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\start-sandbox.ps1
   .\.venv\Scripts\python.exe scripts\preflight.py
   ```

   `preflight.py` 只检查配置与 Docker，不发送模型请求；条件不足时退出码为 2。

4. **启动工作台并打开界面**

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\workbench.ps1 -Port 8005
   ```

   访问 <http://127.0.0.1:8005>（默认脚本端口为 8000；最近一次本机验证使用 8005；服务未运行时先启动）。仅监听本机，不是公网或多人服务。

5. **运行测试（可选，用于核对交付质量）**

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q      # 后端：872 passed, 278 subtests passed
   cd frontend
   npm test                                     # 前端：110 passed
   npm run build                                # TypeScript 检查 + Vite 生产构建
   ```

## 3. 架构一览

- **前端**：React 19 + TypeScript + Vite 工作台，构建产物由后端单端口托管；包含对话、任务/运行详情、协作图事件回放、项目版本与交付页、Web 预览 iframe。
- **后端**：FastAPI + SQLite，提供 Agent/工具/知识配置、工作流模板、调度器、运行事件账本、验收门控、快照与恢复、导出与恢复包生成。
- **模型接入**：可替换适配器，走 OpenAI 兼容 `POST /chat/completions`，支持只读工具调用与 JSON 结构化输出；Key 只保存在服务端环境变量。
- **隔离执行**：WSL2 专用 `AgentForge` 发行版内的 Docker Engine 29.9.3；验收容器关闭网络、只读根文件系统、非 root、丢弃 capabilities、限制内存/CPU/进程数，外层 25 秒超时并清理容器。候选 Python 不在 Windows 宿主机执行。
- **验证闭环**：候选源码哈希 → 冻结依赖与独立 Docker 验收 → 真实审查联合门控 → 失败反馈修正（≤2 轮）；候选变化会使旧验收失效，未通过则保留待处理。
- **证据留存**：每次运行保存事件、步骤、工具回执、候选哈希与测试输出；可按批导出复现包（ZIP + SHA256）。

## 4. 功能与赛题要求对照

| 赛题要求 | AgentForge 实现 | 主要证据 |
| --- | --- | --- |
| Agent 定义与配置 | 新增/编辑/启停 Agent，配置角色、能力标签、提示词、模型与可用工具；每次保存生成版本，历史运行固定当时配置 | `docs/architecture.md`、`docs/implementation-status.md` |
| 工作流编排 | 支持顺序、并行、条件判断、有限循环与子工作流；提供“开发”和“审查”两套模板及“项目持续开发”路径；代码化配置 + 可视化事件回放 | `docs/autonomous-orchestration.md`、`docs/collaboration-graph-verification.md` |
| 任务调度与执行 | 先按能力与工具权限筛选，再结合依赖、优先级、负载与在途 token 预留选择执行者；记录调度依据，失败可重新分派 | `docs/autonomous-orchestration.md` |
| 结果验证与监控 | 冻结验收在独立 Docker 中真实执行，不采信候选自改测试或自报成功；真实审查联合门控；运行中实时轮询状态与失败分类 | `docs/runtime-evidence.md`、`docs/implementation-status.md` |
| 反馈循环与优化 | 真实失败反馈给修正 Agent，最多两轮；保留失败与格式重试；新增原预算内的收尾预留策略（`finishing-budget-v1`） | `docs/multi-task-evaluation.md`、`docs/delivery-readiness-20261110.md` |
| 至少一种自我循环机制 | 验收失败 → 修正 → 复验 → 审查的闭环已用**预设缺陷**真实模型实验验证；另有预算不足时按门控收尾的自我限制 | `docs/multi-task-evaluation.md`、`docs/runtime-evidence.md` |
| 调度/验证/循环的明确自定义改进 | ① 能力 + 权限双重筛选与负载调度；② 候选哈希绑定冻结验收、门控不通过不标成功；③ 原总预算内的最终验收/审查资源预留与收尾；④ 协作图事件回放 | `docs/autonomous-orchestration.md`、`docs/delivery-readiness-20261110.md` |
| 知识库、工具调用、运行记录实际作用 | `knowledge/` 文本检索（关键词，非向量）；文件读写/搜索/命令/Docker 应用工具可查看输入输出；SQLite 持久化运行与快照并支持恢复 | `docs/implementation-status.md`、`docs/project-workspaces-verification.md` |
| 真实运行的多 Agent 原型 | `deepseek-v4.1-flash` 真实协议探针、真实开发/审查、项目持续开发、Web 项目开发与预览均已实际运行 | `docs/runtime-evidence.md`、`docs/web-projects-validation.md` |

## 5. 真实数据摘要（全部可在证据中核对）

- **四项真实开发任务**：43 次模型请求 / 313,562 tokens，四项各 **10/10** Docker 验收通过；3 次 JSON 格式重试，自然代码失败与修正均为 0（`docs/multi-task-evaluation.md`）。
- **项目持续开发**：同一记账项目两轮真实迭代到版本 3，**13/13、20/20** Docker 验收通过，18 次请求 / 136,769 tokens，1 次格式重试（`docs/project-workspaces-verification.md`）。
- **Web 项目**：从 Flask 健康页真实开发工作项看板，**14 项**生成 HTTP 验收 + **12 项**事前冻结独立复核通过；另一预设缺陷实验按“真实失败 → 真实修正 → 12/12 通过”完成（`docs/web-projects-validation.md`）。
- **动态编排对话**：2 个动态子任务、9 次请求、**12/12** 生成验收 + **8/8** 独立 Docker 检查通过（`docs/autonomous-chat-verification.md`）。
- **三策略对照（24 个预注册槽位）**：单 Agent、固定串行、动态并行的功能通过为 **6/8、6/8、8/8**，完整流程为 **6/8、6/8、7/8**；174 请求 / 已知 1,150,622 tokens；保留 4 次 HTTP 429、13 次格式重试与 1 次本地记录中断（`docs/comparison-evaluation.md`）。
- **最新交付批次（2026-11-10）**：模型调用增量 **0**；后端 **872 passed + 278 subtests**、前端 **110 passed + 构建通过**；真实 Docker 预览刷新/操作/停止与 390px 布局实测（`docs/delivery-readiness-20261110.md`）。
- **边界与协议**：六项合成候选容器探针全部按预期（0 模型调用）；真实协议探针 2 请求 / 564 tokens 通过；预设合成缺陷的真实修复 4 请求 / 16,883 tokens 并通过独立验收与审查（`docs/runtime-evidence.md`）。

## 6. 已知边界（如实声明，不作为已完成项）

- **单机本地原型**：未部署公网，不是多人服务；已在 Windows 11 + WSL2 路径实测，Linux 新机复现尚未实测。
- **可复现性缺口**：Dockerfile 的 `FROM` 仍为浮动 tag `python:3.13-slim`，正式提交前需锁定 digest；源码发布许可证尚未最终确定。
- **沙箱强度**：父子进程同容器、同 UID，是受控执行环境，不是面向任意恶意代码的强对抗沙箱，仅支持受控示例项目。
- **效果声明**：真实模型任务成功率、费用与“动态并行优于单 Agent”尚未被统计证明；对照实验仅 24 槽位，且存在 429 与格式重试。
- **自我修正**：已验证的是**预设合成缺陷**的真实修复；自然代码失败后的自动修正尚无样本，不宣称主工作流自然自修正已获证明。
- **Web 能力**：预览仅支持 wheel 依赖的 Python Web 应用，无 npm 构建或外部网络服务；预览成功不替代 Docker 验收。
- **其他**：知识库为关键词检索而非向量 RAG；工作流为代码化配置 + 可视化展示，无完整拖拽编辑器；无 Key 评委无法触发真实模型闭环。

## 7. 证据索引

- **总索引**：[`submission/证据索引.md`](证据索引.md)（9 个复现包及 SHA256、docs 分组、`.agentforge` 关键运行路径、真实/预设/模拟分类）。
- **docs（26 篇）**：架构与状态、环境与模型、编排与协作、项目与 Web、评测与对照、交付与进度。
- **output/**：各批脱敏证据、9 个复现包与 `.sha256`、`output/playwright/` 真实界面截图。
- **演示视频**：`submission/演示视频.mp4`（4:02，1920×1080，无音轨+烧录字幕），SHA256 `B13A6507C3C75445E1993D4335B12EB4025CF571181BB64B319BB26F3B70A594`。
- **关键文档**：[实现状态](../docs/implementation-status.md)、[架构说明](../docs/architecture.md)、[交付页与收尾](../docs/delivery-readiness-20261110.md)、[运行证据](../docs/runtime-evidence.md)、[多任务评测](../docs/multi-task-evaluation.md)、[三策略对照](../docs/comparison-evaluation.md)、[依赖说明](../docs/third-party.md)。
- **干净克隆复现记录**：[复现验证记录](复现验证记录-20261112.md)（公开仓库 `git clone --depth 1` 后：后端 872+278、前端 110、`npm ci`+`npm run build` 全部通过，构建产物哈希与交付记录一致）。
- **代码仓库**：<https://github.com/yancheng0518/agentforge-future-cup>（**已公开**，可匿名浏览/clone），tag `v1.2-submission-20261112`；完整提交材料在 `submission/`。

## 8. 提交材料清单

| 材料 | 状态 | 位置 / 说明 |
| --- | --- | --- |
| PDF 方案书（≥5000 字） | ✅ 已完成：18 页 A4，中文约 9,730 字 | `submission/AgentForge-方案书.pdf`（源稿 `submission/方案书-初稿.md`） |
| 方案讲解 PPT（10–15 页） | ✅ 已完成：**2026 未来杯官方模板**、16:9、15 页（含封面/目录/结束页，正文 13 页），逐页渲染检查 | `submission/项目介绍.pptx` |
| 演示视频（3–5 分钟） | ✅ 已完成：4:02、1920×1080、30fps、H.264、静音+烧录字幕 | `submission/演示视频.mp4`（52.9 MB；分镜见 `submission/演示视频脚本.md`；SHA256 见 `submission/演示视频.sha256`） |
| 可运行源码仓库 | ✅ 已完成并公开 | <https://github.com/yancheng0518/agentforge-future-cup>，`main` + tag `v1.2-submission-20261112` |
| 团队介绍 / 应用价值 / 推广设想 | ✅ 已填写 | 方案书 §9–11（团队信息以报名信息为准） |
| 学生身份证明等证明材料 | ⏳ 待用户上传 | 按官网通用要求提交（学生证/在读证明等） |

## 9. 联系方式

- 队伍名称：奶龙队
- 学校 / 学院：湖北工业大学工程技术学院
- 团队成员：王拙诚、吴捍卫、黄浩
- 指导教师：刘浪
- 队伍编号：2117173210695860225
- 联系人：王拙诚（项目负责人）
- 邮箱 / 电话：以大赛官网报名信息为准（公开材料不展示个人联系方式）
