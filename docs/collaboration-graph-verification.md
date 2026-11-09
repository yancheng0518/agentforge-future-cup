# 协作图与事件回放验证 · 2026-11-08

浅色对话页现已增加“查看协作图”。图是已有运行事件的只读视图：节点、任务依赖、Docker 验收和反馈来自本次运行，不预设固定 Agent 名称、数量或工作阶段。旧记录和旧复现包保留。

## 使用

1. 按 README 启动 Docker、后端和前端，访问 <http://127.0.0.1:5173>，输入标准库 Python 项目需求并发送。
2. 点击执行状态或任务分工旁的“查看协作图”，可“放大查看”。点击主 Agent、子任务、工具或检查节点，查看实际指令、文件范围、结果、验收计数和来源事件。
3. 拖动时间轴，或使用上一步、下一步、播放、暂停。回放以每秒一条事件切换，不按实际耗时比例播放；不会重新运行任务或调用模型。“回到实时”恢复跟随新事件。
4. 历史记录也可打开图。信息不足时只显示“最终记录”，明确提示不能完整回放，不补造过去的状态。按 Escape 或关闭按钮返回对话。

当前任务示例：<http://127.0.0.1:5173/#task=auto%3A9f25467d40074e0eb520e178b00b11e1>。首次进入链接后点击“查看协作图”。图的位置不代表同时执行；连线表示派发、依赖、工具与反馈，不表示 Agent 私聊。

## 事件和真实性

- 正常回放只归约所选事件及此前内容。任务在实际建任务/派发事件出现后才显示；尚未执行的决定不能当成执行。回到第一条事件不会显示最终任务、产出和成功状态。
- 实时运行时切到历史位置，后续轮询不会把游标推到终点。播放可暂停，页面隐藏时暂停；减少动画设置下允许主动逐条播放，关闭过渡动画。
- 新增 `verification_started` 只表示“已请求验收，等待容器回执”。完成回执同时确认 `isolated` 和 `container_invoked` 后才称实际 Docker 隔离执行。异常或取消保存 `verification_interrupted`；没有完成回执不能声称测试已执行完。
- 旧运行没有开始事件时不补写开始时间。对尚未确认完成的任务/工具/检查，在运行中断或结束时显示完成未确认。
- 初始脚手架/项目基线、公开预设缺陷、AI 生成验收下的候选失败、格式重试和模型传输错误分别显示。AI 测试不通过仍可能是验收错误，不自动归因于代码缺陷。
- 事后核查进入时间轴后，错误的原始成功会改为需要处理，主节点摘要显示复核结论。原始状态和摘要保留在节点数据里；此前的历史帧仍忠实显示当时记录。

## 本轮真实模型和 Docker

本轮只有 **一次新界面提交**，精确需求保存在 `output/collaboration-graph-20261108/ui-request.txt`。需求是 ASCII 分词、词频排序、limit 校验和完整报告两个模块。浏览器实际填写并发送，没有用 API 冒充界面提交。

- 运行 ID：`9f25467d40074e0eb520e178b00b11e1`，本地时间 22:04:47–22:05:45，约 58 秒。
- 平台模型 `deepseek-v4.1-flash`，适配器 `autonomous-5-proportional-review`；9 次真实请求、58,713 tokens，0 格式重试、0 传输失败、0 未知用量。
- 使用默认预算：40 次请求、300,000 tokens、单次最大输出 8,192、900 秒，未为该任务放宽限制。请求超时按启动脚本为 120 秒。
- 主 Agent 创建 `impl-tokenize` 与依赖它的 `impl-report`，分配两个实际子 Agent，4 次决定，41 条事件。没有预设角色或强制并行。
- 未实现脚手架首次 Docker 检查有 12 项错误；这是输入基线检查，不算自然代码失败。实现后首次候选的 **12/12 AI 生成冻结验收通过**，最终模型语义审查通过。
- 候选 SHA-256：`60e8c83d95a64c03ffdc13311a3541b32b532b26657cb276fa8058dee38cee7a`。
- 两组 `verification_started → verification` 的序号、候选哈希和回执匹配；使用专用 WSL Docker，实际镜像 ID `sha256:1edc3cba75dd1917bb78226e897fdb801a0272ea2e02319345de0bd3b8cae225`。

本轮自然候选失败 **0**、自然代码修正 **0**、预设缺陷 **0**。这是开发首轮成功样本，不是自然自修正成功样本。所有模型请求、候选、冻结测试、实现快照和容器回执保存在 `.agentforge/autonomous/runs/9f25467d40074e0eb520e178b00b11e1/`。

## 预先声明的额外验收

在发送需求之前，已保存 `verify_text_report.py` 和 `verification-declaration.json`，冻结需求、脚本和独立测试套件的 SHA-256。运行结束后，复制相同候选到新检查目录，在 Docker 执行这套 8 项验收：ASCII/Unicode 分隔、空文本、计数和并列排序、有效/无效 limit、截断前总数。

结果 **8/8 通过、0 失败、0 错误、0 模型调用**；原候选和原运行所有文件哈希未变。证据：`.agentforge/collaboration-graph-checks-20261108/20261108T140701Z/evidence.json`。审计确认声明早于运行，声明的三项哈希与实际脚本均一致。该结果是额外检查，未回写或覆盖运行中的 AI 验收。

```powershell
# 对已保存候选复核；只在 Docker 执行，每次写入新的时间目录
.\.venv\Scripts\python.exe output\collaboration-graph-20261108\verify_text_report.py --run-id 9f25467d40074e0eb520e178b00b11e1

# 只读审计，不调用模型或执行候选；使用新报告名
.\.venv\Scripts\python.exe scripts\audit_autonomous.py --data-dir .agentforge\autonomous --output output\collaboration-audit-new.json
```

新的模型调用不保证产生相同代码、相同分工或用量。需要重跑开发时，在界面新建任务并粘贴保存的需求，保留原运行，再对新运行 ID 执行独立检查；不要改测试适应候选结果。

## 浏览器验证与旧失败回放

- 新任务实时显示 tokenizer 已完成、report 正在执行及实际依赖。回放停在事件 1 时，后台事件数从 38 增至 41，游标仍为 1，图中只有当时的主 Agent。
- 点击任务查看实际指令和文件，点击 Docker 查看 12 项测试、0 失败/错误及隔离回执。播放从事件 1 推进至 20，暂停有效，返回实时恢复末尾。
- 旧成功 `05002763…` 的回放从起点重建任务，后出现的节点不会泄漏结果；选中尚未出现的节点明确提示未出现。
- 旧失败案例 `d2b9e645…` 在事件 22 显示候选测试失败，事件 26 新增排序修正任务；最终事件 69 显示事后核查失败、主 Agent 需要处理。它曾迎合冲突的 AI 验收，**不能算自然自修正成功**。原始失败、格式重试和错误成功完整保留。
- 模态放大及窄屏状态下，背景不可交互，Ctrl+K 不跳出面板，Escape 关闭后焦点回到“查看协作图”。修复了初次实现的背景焦点与关闭后焦点丢失问题。
- 390×844 下页面 client/scroll width 均为 390，节点详情可操作，无页面横向溢出；已恢复默认视口。浏览器控制台错误记录为空。
- 回放前后旧五个动态运行的模型请求数保持 8/4/5/2/16，仅新增上述一条 9 请求任务。证据为 `browser-replay-before.json`、`browser-replay-after.json`，播放组件也没有 API 写入路径。

截图和浏览器记录在 `output/collaboration-graph-20261108/`。`collaboration-graph-final.png` 展示真实任务与 Docker 节点；`historical-posthoc-rejected.png` 展示被复核否定的旧成功；`mobile-graph.png` 展示窄屏布局。这是局部交互验证，未完成正式无障碍审计。

## 回归、保存与复现包

- 后端 **464 passed, 165 subtests passed**，73.15 秒；包括新增的准备/完成/取消/异常验收事件测试。
- 前端 **66 项通过**，其中 18 项协作图事件投影测试；TypeScript 与 Vite 生产构建通过。测试使用合成事件及只读历史样本，不算真实模型效果证据。
- `review-final.md` 保存代码审查；`frontend-final-*` 保存最终命令日志和源码哈希。后端日志为 `pytest-final.txt`。
- `preservation-before.json` / `preservation-after.json` 确认 1,495 个既有证据及归档文件哈希不变；原主库 17 条、原动态库 5 条、旧验证库 3 条记录均未改写，仅动态库新增本轮运行。界面共 23 条记录。
- 全动态库只读审计通过，仍保留三个“准备阶段结束，未生成候选”的历史警告；本轮新运行审计无问题。原始 succeeded 共 3 条，其中 1 条被事后否定，不能用原始状态计作 3 次有效完成。
- 新归档使用 `output/agentforge-collaboration-graph-20261108.zip`，旧包不会被覆盖。包含源码、本轮证据、完整动态及旧工作台历史、独立 Docker 检查和一致性数据库备份；排除 `.env`、依赖缓存和数据库 sidecar，检查配置凭据字节并逐成员核验哈希。具体包哈希与文件数见 `output/collaboration-graph-20261108/archive-manifest.json` 和 ZIP 旁的 `.sha256`。

归档只解压到全新空目录，不覆盖当前项目。包内 `.agentforge/agentforge.sqlite3` 与 `.agentforge/autonomous/agentforge.sqlite3` 是 SQLite backup API 生成的一致性快照，合计 23 条界面历史；在新目录按 README 重建依赖并启动即可读取记录，无需复制数据库覆盖当前工作区。模型和 Docker 的重新配置见包内 `COLLABORATION_GRAPH_RESTORE.md`。仅核验包时可运行：

```powershell
.\.venv\Scripts\python.exe scripts\export_reproduction.py --verify output\agentforge-collaboration-graph-20261108.zip
```

前端测试中的两个真实历史 fixture 随归档保存，不能只取源码后将缺 fixture 的测试误判为业务失败。换机从零安装尚未实测；AI 验收/语义审查仍可能出错；运行范围仍是标准库 Python 多模块和 JSON 函数接口。候选没有应用到用户原项目，没有向外部仓库提交或发布。
