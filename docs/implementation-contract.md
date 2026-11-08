# 第一版协作接口

后端目录 `backend/agentforge`。前端 React + TypeScript + Vite，目录 `frontend`。API 前缀 `/api`，Vite 代理至 `127.0.0.1:8000`。

## Runtime（由 runtime.py 提供）

`Runtime(root: Path, data_dir: Path)`。所有公开操作为 async。

- `health()` -> `{model: {configured, model, base_url}, sandbox: {available, reason}, mode: string}`，无密钥值。
- `snapshot_task(task_id)` 返回固定白名单任务的基线、需求、验收及哈希（同步读取，不执行）。`prepare(run_id, task_snapshot=None)` -> `{workspace, base_hash, ...}`，复制到 data/runs/{id}/candidate，所选任务验收复制到独立 trusted 目录。历史单任务调用保持兼容。
- `read_project(run_id)` -> `{files: [{path, content}]}`。
- `retrieve(query, run_id=None)` -> `{matches: [{path, excerpt, score}]}`；有任务快照时首先返回冻结的当前任务契约。
- `invoke(agent, task, context)` -> dict。agent 含 role,prompt,model；task 含 mode(mock/live),requirement,kind；context 含 project,retrieval,test,review,attempt。返回 `{output: dict, model_mode, model_calls, duration_ms, tool_calls: list}`；role 为 planner/developer/tester/reviewer/fixer。mock 必须固定标识；首轮有意遗漏筛选行为，用于演示修正，测试仍只能容器运行。
- `apply_candidate(run_id, files)` -> diff dict；files 为 `[{path,content}]`，只允许 todo.py。
- `test(run_id)` -> `{status: passed|failed|blocked, exit_code, stdout, stderr, isolated, reason, duration_ms}`。隔离不可用不得宿主执行候选代码。
- `diff(run_id)` -> `{files: [{path,before,after,diff}], unified_diff}`。
- `apply(run_id)` -> `{applied, files}`，仅显式确认 API 调用；基线冲突拒绝。

## HTTP

- GET `/health` Runtime health 加服务状态。
- GET `/agents` -> Agent 数组，字段 id,name,role,capabilities,enabled,version,prompt,model,tools。
- POST `/agents` / PUT `/agents/{id}` -> 保存配置生成新版本。
- GET `/workflows` -> 模板数组 id,name,description,nodes；nodes id,label,capability,depends_on,kind。
- GET `/tasks` -> 四项任务 id/name/requirement/expected_tests/路径和基线、验收、需求哈希。不返回参考实现或合成控制。
- GET `/knowledge` -> `{matches:[...]}`，可带 q。
- GET `/project?task_id=completion` -> 对应任务基线 `{files:[{path,content}]}`，task_id 必须来自白名单。
- GET `/runs` -> 运行数组，字段 id,requirement,workflow_id,mode,status,created_at,updated_at,attempt,summary。
- POST `/runs` body `{requirement,workflow_id: development|review,mode: mock|live,task_id: completion|bulk_edit|priority_query|json_merge}` -> 新运行（含 id）。后台启动，冻结任务与模型条件；默认 completion。扩展评测任务仅支持 live，mock 只覆盖完成状态任务。
- GET `/runs/{id}` -> 运行详情，加 events 数组、steps 数组、snapshots 数组、diff 对象。events 为 `{id,timestamp,type,node_id,message,data}`；steps 为 `{id,label,status,agent_id,agent_name,attempt,output}`。
- POST `/runs/{id}/resume` -> 恢复尚未完成节点。
- POST `/runs/{id}/apply` body `{confirmed:true}` -> 应用候选，未验证成功/非development不得应用。

扩展评测任务的固定基线禁止应用覆盖；候选副本与每版源码保留。completion 仍要求明确确认及基线/候选哈希一致。模型请求开始时间在进程内至少间隔4.2秒（可配置），控制并行调用速率；不是无限重试机制。

运行状态 `queued,running,blocked,failed,needs_human,succeeded`；blocked 指明确前置条件缺失，不等于通过。事件 type 建议 schedule/tool/agent/validation/feedback/snapshot/error/status。

工作流先计划与开发，再并行测试与审查；验证子工作流、条件分支和最多两轮修正。节点输出持久保存。review 模板不修改文件。SQLite 记录运行时配置版本。平台测试可用明示 stub 验证调度逻辑，但不能把 stub 记录当成真实隔离测试。
