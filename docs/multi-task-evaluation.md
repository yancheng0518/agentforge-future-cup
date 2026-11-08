# 多任务真实评测与复现记录

2026-11-08 已完成四项真实开发任务、协议修复复测与三项预设缺陷修正实验：**修复后四项真实开发均通过；三项明示预设缺陷均经标准 Engine 一轮真实模型修正后通过。自然代码测试失败及自然失败后修正仍为 0。** 首批八次运行的全部成功与失败保留，与后续批次分开呈现。

后续版本的结果必须单独归档，不覆盖首批记录，也不能把预设缺陷实验算入自然失败。单元测试中的假 API 数据仅验证报告逻辑，不属于真实模型成绩。

## 四项固定任务

四项任务均只允许修改候选 `todo.py`，类名为 `TodoStore`，各有预先确定的 10 项独立验收。验收在 Docker 内运行；没有隔离环境时阻塞，不在宿主机运行候选。任务起点缺少待开发功能，是维护者提供的需求基线，不能计作模型自然失败。

- `completion`：增加默认未完成状态、完成/恢复、按状态筛选，保持标题校验、单调 id、返回副本和实例独立。基线为 `examples/todo/todo.py`，契约为 `knowledge/requirements.md`，验收为 `acceptance/test_todo.py`。
- `bulk_edit`：增加 `rename`、`delete`、`batch_complete`；严格验证 id/布尔类型、拒绝重复 id、先完整校验再修改，批量失败必须原子回滚，删除不复用 id。对应 `benchmarks/tasks/bulk_edit/` 下的 `todo.py`、`requirement.md`、`test_todo.py`。
- `priority_query`：增加默认优先级、`set_priority`、`query`；先验证与过滤，再按优先级降序/id 升序排序，最后分页；不得改变 `list()` 的插入顺序。对应 `benchmarks/tasks/priority_query/` 下的三份文件。
- `json_merge`：增加版本化 JSON 导入导出和 `error/replace/skip` 合并；精确 schema/type 校验、全文验证后原子修改，保留位置和输入顺序，正确推进下一 id。对应 `benchmarks/tasks/json_merge/` 下的三份文件。

完整约束以各任务的 `requirement.md` 或 `knowledge/requirements.md` 为准。API `GET /api/tasks` 返回白名单任务及要求、路径、测试数量和哈希，不提供参考答案或合成候选。扩展三项任务只允许真实模型模式；`completion` 的模拟模式仍是独立的演示入口。

## 先校验独立验收，再冻结真实批次

首批控制实验证据：

`D:\未来杯\.agentforge\task-controls\20261108T084610Z-fd7236\evidence.json`

四个参考实现各通过完整 10 项 Docker 验收；四个明确标注的合成缺陷候选分别在预先声明的测试上失败，8 个控制均得到预期结果。负控制缺陷为：完成状态缺失筛选、批量操作未拒绝重复 id、优先级反向排序、JSON 导入未推进下一 id。这里的“8 个控制通过”指符合各自预期，**不是 8 个候选都通过功能测试**。模型调用数为 0，目标基线未变更，结束时没有残留运行中的 AgentForge 容器。

控制实验冻结需求、基线、独立验收、参考/合成候选、验收运行器和镜像信息。它证明验收可以识别这几类已知缺陷，不能替代真实模型开发或证明对任意缺陷有效。

真实批次在任何 POST 之前冻结：

- 任务顺序、重复次数、全部试次、模型参数、Agent 版本、工作流和最多两轮代码修正。
- 每项任务的需求、基线、验收及 SHA-256，以及参与执行的项目源文件清单和副本。
- 候选来源 `live_developer_output_without_injection`、不注入缺陷、全部结果纳入统计的规则。

真实批次归档排除 `.env`、参考答案及合成缺陷答案。运行器给每个 run 再保存独立验收副本与候选版本。评测脚本保留脱敏运行详情、事件、步骤、Agent/工作流版本、候选差异、逐轮源码、测试/反馈和哈希关联。

首批固定顺序为 `completion → bulk_edit → priority_query → json_merge`，完整重复两遍，共 8 次；新工作流开始间隔至少 60 秒。每个模型请求的进程内开始间隔至少 4.2 秒。120 次请求是“提交下一任务之前”的预算闸门，已提交工作流允许正常完成，不会为达成整齐计数强杀正在运行的任务。900 秒是单次观察上限，不代表自动取消后端运行。

## 首批原始结果

批次目录：

`D:\未来杯\.agentforge\evaluations\20261108T084840Z-9d855f`

每条的 `trials/<试次>/evidence.json` 保存完整原始详情。测试“通过”在这里指有候选写入和哈希关联的完整 Docker 10/10；工作流还要求同轮审查与最终条件通过。

- `01-completion-r1`：Docker 10/10；工作流失败于 `round-0/verify/inspect` 的模型 JSON 契约；8 次请求，27,691 tokens。
- `02-bulk_edit-r1`：Docker 10/10，工作流成功；13 次请求，96,286 tokens。
- `03-priority_query-r1`：`plan` 模型 JSON 契约失败，未观察到候选 Docker 执行；2 次请求，10,043 tokens。
- `04-json_merge-r1`：Docker 10/10；工作流失败于 `round-0/verify/inspect` 的模型 JSON 契约；11 次请求，106,423 tokens。
- `05-completion-r2`：Docker 10/10，工作流成功；8 次请求，27,747 tokens。
- `06-bulk_edit-r2`：Docker 10/10；工作流失败于 `round-0/verify/inspect` 的模型 JSON 契约；10 次请求，72,752 tokens。
- `07-priority_query-r2`：`develop` 模型 JSON 契约失败，未观察到候选 Docker 执行；6 次请求，32,498 tokens。
- `08-json_merge-r2`：Docker 10/10；工作流失败于 `round-0/verify/inspect` 的模型 JSON 契约；10 次请求，93,072 tokens。

合计 **68 次模型请求、466,512 tokens**，与每个 Agent 事件逐条合计相符，8 个 run 均报告用量完整。此处是用量，不是费用推算。6 次实际完整验收共 60 项通过；2 次未进入验收不能计作测试通过。工作流成功为 2/8，不把 6 次代码测试通过改写为 6 次工作流成功。

原始 `summary.json`、`preregistration.json`、`state.json` 与 8 份 `evidence.json` 均保留。新增 `audit_report.json` 和 `audit_report.md` 对测试事实单独核对，附每次 Docker stdout/stderr、候选与验收哈希、失败 Agent 事件、源证据 SHA-256。新增审计未改写原始结果。

## 如何区分自然失败、审查失败和未完成证据

自然代码测试失败要求：真实开发/修正 Agent 生成候选 → 对应候选成功写入 → 匹配同轮候选哈希和冻结验收 SHA → 隔离验收完整运行 10 项并失败。**这个测试事实不依赖 review 或 gate 成功。** 基线失败、合成控制失败、隔离启动失败、测试未完成和模型 JSON 协议失败均不计入自然代码失败。

原报告的 `first_round_result=unverified` 表示未得到完整验证结论。首批 01、04、06、08 的 `rounds[0].test_suite_completed=true`、`test_passed=true`，但 gate 未完成，因此仍是 `unverified`。旧通用判定中的 `strict_outcome.real_isolated_test_passed=false` 依赖完整 gate，不能解释为 Docker 未执行。请同时查看 `rounds` 和新增审计中的 `independent_docker_tests`。

`review_only_failure_observed` 只表示测试通过后，审查明确返回有效 `passed=false`。模型无法输出合法审查 JSON 属于协议失败，不等同于“审查发现代码缺陷”。首批自然测试失败、有效审查拒绝和自动代码修正均为 0。没有自然失败分母，不能据此报告自然错误修复率。

自动代码修正还要求保留有序的失败、反馈、真实 fixer 输出、候选变化与复验；修复模型输出格式的重试必须单独记作协议恢复。

## 两次观察器修复的审计

运行期间出现了两处观察器空值问题：正常 run 级事件的 `node_id=None` 传入正则；尚未完成步骤的 `output=None` 被当作字典。它们是本地报告程序错误，不能归因于模型候选代码。

当时未修改冻结脚本和运行条件。批次内 `observer_patch.py` 仅过滤非字符串节点，并对分类器使用的 deepcopy 将空输出视为缺失证据；原 API 导出的 run 不变。已提交的 run 继续观察，未重新提交失败试次。

对应审计文件为 `observer-repair-audit.json`、`observer-repair-v2-audit.json`，第一版包装器保留为 `observer_patch_v1.py.txt`。批次结束后才将两个空值防护纳入当前 `scripts/evaluate_tasks.py`。无 gate 的测试成功/失败及 pending 空输出均有回归测试。修复后的分类重新计算，与首批保留的逐轮结果一致。

关联本地报告测试已执行：`test_evaluate_tasks.py`、`test_live_demo.py`、`test_live_repair_probe.py` 共 54 passed、5 subtests passed。这些是使用模拟输入的报告/因果关联检查，不是额外 54 次模型运行。

## 协议诊断与下一版调整

首批失败响应未保留原始模型正文、`finish_reason` 或 JSON 错误位置，因此不能追溯断言六次失败都由截断、语法或同一 schema 错误导致。

新建的单节点诊断在 `D:\未来杯\.agentforge\protocol-probes\20261108T090324Z-c09c8149\evidence.json` 中复现了同类问题：`json_merge` 静态初审第 4 次响应在 JSON 前附加自然语言说明，`finish_reason=stop`，`completion_tokens=336`。这次响应确有混合正文，不是 `length` 截断；这是**新的诊断调用**，没有改写历史 run，也不能替代六份历史失败的缺失诊断。

当前适配器 `adapter_revision=2` 保留严格 JSON/角色契约校验，每次 Agent 调用最多 4 个请求：最多前 2 次允许自动工具调用，之后强制最终回答，并为一次禁用工具的契约重试保留预算。`automatic_tool_rounds=2`、`contract_retries=1`；旧字段 `tool_rounds=4` 表示总请求预算。不会通过字符串强转 `passed` 或截取混合正文中的 JSON 来放宽验收。成功与失败路径均保留脱敏诊断、结束原因、JSON 错误位置和重试计数。

后续批次需重新冻结此适配器及参数，独立统计协议恢复和代码修正；首批冻结档仍代表原始条件。

## 复现：自然开发批次

在项目根目录使用 PowerShell。模型配置保留在本地 `.env`，不要打印或复制密钥到报告。先启动 WSL Docker 并检查环境，再使用单独终端启动 API。测试不会在隔离缺失时回退到宿主执行。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-sandbox.ps1
.\.venv\Scripts\python.exe scripts\preflight.py
.\.venv\Scripts\python.exe scripts\probe_task_suites.py
powershell -ExecutionPolicy Bypass -File .\scripts\backend.ps1
```

控制脚本产生新的 `.agentforge/task-controls/<批次>/evidence.json`。检查 8 个控制确实符合预期后，再开始真实评测。API 启动后，在另一终端执行下列命令；输出目录必须不存在，重新复现应更换目录名。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_tasks.py --tasks completion bulk_edit priority_query json_merge --repetitions 2 --interval 60 --max-model-calls 120 --timeout 900 --output .agentforge/evaluations/reproduce-live-v2 --dry-run
```

`--dry-run` 只通过本机 API 读取并冻结任务/配置，不提交模型工作。检查 `preregistration.json` 后，保持代码、任务、Agent、工作流和模型参数不变，执行：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_tasks.py --resume .agentforge/evaluations/reproduce-live-v2
```

`--resume` 接着观察已提交 run 或提交原计划中尚未开始的试次；不会重复执行已经失败的试次，也不调用 API 的 `/resume`。丢失 POST 回复会保留 `submission_uncertain`，不能盲目重发。来源发生变化则另建批次，不能修改旧冻结档使其通过校验。不要同时运行另一个真实评测或模型探针。

默认不传 `--repetitions` 时每项只跑一次。`--tasks` 可选固定子集，间隔最少 60 秒。新运行可能计费，原批次全体失败与成功都应保留。模型采样、服务状态和新适配器会影响结果，复现的是可审计流程，不承诺逐字生成或相同成绩。

## 复现：明确预设缺陷的修正工作流

`scripts/evaluate_seeded_workflows.py` 使用真实 Engine、Docker 和模型节点，仅将初始 `develop` 输出替换为预注册维护者缺陷夹具。规划、测试解读、静态/最终审查和 fixer 仍调用真实模型，反馈与最多两轮修正由普通 Engine 负责。它不自动应用结果，也不改验收。

三项夹具来自 `benchmarks/synthetic/`：`bulk_edit` 遗漏重复 id 校验、`priority_query` 升序排列优先级、`json_merge` 未推进下一 id。候选来源固定为 `declared_synthetic_developer_fixture`，`fault_injection=true`，`natural_model_development_failure=false`。即使成功，也只能证明这些预设缺陷下的真实修正闭环。

先用一个新目录检查冻结内容：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_seeded_workflows.py --tasks bulk_edit priority_query json_merge --repetitions 1 --output .agentforge/seeded-workflows/reproduce-seeded-plan --dry-run
```

实际实验需另一个新目录；该脚本没有 `--resume`，不应在已存在的 dry-run 目录上重跑。下面引用本机已保存的 8 个控制，脚本会重新校验其原始结果、镜像、冻结字节和哈希；若当前条件不匹配，应先重新运行控制，而不能篡改旧证据。

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_seeded_workflows.py --tasks bulk_edit priority_query json_merge --repetitions 1 --max-model-calls 100 --max-seconds 3600 --controls .agentforge/task-controls/20261108T084610Z-fd7236/evidence.json --output .agentforge/seeded-workflows/reproduce-seeded-live
```

省略 `--controls` 时脚本会先运行当前三项参考实现的 Docker 正控制。100 次调用/3600 秒同样是提交下一试次前的闸门，不中止已经开始的普通工作流。每次保留 `preregistration.json`、`summary.json`、控制证据、独立数据库和各试次导出。它不通过失败重跑筛选成功结果。

## 适配器修复后的独立复测 · 已完成

批次 `.agentforge/evaluations/20261108T090616Z-606cb8` 预注册四项任务各一次，固定顺序，全部完成且全部成功；没有重跑或覆盖首批失败。预注册文件 SHA256 为 `c84b7084797134994788af3cf248a1acc018e838bd0da8c369478e3969bbe11a`。完整明细在该目录的 `audit_v2.json`、`summary.json` 和 `trials/*/evidence.json`。

- 完成状态：`7ff15e24e5af4e0a97167bef70c32956`，9 次请求、32,864 tokens，无格式重试。
- 批量编辑：`a3e66b793e00417aaf2e2b6583ce4f44`，11 次请求、82,419 tokens，静态审查格式重试 1 次。
- 优先级查询：`4d016c52d9b546829973b21dca2eeee1`，12 次请求、87,077 tokens，静态审查格式重试 1 次。
- JSON 合并：`c1777cf1dca74b95bbb0a638f04ad395`，11 次请求、111,202 tokens，静态审查格式重试 1 次。

合计 **43 次请求、313,562 tokens、四项各 10/10 Docker 测试和真实审查通过**。全部为真实开发候选，未注入缺陷、未应用，代码修正轮次全部为 0。自然测试失败与自然失败后修正均为 **0**，不能把 3 次 JSON 格式恢复计作代码自修正。

三次格式错误均已持久化原始内容（凭据脱敏）、内容哈希、`finish_reason=stop`、解析位置 0 和真实用量；成功的格式重试同样保留失败记录。适配器没有从说明文字中猜取 JSON，也没有强转 `passed`。它在每节点 4 请求总预算内，将自动工具阶段限制为前两次，预留一次完整 JSON 重发；格式重试禁止工具调用，最多一次。两批样本数、实现版本不同，结果仅为实际运行记录，不能据此计算可靠的性能提升。

## 预设缺陷的标准工作流修正

批次 `.agentforge/seeded-workflows/20261108T091215Z-1da7d89a` 已完成 3/3 次预注册实验，共 **46 次真实模型请求、412,072 tokens、205.37 秒**。三项均由标准 Engine 触发一轮自动代码修正，最终各 10/10 容器验收与真实审查通过。全体试次保留、没有额外重开失败任务，基线未变，`applied=false`。预注册 SHA256：`93e46a030b32de1aaf470f9eb4af2297169c215cc5b4aed6067dd262c43be22d`。

- `bulk_edit`，run `55b6e9d6bac44a8e85aa6b8008cfb774`：预设遗漏重复 id 校验，初轮完整测试失败。事件 20 的预设候选 → 33 的失败测试 → 47 的反馈 → 52 的真实 fixer 候选 → 65 的通过测试；18 请求、140,810 tokens。
- `priority_query`，run `7e7f81c1bf544948b747432ce8123518`：预设优先级反向排序。事件 98 → 111（失败测试）→ 124（反馈）→ 127（真实 fixer）→ 140（通过测试）；13 请求、103,683 tokens。
- `json_merge`，run `257834af469548d7a77dc184d1bae0fe`：预设导入后不推进 id 分配。事件 173 → 186（失败测试）→ 200（反馈）→ 203（真实 fixer）→ 216（通过测试）；15 请求、167,579 tokens。

每项的 `trials/*/run.json` 保存原始事件、SQLite 导出、快照、每次候选源码、完整测试 stdout/stderr、失败反馈与诊断；`classification.json` 核对候选哈希变化、冻结验收哈希、同轮 scope、真实测试/审查与 gate 顺序。初始开发输出明确为 `synthetic_fixture`，模型请求为 0；其他模型节点包括 fixer 均真实调用。**这证明三个已知预设缺陷下的主工作流真实修正闭环，不是自然生成错误后的修复率。**

## 平台回归与本地复现包

该阶段平台回归为 **162 passed、133 subtests passed（31.89 秒）**，TypeScript/Vite 构建通过。后续对照实现后的 287 项回归与真实试验另见[三组对照](comparison-evaluation.md)。候选仅在 Docker 执行，平台测试采用假网络/假容器传输，不能增加真实模型成绩。服务取消保留已发请求和已知用量；正在进行的 HTTP 用量未知时保持 `metrics_complete=false`。Docker 启动异常返回 blocked/isolated=false，避免把未确认启动写成真实测试。以上异常分支有离线回归，未故意中断本阶段收费模型请求。

本地完整包为 `output/agentforge-reproduction-20261108.zip`。包含当前源码、测试、契约、锁文件、上述两批自然开发、三项预设缺陷、八个控制及两次协议诊断的原始导出和冻结代码；保留首批失败与观察器修复审计。它排除真实 `.env`、WSL 磁盘、依赖目录和 SQLite 数据库，以完整 JSON 快照保存运行记录。没有上传。

包内 `REPRODUCTION_MANIFEST.json` 列出每个成员的大小和 SHA256；导出时扫描配置密钥并重新读取 ZIP 核验。验证命令不执行任何候选：

```powershell
.\.venv\Scripts\python.exe scripts\export_reproduction.py --verify output\agentforge-reproduction-20261108.zip
```

重新导出时使用 `scripts/export_reproduction.py --evidence <证据目录>` 重复指定上述六个批次目录，再给一个不存在的 `--output` 路径。原文件不改写。配置本地密钥、安装依赖与启动 Docker 后，按前文命令在**新目录**重新运行；模型响应及用量不保证一致。历史条件应看各自 `frozen/`，不要将当前修复版本冒称为首批代码。

## 尚待验证

真实自然失败后的自动代码修正尚未观察到。只能在真实开发候选确实失败且证据链完整时补充，不能用预设缺陷成绩代替。同模型、同任务、同预算上限的单 Agent/固定串行/动态并行对照已完成原 24 槽位采集，保留所有服务失败和本地中断；设计、两阶段边界与原始记录见[对照复现](comparison-evaluation.md)。另一台机器从零安装复现仍未完成。四项任务同属 TodoStore 的功能开发，不代表四个独立项目。本文件所述历史批次运行配置使用可变镜像标签；新对照已使用不可变本地镜像 ID。基础 Dockerfile 仍未固定远程 digest，不承诺未来机器重建出逐字节相同镜像。
