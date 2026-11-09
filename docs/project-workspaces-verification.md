# 同一项目连续开发：真实验证与复现

2026-11-09，已在同一个记账项目上完成两轮真实模型开发，形成导入 v1、分类报表 v2、删除记录 v3。两轮合计 **18 次模型请求、136,769 tokens、1 次 JSON 格式重试**；冻结函数验收分别 **13/13、20/20** 通过，版本均自动保存。模型候选自然失败、自然修复、预设缺陷实验均为 **0**。这些数字只属于本批两条运行，不合并旧实验或平台回归测试。

本机最终界面地址是 <http://127.0.0.1:8002>。它是本地工作台，不是公网部署。本文依据保留的请求、事件、源码快照、Docker 回执、SQLite 和浏览器检查说明结果；没有把模型总结单独当作通过证据。

## 验证对象与事前声明

输入是维护者编写、已经实现 `add/list` 的标准库 Python 项目 [examples/expense_cli](../examples/expense_cli)，不是为演示修复而植入缺陷的程序。事前声明保留在 [verification-declaration.json](../output/project-workspaces-20261109/verification-declaration.json)，既有六项函数验收在 [acceptance_v1.py.txt](../benchmarks/project-workspaces/acceptance_v1.py.txt)。

第一轮需求是增加整数分计价的分类汇总及 `report` 命令；第二轮是在同一版本链上增加 `delete_expense` 和 `delete --id`，保留旧字段、顺序、已有行为与持久数据。需求原文为 [round1.txt](../benchmarks/project-workspaces/round1.txt)、[round2.txt](../benchmarks/project-workspaces/round2.txt)。

用户在实施期间要求采用 Codex 同类处理策略，因此在第二轮提交前追加了 [round2-amendment.json](../output/project-workspaces-20261109/round2-amendment.json)：要求模型自行用工具检查 CLI，并自由选择直接修改或委派。新增指令没有改变功能期望，没有预先植入错误，也没有要求必须创建子 Agent。第二轮使用工具是明确的任务要求；直接修改和不委派是模型在允许范围内的选择。

真实模型为比赛平台 `deepseek-v4.1-flash`，配置快照为 `temperature=0.1`、`thinking.type=disabled`、输出上限 8192、适配器 `autonomous-6-project-revisions`。两轮均为 `mode=live`，不是模拟输出。调用计数包含 intake、语义审查、主 Agent 决策以及格式重试；tokens 来自本批完整的 provider usage，没有用预算估算替代。

两轮预算上限相同：40 请求、300,000 tokens、24 决策步、32 子任务、最多 3 并行、8 次验证、900 秒；请求超时 120 秒、最小请求间隔 4.2 秒、至多 1 次契约重试。两轮功能不同，这不是同任务策略对照。Docker 使用 `agentforge-test:py313`，实际镜像 digest 为 `sha256:1edc3cba75dd1917bb78226e897fdb801a0272ea2e02319345de0bd3b8cae225`。

## 两轮实际过程

第一轮运行 `ce012be5a56e452c86805463b3ba65e6`：

- 主 Agent 在冻结既有源码后增加七项函数验收，与原六项合并为十三项；导入基线未实现新增汇总，按 `imported_baseline` 记录失败。
- 自主选择 `edit → verify → finish`，直接修改 `ledger.py` 和 `main.py`。没有子任务、没有应用工具调用，也没有代码修复循环。
- 当前候选的十三项 Docker 验收全部发现并通过，随后需求语义审查通过，保存 v2。
- 共 **6 次请求、40,134 tokens、0 次格式重试**。完整记录为 [round1-evidence.json](../output/project-workspaces-20261109/round1-evidence.json)。

第二轮运行 `c77677a111d641ebb743b7678d5546e4`：

- 导入的是第一轮 v2，而不是原始 v1。原十三项验收全部保留，新增七项删除验收，合计二十项；v2 尚未实现删除，因此再次产生单独标记的 `imported_baseline` 失败。
- 实际决策顺序为 `edit → tool × 5 → verify → finish`。五次均调用 `run_application`，在本轮专属临时数据区依次执行 `add Coffee`、`report`、`delete --id 1`、`list`、`delete --id 999`。
- 前四次应用退出 0；最后一次按需求抛出 `KeyError: 999`、非零退出，状态保持 `failed`、未保存新数据，前后数据 hash 相同。这是预期负例，不算自然候选缺陷或修复。
- 工具结果真实反馈给后续主 Agent 请求。工具调用没有修改源码；随后二十项冻结函数验收全部发现并通过，需求语义审查通过，保存 v3。
- 共 **12 次请求、96,635 tokens、1 次 JSON 格式重试**。格式重试不是代码修正。完整记录为 [round2-evidence.json](../output/project-workspaces-20261109/round2-evidence.json)。

两轮的两个导入基线失败分别代表“旧版本尚未实现本轮新增功能”。没有出现“模型写出的候选失败后又被修好”，因此不能把本批写成自然自修复成功案例。应用工具退出 0 也没有授予验收通过或完成状态；完成仍需要当前源码 hash 对应的独立验收和语义审查。

## 版本链、配置与资料的真实使用

项目 ID 为 `4da12061fc7c43fe965b630637b86a2c`，三个不可变版本分别是：

- **v1**：`ef3c6a152cd4460a80e6e225fb0e366c`，导入时标为 `unverified`；源码 SHA-256 为 `b371a318a8e22ff013ce2a739b62a1526c8963bdcc93c54189c0d3c4acacb282`。
- **v2**：`40e4c771e98f4e1197f97e49240a9530`，十三项函数验收与审查通过；源码 SHA-256 为 `1329511d2e20ecde3bacc3181df47f041b5f6db196efb2aed8393f3d764feb57`。
- **v3**：`f0191ca23c9c4ae1b45b13ecf5be3274`，二十项函数验收与审查通过；源码 SHA-256 为 `123a742179c0aef9274809bbeab115a7b26de4e14c09ce83ca59f3b354f76a45`。

版本发布在最终导出之前落库，事件、终态和导出包含发布结果。基线过期、候选 hash 不匹配或保存异常时保留候选，不能以“模型已完成”推断项目头版本已推进。原始 `examples/expense_cli` 与旧版本没有被模型覆盖。

本批冻结了 `general@1` 配置以及“记账项目约定”资料 v1。配置目录中的名称、描述、能力与版本 hash 实际进入模型上下文；资料采用本项目冻结文档上的有界关键词检索，真实请求含版本引用和节选，事件记录 `knowledge_retrieved`。它不是向量数据库，也不检索其他项目。

**本批没有委派子 Agent。** 所以本批证明了配置目录冻结、实际上下文传入、知识引用与主 Agent 工具循环；没有证明新配置的 worker 提示词在真实模型委派中被执行，也没有跨模型协作、负载调度收益或多 Agent 相对效率的实测结论。选择指定 profile 后注入描述和 prompt、拒绝未知/停用 profile、冻结版本与权限边界的行为由平台回归测试覆盖。当前主 Agent 和所有可委派 worker 继承同一模型，配置不能扩大宿主工具权限。

## 应用持久化与浏览器交付

本项目的真实应用数据与模型工具临时数据分开保存。正式应用数据在 v1 阶段写入 Lunch（1200 分）和 Bus（300 分）；第一轮后通过界面运行 `report`，得到总计 1500 分，两类汇总，数据 hash 未变，证据为 [round1-application-check.json](../output/project-workspaces-20261109/round1-application-check.json)。

初次应用运行曾因 `/data` 权限出现 `PermissionError`，原失败保存在 [baseline-application-1.json](../output/project-workspaces-20261109/baseline-application-1.json)。运行器权限修正后，两次独立进程成功写入 Lunch 和 Bus，记录为 `baseline-application-fixed-1.json`、`baseline-application-fixed-2.json`。这是宿主运行器错误修正，模型调用为 0，不计模型自然修复。

第二轮完成后，独立交付脚本针对项目正式数据真实运行五项 Docker 检查，全部符合预声明结果，见 [delivery-checks/summary.json](../output/project-workspaces-20261109/delivery-checks/summary.json)：

1. `report` 仍返回 1500 分，证明第二轮工具临时数据没有覆盖正式 Lunch/Bus。
2. `delete --id 1` 返回删除 ID 1、余下一条，并保存状态。
3. 再次 `report` 返回 300 分、仅 travel 分类。
4. `delete --id 999` 非零退出、`data_saved=false`，正式数据 hash 不变。
5. `list` 返回原 ID 2 的 Bus，金额、分类不变。

这些 CLI 与跨进程数据检查不调用模型，也不增加十三/二十项函数验收计数。五次工具调用与五项独立正式数据检查是两组证据，不能混算成十项新增函数验收。

最终在端口 8002 的浏览器核对项目三版本、源码与验收状态，下载 v3 ZIP 并核对内容，另导入一个无模型调用的项目确认显示“导入版本 · 未验收”。真实界面运行 v3 `list` 后，回执显示 Docker 已执行、退出 0，输出保留的 Bus 数据；此回执使用最终状态提交处理代码。历史显示二十五条运行；回放只读取已存事件。390px 视口下页面宽度仍为 390px，所检查过程无 console error/warning。见 [ui-delivery-evidence.json](../output/project-workspaces-20261109/ui-delivery-evidence.json)、[桌面截图](../output/project-workspaces-20261109/final-project-desktop.png)、[移动截图](../output/project-workspaces-20261109/final-project-mobile.png) 与 [应用回执截图](../output/project-workspaces-20261109/final-application-receipt.png)。这些结果不替代全部浏览器、无障碍或长时间运行验收。

## 公开机制与本项目实现

OpenAI 公开说明把 Codex 长任务描述为计划、编辑、运行工具、观察反馈、修正和更新记录的循环；多 Agent 说明建议把独立任务委派，短任务和有依赖的步骤保留给主 Agent。[Codex 长任务说明](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex)、[多 Agent 说明](https://developers.openai.com/api/docs/guides/agents-api/multi-agent)（2026-11-09 核对）。

本项目采用这类机制：宿主提供冻结工程、工具和结果反馈；模型逐步决定直接编辑、调用工具、委派、继续、验证或结束。任务图来自实际委派和事件，不要求每个小修改都经过固定角色。直接编辑受路径、写入范围和活动任务冲突约束，修改后旧验证失效；工具调用受总步数、时间与输出大小限制。`finish` 是模型提议，宿主仍执行完成条件检查。

实际实现是本项目的 Python 编排内核、比赛平台模型、SQLite 与本地 Docker；没有接入 OpenAI 托管 Codex harness，不宣称复现其内部实现或达到相同能力。旧模板工作流、旧角色调度实验与本批动态项目运行仍独立保留。

## 代码快照、平台测试与历史保护

两条真实模型运行目录分别保存 `implementation-manifest.json` 和 `implementation/`，每条有十九份实际宿主实现文件。它们是本次模型运行的复现基准。当前最终代码与这两套快照相比，已继续修正 `autonomous_model.py`、`project_runner.py`、`project_tools.py`：包括文件读取分页、应用回执源码 hash 核对、数据提交冲突/未知状态与执行意图证据处理。旧快照未被替换。

**两轮模型没有在这些最终修正后再次运行。** 最终代码的错误分支通过明确的合成/故障注入回归验证；最终浏览器应用执行另外验证了真实 Docker 正常路径。不能把最终平台测试通过写成重新消费模型后两轮仍成功，也不能把故障注入当作实际发生的存储故障。

最终平台测试记录为 [pytest-final-601.txt](../output/project-workspaces-20261109/pytest-final-601.txt)：**601 passed、278 subtests passed，91.55 秒**；前端 [87 项测试通过](../output/project-workspaces-20261109/frontend-tests-final.txt)，[生产构建通过](../output/project-workspaces-20261109/frontend-build-final.txt)。这些测试覆盖平台行为，使用 StubRuntime、模拟网络或明确的故障注入处不算真实模型效果。较早的 `pytest-final.txt`（599 项）保留为中间结果，不是最终计数。

[run-audit.json](../output/project-workspaces-20261109/run-audit.json) 只读核对全部八条动态运行：无 error，三条旧记录保留“intake 未准备候选”的 warning。源码/验收/运行器 hash、修改链、工具结果、当前验证 hash、发布结果及最终 JSON 与 SQLite 对应关系通过；usage 原样保留。全库共 62 次请求、498,528 已知 tokens，另有一次旧 usage 未知；不能将这组全库数字替换本批 18 次、136,769 tokens。哈希审计证明记录内部一致性，不证明对协同恶意改写的抵抗力。

历史保护的原始检查 [preservation-after.json](../output/project-workspaces-20261109/preservation-after.json) 保持 `passed=false`：事前清单误纳入正在使用的 `.agentforge/wsl/AgentForge/ext4.vhdx`，Docker 正常运行改变了该虚拟磁盘。原始清单与失败报告没有改写。后续 [preservation-adjudication.json](../output/project-workspaces-20261109/preservation-adjudication.json) 明确作范围裁定：其余 **1646 个历史证据文件全部字节不变**，三套数据库的全部原有行保留；动态库只追加本批两条运行及事件。不能声称“1647 个文件全部字节不变”，也不能把原始失败隐藏为全通过。

## 复现与复核方法

只读复核无需模型或 Docker。先阅读本批两个完整导出、交付检查、审计与历史保护裁定；重新运行审计必须使用新输出文件，脚本拒绝覆盖旧报告：

```powershell
.\.venv\Scripts\python.exe scripts\audit_autonomous.py --data-dir .agentforge/autonomous --output output/project-workspaces-review-new.json
```

平台回归可在已安装依赖的工作区执行 `.\.venv\Scripts\python.exe -m pytest -q`，前端目录执行 `npm test`、`npm run build`。这些命令不能替代真实模型或应用验证。

重新验证开发过程应使用独立工作目录和新项目，按 [README](../README.md) 安装锁定依赖、设置自己的模型凭据及 Docker，运行预检后启动工作台：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-sandbox.ps1
.\.venv\Scripts\python.exe scripts\preflight.py
powershell -ExecutionPolicy Bypass -File .\scripts\workbench.ps1 -Port 8002
```

若 8002 已由原服务占用，给新服务换一个独立端口。精确复现本批输入时，通过 `POST /api/projects/import` 提交 `examples/expense_cli` 两个源码文件和 `benchmarks/project-workspaces/acceptance_v1.py.txt` 的既有验收；仅通过 UI 导入源码的版本没有这六项原始验收，因此不能声称是相同验收起点。通过项目资料入口保存 `knowledge.txt`，选择启用的配置版本，然后依次提交两轮原始需求和第二轮预先声明的追加指令。

每轮 `POST /api/projects/{project_id}/runs` 使用当前 `base_revision_id`、需求、明确的配置/资料版本与新的 `idempotency_key`。同一次提交重试沿用原 key，第二轮等待第一轮终态与版本发布后再提交。新模型运行会产生新的请求与费用，也可能选择不同的动作、输出或委派方式；不承诺相同 tokens、测试生成数量和成功率。

应用交付复现需要一个独立项目，先经受控“运行程序”向正式数据写入 Lunch 和 Bus，按声明的输入字段和整数分金额设置。两轮完成后，在该项目运行：

```powershell
.\.venv\Scripts\python.exe scripts\verify_project_delivery.py --base-url http://127.0.0.1:8002 --project-id 新项目ID --output output/project-delivery-new
```

该脚本会真实删除所选示例项目的 Lunch 数据；原交付验证后的项目已只剩 Bus，不能在原项目重新从第一项检查开始并期待 1500 分。保留同一次检查的 intent/结果目录可防止中断后重复执行；不同实验使用新项目和新输出目录。不要在宿主机直接导入或运行候选 Python。

完整归档、新空目录恢复和校验命令见 [project-workspaces-restore.md](project-workspaces-restore.md)。归档完成与否以实际 ZIP、SHA-256 和 archive manifest 为准；本文不把打包脚本或原机检查当作全新机器安装已完成。

## 已知边界

- 当前支持有界标准库 Python 源码、JSON 函数接口验收和短时 CLI。未验证任意大型仓库、第三方依赖安装、常驻服务或多人并发部署。
- AI 生成验收和同模型语义审查都可能遗漏需求；独立进程执行与冻结源码解决部分执行和证据问题，不等于独立人工业务评审。CLI 与持久化仅覆盖上述明确用例。
- 候选在 Docker 中运行，网络关闭、非 root、源码与根文件系统只读，应用用 `/data` tmpfs，运行上限 15 秒、外层容器期限 25 秒、持久状态上限 32 文件/64 KiB。测试驱动和候选子进程仍共享容器及 UID，不是任意恶意代码的强对抗隔离服务。
- 模型应用工具只使用每轮 scratch 数据，正式应用运行使用该项目的持久数据；应用退出 0、数据提交成功和验收通过是分别记录的事实。提交结果未知时保留意图与错误，不自动重跑应用。
- `read_file` 支持从 1 开始、最多 400 行的分页，单次内容最多 16,000 字符；极长单行仍可能截断，当前没有按字符继续读取接口。超时、取消、存储提交冲突等分支主要由平台回归覆盖，本批两轮没有真实触发这些分支。
- 本批是一个维护者构造的记账样本上的两次迭代。没有新 profile 真实委派效果、自然代码修复、动态调度对照或跨项目泛化结果；旧实验结论不能直接转移到本阶段。
