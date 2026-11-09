# 模型驱动的动态编排内核

2026-11-08 新增 `model-directed-v1`，随后接入现有浅色对话界面。浏览器直接输入自然语言需求，调用 `/api/autonomous/requests`，由模型准备并审查契约、生成任务和子 Agent 指令。原有 CLI 与 task bundle API 保留。界面闭环、错误修正及全部尝试见 [对话闭环验证](autonomous-chat-verification.md)；本文下方三项 bundle 实验是此前的独立批次。

## 执行方式

主 Agent 读取需求、当前项目、任务结果、上轮验收和剩余预算，决定下一步行动：

- `dispatch`：建立任意名称的任务、指令、依赖、文件写入范围，为每项任务创建独立子 Agent 上下文。
- `continue`：执行已有任务图的下一批就绪任务。
- `cancel`：撤销不再需要的待执行任务，保留原记录。
- `verify`：运行冻结的独立 Docker 验收。
- `finish`：提出完成；执行器要求当前候选哈希已经通过隔离验收，且没有待执行任务。
- `stop`：明确报告无法继续的原因。

执行器校验依赖引用、环、重复 ID、写入范围和预算。依赖满足且写入路径不重叠的任务可以并发；每批执行后把控制权还给主 Agent。失败任务不会暗中重跑，主 Agent 可以创建新任务替换。没有预设五角色节点序列，也不导入旧 `TASKS` 目录决定可接受需求。固定的是权限、资源预算和完成判据。

模型仅提出文件修改；候选源码不在宿主机执行，不自动应用到原项目。新入口没有静默模拟降级。

## 输入与边界

输入 JSON 的字段严格为：

```json
{
  "requirement": "明确的需求和函数接口",
  "files": {"module.py": "现有源码或待实现脚手架"},
  "acceptance": "独立的 Python unittest 验收源码",
  "provenance": {"kind": "natural", "description": "来源说明"}
}
```

支持标准库 Python 多模块项目，最多 32 个文件，每文件 64 KiB、总计 256 KiB。验收使用 `from bridge import call`，例如 `call("module", "calculate", 2, scale=3)`；参数和返回值必须能用 JSON 表示，每次调用使用新子进程。暂不支持跨调用对象状态、第三方依赖安装、任意终端命令或文件删除。独立验收由维护者提供并冻结，不由执行子 Agent 更改。这不是完整的 Codex 通用工作区代理。

## 使用与复现

在 `D:\未来杯` 的 PowerShell 中执行，沿用本地 `.env`，不要将密钥传入命令行或归档：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-sandbox.ps1
.\.venv\Scripts\python.exe scripts\preflight.py
.\.venv\Scripts\python.exe scripts\run_autonomous.py benchmarks\autonomous\sales_report.json --data-dir .agentforge\my-autonomous-runs --max-tokens 300000 --max-calls 40 --wall-seconds 900
```

替换任务包路径即可运行其他 Python 任务，无需修改任务枚举、角色定义或引擎。另两个验证包为 `benchmarks\autonomous\interval_library.json` 和 `benchmarks\autonomous\shipping_seeded.json`。

每次命令创建新运行 ID 并重新请求模型，可能产生费用；结果保存在所选目录的 SQLite 和 `runs/<id>/`，不会覆盖旧运行。Ctrl+C 尽量保存中断和未知用量；进程被强杀不能保证远端请求停止，不自动重放中断请求。

后端重启后提供独立接口：

- `POST /api/autonomous/runs`，正文 `{"bundle": <任务包>, "budget": {"max_calls": 40, "max_tokens": 300000}}`。
- `GET /api/autonomous/runs`：新内核的历史。
- `GET /api/autonomous/runs/<id>`：运行、决策、事件、步骤和快照。
- `GET /api/autonomous/runs/<id>/candidate`：候选文件及哈希；尚未准备好返回 409。

API 使用 `.agentforge/autonomous/agentforge.sqlite3`；本次真实验证使用 `.agentforge/autonomous-validation-20261108`。它们与旧工作台数据库分开。API 最多两项运行同时执行，这不代表模型服务商保证支持同等请求并发。

默认预算：40 次模型请求、200,000 预算 tokens、每次最多 4,096 输出 tokens、24 次主 Agent 决策、32 项任务、3 个并行任务、8 次验收、900 秒。本次真实验证统一将 token 预算设为 300,000，其余沿用默认。失败 HTTP 请求和格式重试均占请求额度，每次模型调用至多一次格式重试。发送前按提示 UTF-8 字节数加输出上限保守预留；用量缺失时保留该预留估计。这不是精确账单上限。

## 三次真实运行

均使用平台 `deepseek-v4.1-flash` 和实际 Docker 镜像 `sha256:1edc3cba75dd1917bb78226e897fdb801a0272ea2e02319345de0bd3b8cae225`。

1. CSV 报表，`3f64240036f443ec9300d6ea14d6ea9e`：主 Agent 首次派发解析和报表两个任务；报表请求 HTTP 429 失败后，增加新 ID 的替代任务。最终 12/12 通过。9 次请求、已知 42,022 tokens、2 次格式重试、1 次用量未知的 HTTP 429；自然代码失败 0。
2. 区间工具，`82b877460dce442486120909087ce608`：主 Agent 选择一个实现任务，没有照搬 CSV 的任务图。最终 12/12 通过。4 次请求、15,840 tokens；无格式重试、传输失败、自然代码失败。
3. 公开预设缺陷运费任务，`738f6262c2044c5cb69b91739d3f2cd5`：主 Agent 先验收基线，10 项中 3 项失败；读取反馈后创建修复任务，重测 10/10 通过。5 次请求、22,065 tokens，无格式重试。这是预设缺陷修复，不能计为自然代码错误发现。

合计 18 次真实请求、已知 79,927 tokens，1 次请求用量未知；已知总量不能当作完整账单。全部失败请求保留。两个 natural 任务来自未实现脚手架，基线 NotImplementedError 不计为模型自然失败。

三次运行期间修复了异常处理边界；每次 CLI 均保存启动时 `implementation/` 源码及 `implementation-manifest.json`，不能把三次称为完全相同代码版本的控制实验。后续边界修复通过回归测试，历史副本保持不变。本轮是功能验证，不证明优于单 Agent。

## 证据与分类

本轮完整平台回归为 **374 passed、165 subtests passed（59.40 秒）**；随后新增的只读归档审计另有 **12 项测试通过**。这些是假模型/假传输或合成证据测试，不代替上述真实运行。真实运行归档审计通过、0 个问题；模型请求、任务、决策、验收和冻结源码哈希一致。旧数据与前端共 1,137 个文件哈希不变，原数据库五张表逐行哈希不变（17 项运行、776 条事件、125 项步骤、101 份快照、6 个 Agent 版本）。

本次复现包为 `output/agentforge-autonomous-core-20261108.zip`，校验和在同名 `.sha256` 文件。包含当前源码、三次真实运行完整证据与各自源码快照、无模型的 Docker 基线/smoke、回归日志、审计和旧数据保留清单；不包含密钥、虚拟环境、node_modules 或 WSL 磁盘。旧实验本身仍保存在此前独立归档中。

每次保留原始任务、模型配置、预算、逐请求提示和最终输出（不保存密钥和模型隐藏推理）、所有 HTTP/协议失败、已知/未知用量、主 Agent 决策、任务身份与依赖、每轮源码和哈希、冻结测试和执行器、实际镜像 ID、stdout/stderr 和退出状态。

- `natural_candidate`：无注入的模型候选；其验收失败才计为自然代码失败。
- `input_baseline`：修改前输入状态，不假定所有输入都未实现。
- `seeded_defect_baseline`、`seeded_experiment_candidate`：公开预设缺陷实验，独立统计。
- 格式重试、HTTP 失败、调度决定被拒、隔离启动/超时各自留证。无有效回执时标记阻塞，不算代码失败。

另有三个输入基线的真实 Docker 验证，模型调用数为 0：CSV 12 方法/21 个错误，区间 12 方法/24 个错误（含子测试错误），运费 10 方法/3 个失败。证据在 `.agentforge/autonomous-fixture-baselines/20261108T121912Z/evidence.json`。手写加法函数的 1/1 Docker 正向 smoke 位于 `.agentforge/autonomous-sandbox-smoke/20261108T122136Z/evidence.json`，同样不是模型任务。

```powershell
# 平台回归：假模型/假传输明确区分，不产生模型效果数据
.\.venv\Scripts\python.exe -m pytest tests -q
# 三个原始输入的真实 Docker 基线，不调用模型
.\.venv\Scripts\python.exe benchmarks\autonomous\verify_baselines.py
# 只读核验已保存的真实运行，不调用模型或 Docker
.\.venv\Scripts\python.exe scripts\audit_autonomous.py --data-dir .agentforge\autonomous-validation-20261108 --output output\autonomous-audit-new.json
```

旧 17 项工作台记录、此前任务和预设缺陷实验、24 槽位单 Agent/固定串行/动态并行对照及已有 ZIP 全保留。旧对照中的 `dynamic` 指旧模板内的能力/负载/并行调度，不能重新命名为本次模型驱动编排。修改前清单和数据库备份在 `output/autonomous-core-20261108/preservation-before.json` 和 `legacy-before.sqlite3`；修改后核验结果另存同目录。

Docker 使用无网络、只读挂载、非 root、删除 capabilities 与资源限额，可信验收父进程不导入候选。但父子仍共享容器、UID 和进程命名空间，不宣称抵御任意恶意代码。新机重装复现、自然代码失败后的真实修正样本、跨语言工具和前端新入口仍待验证/实现。
