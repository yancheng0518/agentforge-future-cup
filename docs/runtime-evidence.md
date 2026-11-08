# 运行验证证据 · 2026-11-08

本文件记录后续实测，不覆盖 `runtime-evidence.json` 中第一轮“无 Docker、模拟模型受阻”的历史快照。时间为 Asia/Shanghai。

**最新结果：** `deepseek-v4.1-flash` 的四项真实开发复测全部通过；三项明示预设缺陷也已在标准 Engine 中经一轮真实模型自动修正通过。自然代码失败后修正仍未观察到。完整批次、失败保留、协议修复和复现包见 [多任务评测](multi-task-evaluation.md)。下文保留早期 403、模拟验证、最初单项真实模型与独立修正探针的历史时间线，不覆盖旧结果。

## Docker 隔离与验收 · 已实测

2026-11-08 15:21–15:25，使用专用 WSL2 发行版 `AgentForge` 内的 Docker Engine，Windows 后端通过受控 WSL 参数调用执行器。`.env` 的 `AGENTFORGE_DOCKER_BACKEND=wsl`、`AGENTFORGE_WSL_DISTRO=AgentForge` 已配置，其他字段与 API Key 保留不变。

使用镜像 `agentforge-test:py313`，实测镜像 ID：

```text
sha256:1edc3cba75dd1917bb78226e897fdb801a0272ea2e02319345de0bd3b8cae225
```

这只是本地构建的镜像 ID，不是可从远程仓库重新拉取的发行版 digest。候选源码仅在受控容器的 worker 内执行，没有在 Windows 宿主 Python 中导入或执行。

### 六项真实容器探针

执行：

```powershell
.\.venv\Scripts\python.exe scripts\probe_sandbox.py --include-timeout
```

结果：全部达到预期，总耗时 42.81 秒。证据保存在 `.agentforge/sandbox-probe.json`，候选均明确标为合成测试样例，不是 LLM 生成结果。

- 正确参考候选：10 项独立验收通过，退出码 0。
- 预设遗漏筛选：验收失败，退出码 1。
- 使用类变量共享任务状态：验收失败，退出码 1，证明多实例测试没有被不同解释器隔离掩盖。
- 返回可变内部字典别名：验收失败，退出码 1，证明引用变更代理保留了拷贝语义检查。
- 伪造成功标记后退出：验收失败，退出码 1，候选 stdout 没有成为可信验收结果。
- 忙循环候选：约 24.69 秒后因 RPC 超时被拒，退出码 1。这次由 RPC 超时机制结束，不宣称外层 25 秒 Docker 强制终止分支已被触发。

探针结束后，`docker ps --filter name=agentforge-` 没有残留运行容器。

### 容器边界与实际 Engine 反馈闭环

执行：

```powershell
.\.venv\Scripts\python.exe scripts\probe_workflow.py
```

容器内的合成候选检查并确认：有效 UID 为 65534、有效 capabilities 为 0、NoNewPrivs 为 1、Seccomp 为 2、网络命名空间只有 `lo` 接口；向候选目录、独立验收目录与根文件系统写入均被拒绝。带这些前置断言的正确参考候选实际通过独立验收。

随后运行真正的 Engine、SQLite、文件工具与 Docker 测试，只将模型适配器设为明确标注的 `mock`。该运行没有调用模型服务，没有应用候选到目标项目。

- 运行 ID：`f6983cf9a91c45d793f849201f988bc7`。
- 首轮：实际运行 10 项测试，2 项失败，退出码 1。
- 平台记录反馈事件，调用模拟修正角色，写入真实候选副本。
- 修正第 1 轮：实际运行 10 项测试，全部通过，退出码 0；审查门控通过。
- 最终状态 `succeeded`，`attempt=1`，`model_calls=0`，`applied=false`。
- 边界检查与工作流合计耗时 16.66 秒。

完整运行、事件、步骤、差异和测试输出保存在 `.agentforge/verification/20261108T072425Z-69e01a/evidence.json`，使用独立验证数据库，不污染前端已有运行历史。

以上证明隔离执行和“实际测试失败→反馈→候选修正→实际测试通过”的平台流程可用，**不代表真实 LLM 自我修正已通过**。同容器、同 UID 的父测试进程与候选 worker 仍不是面向任意恶意代码的强对抗沙箱；本次边界探针只证明列出的具体限制实际生效。

### 浏览器恢复历史运行 · 已实测

15:27，通过工作台“检查并恢复”继续原先被 Docker 缺失阻塞的模拟开发运行 `498280fc3f0b4c79b7f5224a6fdc5e2a`。复用原规划和开发快照，实际容器首轮失败、反馈修正后通过；最终 `succeeded`、`attempt=1`、`model_calls=0`、`applied=false`，累计 108 条事件。页面实时轮询更新为“已通过”，显示隔离测试可用和“模拟模型”标签。

脱敏 API 导出为 `.agentforge/evidence/browser-mock-docker-resumed.json`；截图为 `output/playwright/docker-workflow-passed.png`。原有阻塞事件保留为历史，不被成功结果覆盖。没有点击“确认应用”，目标示例仍为原始版本。

浏览器验证后已停止本轮启动的 8000/5173 预览服务，Docker Engine 保持可用。用户替换新密钥后按 README 重新启动后端，确保加载新配置。

## 历史：新项目密钥 · 充值前真实 API 受阻

用户完成 `AgentForge-dev` 本地替换后，15:37 发出一次工具探测，仍 HTTP 403，模型未产生输出；15:40 模型列表 HTTP 200，但为空。原始密钥未记录；脱敏证据是 `.agentforge/model-probe-new-key.json` 和 `.agentforge/model-list-new-key.json`。

此时再次执行 `scripts/preflight.py`，Docker 后端 `wsl`、发行版 `AgentForge`、验收镜像均可用。预检中模型 `configured=true`、`verified=false`，准确区分配置存在与调用成功。由于新密钥仍被拒，没有重复启动整套真实工作流，也没有把模拟模式结果改标真实。

新密钥创建没有解决 403；平台授权/竞赛权益仍待平台核查。完整分析和未发送的核查说明见 [模型诊断](model-diagnosis.md)。

## 充值后真实模型验证 · 已实际运行

15:52，用户报告充值后，原 `deepseek-v4-pro` 从 403 变为 503，平台明确表示没有可用通道；可访问模型列表返回 8 条，原 Pro 不在其中。保持同一 Key 和 Base URL，项目模型改为实际列表中的 `deepseek-v4.1-flash`，没有回退模拟。

### 协议探测

`.agentforge/model-probe-flash.json`：2 次真实请求完成 `read_project` 工具往返及 JSON 输出校验，564 tokens，5.329 秒，`passed=true`。该结果只证明此模型当次协议可用。

### 真实开发 · 首轮通过

运行 `scripts/run_live_demo.py --require-repair`，实际通过本地 API 建立工作流：

- ID：`e9c8badd6c5a4dfd9aad4e9710b5e6da`，`mode=live`、`status=succeeded`、`attempt=0`、`applied=false`。
- 7 次模型请求、3 次只读模型工具调用、19,238 tokens；工具和 usage 来自实际返回，计量完整。
- 10 项独立 Docker 验收全部通过，真实静态初审与最终审查通过；44 条事件，约 18.81 秒。
- **没有修正轮次。** `--require-repair` 的退出码 3 表示没有观察到修正，不表示这次开发失败。
- 脱敏证据：`.agentforge/evidence/live-e9c8badd6c5a4dfd9aad4e9710b5e6da.json`。

浏览器已核查真实模型标签、模型调用已验证、隔离可用、修正轮次 0 和候选 diff；无须修正的条件分支显示“已跳过”。截图：`output/playwright/live-development-passed.png`。

### 真实审查 · 正确发现不完整代码

运行 `scripts/run_live_demo.py --workflow review`：

- ID：`57585159b48a45e6b12e1ef0f69718db`，`mode=live`、`status=needs_human`、`attempt=0`、`applied=false`。
- 目标示例没有应用开发候选，因此仍缺少完成状态、complete 方法和筛选接口；真实 10 项验收中 7 项 ERROR，真实审查明确指出上述问题，未伪报通过。
- 7 次模型请求、4 次模型工具调用、19,867 tokens；37 条事件，约 15.61 秒。
- 这是只读审查的缺陷发现结果，脚本退出码 2；没有调用修正角色。
- 证据：`.agentforge/evidence/live-57585159b48a45e6b12e1ef0f69718db.json`；浏览器截图：`output/playwright/live-review-rejected.png`。

### 明示合成缺陷 · 真实 fixer 修复与复验

执行 `scripts/probe_live_repair.py --max-repairs 2 --review`。这是独立实验，不修改上面的真实开发运行、不改变其模式、不向其候选注入错误，也不修改主数据库。

- 起始源码来自明确标注的固定合成样例：`list(completed)` 遗漏筛选。文件与 SHA256 在首次模型调用前保存。
- 必须先在真实 Docker 中完整执行 10 项验收，准确复现 `test_filter_completed`、`test_filter_incomplete` 两项断言失败，才会调用模型；基础设施异常不作为合格的修正输入。
- 真实 fixer 根据真实失败反馈完成 1 轮修复；修正后 10 项 Docker 验收全部通过，随后真实 reviewer 通过。
- 共 4 次模型请求（fixer 3 次、reviewer 1 次）、16,883 tokens，18.73 秒；计量完整，`applied=false`。
- 验收文件哈希未变化，候选在测试前后未变化，修复文本、diff、反馈、工具调用和每轮哈希完整保存。
- 证据：`.agentforge/live-repair-probes/20261108T080014Z-d3e46e/evidence.json`。
- `live_fix_of_synthetic_defect_verified=true`，`live_review_passed=true`；`natural_model_development_failure=false`，`original_workflow_self_correction=false`。

因此已验证真实模型对预设缺陷的修复与再验收；**没有将它说成首次真实开发自然失败，也没有宣称主工作流自然自修正已得到样本证明。**

### 代码检查与保留边界

新增服务渠道/授权错误分类、真实修正探针、严格事件顺序/hash/候选写入/真实测试证据判定，以及首轮成功时修正分支的跳过显示。最终平台检查：**84 passed, 46 subtests passed，14.36 秒**；前端 TypeScript 与 Vite 生产构建通过。测试中不调用真实模型；上面的真实请求与容器运行独立记录。

这轮成功模型请求共 20 次（协议 2、开发 7、审查 7、独立修正 4），已知 usage 合计 56,552 tokens；另有 1 次 Pro 503 和 1 次模型列表 GET。实际账单费用未查询，失败请求用量不假定为零。

所有目标源码保持原始版本，候选未应用；没有 Git 提交或推送。真实模型成功、合成缺陷修复、模拟闭环和平台单元测试分开保存。在该阶段结束时，换机复现、多任务与调度对照实验尚未完成；后续进展见下方补充记录。

结束前核对 80 个项目文本/探测证据文件，未发现完整本地 Key；目标源码与真实开发基线完全一致。本轮启动的 8000/5173 预览服务已停止，无残留 `agentforge-` 测试容器；Docker Engine 保持可用。按 README 启动即可查看已保存运行，重启后的“模型待验证”表示当前服务进程尚未再发请求，不会抹去历史成功证据。

## 后续：多任务与三策略对照

多任务真实开发及三项明示预设缺陷的主工作流修正已经完成，见[多任务证据](multi-task-evaluation.md)。随后已采集同模型、同任务、同预算上限的 24 次三策略对照：单 Agent、固定串行、动态并行功能通过数为 6/8、6/8、8/8，完整流程为 6/8、6/8、7/8。共 174 请求，已知 1,150,622 tokens；4 次 HTTP 429 的 usage 未知，13 次格式重试单列，没有自然候选测试失败。

第 15 次因本地 Windows 原子保存失败而中断，原阶段保持 blocked；修复保存重试后，新预注册阶段只提交原 16–24 号，不重跑此前成功或失败。全部证据、共同评价标准、两阶段限制、287 项平台回归与新的对照复现包见[对照记录](comparison-evaluation.md)。另一台机器从零复现仍待验证。
