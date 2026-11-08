"""Config-driven DAG execution, bounded repair, explainable dispatch and checkpoints."""
import asyncio
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .definitions import WORKFLOWS
from .storage import Store, now
from .task_catalog import TASKS, public_task


class Blocked(RuntimeError):
    pass


def candidate_fingerprint(diff):
    files = [{"path": f["path"], "after": f["after"]} for f in diff.get("files", [])]
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class Engine:
    def __init__(self, store: Store, runtime):
        self.store, self.runtime = store, runtime
        self.loads = {}
        self.running = set()
        self.tasks = {}

    def create_run(self, requirement, workflow_id="development", mode="mock", task_id="completion"):
        if workflow_id not in WORKFLOWS:
            raise ValueError("未知工作流")
        if mode not in ("mock", "live"):
            raise ValueError("未知模型模式")
        if task_id not in TASKS:
            raise ValueError("未知受控任务")
        if task_id != "completion" and mode != "live":
            raise ValueError("扩展评测任务仅支持真实模型；固定模拟适配器仅用于完成状态任务。")
        task_snapshot = self.runtime.snapshot_task(task_id) if hasattr(self.runtime, "snapshot_task") else None
        if task_id != "completion" and task_snapshot is None:
            raise ValueError("当前运行器不支持任务快照")
        run_id = uuid4().hex
        run = dict(id=run_id, requirement=requirement, workflow_id=workflow_id, mode=mode,
                   status="queued", created_at=now(), updated_at=now(), attempt=0,
                   summary="等待执行", context={}, applied=False, model_calls=0,
                   model_tokens=0, model_metrics_complete=True, agent_invocations=0,
                   agent_versions=deepcopy(self.store.agents()),
                   workflow=deepcopy(WORKFLOWS[workflow_id]))
        run.update(task_id=task_id, task_snapshot=task_snapshot,
                   task=public_task(task_snapshot) if task_snapshot else None,
                   candidate_origin="live_model_development" if mode == "live" else "preset_mock",
                   fault_injection=False,
                   model_config=self.runtime.model_config() if hasattr(self.runtime, "model_config") else None)
        self.store.create_run(run)
        self.store.event(run_id, "status", "运行已建立", data={"mode": mode, "workflow_version": run["workflow"]["version"]})
        if mode == "mock":
            self.store.event(run_id, "status", "模拟模型模式：使用确定性候选；首轮预设筛选缺陷用于演示，不能作为真实 LLM 验收。")
        return run

    def start(self, run_id):
        task = self.tasks.get(run_id)
        if task and not task.done():
            return task
        task = asyncio.create_task(self.execute(run_id))
        self.tasks[run_id] = task
        return task

    async def execute(self, run_id):
        if run_id in self.running:
            return
        run = self.store.run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] == "succeeded":
            return
        self.running.add(run_id)
        self.store.update_run(run_id, status="running", summary="工作流执行中")
        self.store.event(run_id, "status", "开始执行；已完成节点将从快照复用。")
        context = deepcopy(run.get("context", {}))
        # A fix checkpoint can commit just before the process stops, before the
        # run's attempt counter advances. The persisted fix is the authority.
        completed_repairs = [int(match.group(1)) for step in self.store.steps(run_id)
                             if step["status"] == "succeeded" and (match := re.fullmatch(r"round-(\d+)/fix", step["id"]))]
        recovered_attempt = max([run.get("attempt", 0), *completed_repairs])
        context.update(attempt=recovered_attempt, requirement=run["requirement"], run_id=run_id)
        try:
            if run.get("model_config") and hasattr(self.runtime, "model_config") and run["model_config"] != self.runtime.model_config():
                raise Blocked("模型配置与创建运行时不同；请恢复原配置或新建运行，不混用评测条件。")
            if run.get("task"):
                context["task_contract"] = deepcopy(run["task"])
            initial = [n for n in run["workflow"]["nodes"]
                       if n["operation"] not in ("subworkflow", "condition") and not n.get("conditional")]
            await self._dag(run, initial, context)
            verify = next(n for n in run["workflow"]["nodes"] if n["operation"] == "subworkflow")
            max_repairs = run["workflow"]["max_repairs"]
            while True:
                attempt = context["attempt"]
                self.store.update_run(run_id, attempt=attempt)
                verify_id = f"round-{attempt}/verify"
                # Evidence is valid only for the candidate bytes that were tested.
                current_diff = await self.runtime.diff(run_id)
                current_hash = candidate_fingerprint(current_diff)
                validation_hashes = context.setdefault("validation_hashes", {})
                if verify_id in validation_hashes and validation_hashes[verify_id] != current_hash:
                    self.store.invalidate_steps(run_id, verify_id)
                    self.store.event(run_id, "validation", "候选已变化；旧验证节点失效，将重新测试与审查。", verify_id)
                validation_hashes[verify_id] = current_hash
                self._begin_validation(context, verify_id, current_hash)
                context["diff"] = current_diff
                context["project"] = await self.runtime.read_project(run_id)
                self.store.update_run(run_id, context=context)
                await self._node(run, verify, context, verify_id)
                test = context.get("test", {})
                review = context.get("review", {})
                issues = list(review.get("issues") or [])
                passed = test.get("status") == "passed" and test.get("isolated") is True and test.get("exit_code") == 0 and review.get("passed") is True
                gate_id = f"round-{attempt}/gate"
                self.store.step(run_id, dict(id=gate_id, label="验收条件判断", status="succeeded", attempt=attempt,
                                           output={"passed": passed, "test_status": test.get("status"), "issues": issues}))
                self.store.event(run_id, "validation", "全部验收条件满足" if passed else "验收未通过", gate_id,
                                 {"passed": passed, "test": test, "review": review, "attempt": attempt})
                if passed:
                    summary = "独立隔离测试及审查均通过；候选修改尚未应用。" if run["workflow_id"] == "development" else "审查流程完成；独立测试与审查均通过。"
                    if run["mode"] == "mock":
                        summary = "模拟模型运行：" + summary
                    if candidate_fingerprint(await self.runtime.diff(run_id)) != current_hash:
                        raise Blocked("验收期间候选内容发生改变；请恢复运行以重新验证当前内容。")
                    self.store.update_run(run_id, status="succeeded", summary=summary, context=context, verified_hash=current_hash)
                    break
                if attempt >= max_repairs:
                    self.store.update_run(run_id, status="needs_human", summary="审查发现问题，需人工处理。" if max_repairs == 0 else "已达到两轮修正上限，需人工处理。", context=context)
                    break
                self.store.event(run_id, "feedback", "将具体验收问题交给修正 Agent", gate_id,
                                 {"test": test, "issues": issues, "next_attempt": attempt + 1})
                # Repair is itself checkpointed. A restart after repair reuses it.
                fix = next(n for n in run["workflow"]["nodes"] if n.get("kind") == "loop" and n.get("conditional"))
                await self._node(run, fix, context, f"round-{attempt + 1}/fix")
                context["attempt"] = attempt + 1
                self.store.update_run(run_id, attempt=attempt + 1, context=context)
            self.store.event(run_id, "status", self.store.run(run_id)["summary"])
        except Blocked as exc:
            self.store.update_run(run_id, status="blocked", summary=str(exc), context=context)
            self.store.event(run_id, "error", str(exc), data={"recoverable": True})
        except asyncio.CancelledError:
            self.store.update_run(run_id, status="blocked", summary="执行被服务停止中断，可从快照恢复。", context=context)
            self.store.event(run_id, "status", "执行被中断；已完成节点快照已保存。", data={"recoverable": True})
            raise
        except Exception as exc:
            self.store.update_run(run_id, status="failed", summary=f"执行失败：{type(exc).__name__}: {exc}", context=context)
            self.store.event(run_id, "error", f"{type(exc).__name__}: {exc}", data={"recoverable": True})
        finally:
            self.running.discard(run_id)

    @staticmethod
    def _begin_validation(context, verify_id, candidate_hash):
        """Separate old-round feedback from current evidence before parallel work.

        Completed nodes hydrate their output on reuse. Clearing current fields
        therefore works for both new rounds and partially completed resumes.
        """
        scope = {"id": verify_id, "attempt": context["attempt"], "candidate_hash": candidate_hash}
        old_scope = context.get("validation_scope")
        prior = {key: deepcopy(context[key]) for key in ("test", "inspect", "review") if key in context}
        if prior and old_scope != scope:
            context["previous_validation"] = {"scope": deepcopy(old_scope), **prior,
                                              "note": "历史验证，仅供理解修正反馈，不是当前候选的验证结果。"}
        for key in ("test", "inspect", "review"):
            context.pop(key, None)
        context["validation_scope"] = scope

    @staticmethod
    def _restore_output(node, output, context):
        if not isinstance(output, dict):
            return
        if node["operation"] == "test":
            context["test"] = deepcopy(output.get("test", {}))
        elif node["operation"] == "subworkflow":
            for key in ("test", "inspect", "review"):
                if key in output:
                    context[key] = deepcopy(output[key])
        elif node["id"] in ("inspect", "review"):
            context[node["id"]] = deepcopy(output)

    async def _dag(self, run, nodes, context, prefix=""):
        remaining = {n["id"]: n for n in nodes}
        if len(remaining) != len(nodes):
            raise RuntimeError("工作流节点 ID 重复")
        done = set()
        allowed = set(remaining)
        persisted = self.store.steps(run["id"])
        external_done = {s.get("node_id", s["id"].rsplit("/", 1)[-1]) for s in persisted if s["status"] == "succeeded"}
        for n in nodes:
            missing = [d for d in n["depends_on"] if d not in allowed and d not in external_done]
            if missing:
                raise RuntimeError(f"节点 {n['id']} 的外部依赖未完成或不存在：{', '.join(missing)}")
        while remaining:
            ready = sorted([n for n in remaining.values()
                            if all((d in done) if d in allowed else (d in external_done) for d in n["depends_on"])],
                           key=lambda n: (-n["priority"], n["id"]))
            if not ready:
                raise RuntimeError("工作流存在循环依赖或未满足的依赖")
            results = await asyncio.gather(*(self._node(run, n, context, prefix + n["id"]) for n in ready), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            done.update(n["id"] for n in ready)
            for n in ready:
                remaining.pop(n["id"])

    async def _node(self, run, node, context, step_id):
        run_id = run["id"]
        previous = next((s for s in self.store.steps(run_id) if s["id"] == step_id), None)
        if previous and previous["status"] == "succeeded":
            self._restore_output(node, previous.get("output"), context)
            self.store.event(run_id, "snapshot", "复用已完成节点；不重复执行工具", step_id)
            return previous.get("output")
        step = dict(id=step_id, node_id=node["id"], label=node["label"], status="running",
                    attempt=context["attempt"], started_at=now(), output=None)
        if repair := re.fullmatch(r"round-(\d+)/fix", step_id):
            step["attempt"] = int(repair.group(1))
        self.store.step(run_id, step)
        self.store.event(run_id, "status", f"开始：{node['label']}", step_id)
        try:
            op = node["operation"]
            if op == "prepare":
                prepare = self.runtime.prepare(run_id, run["task_snapshot"]) if run.get("task_snapshot") else self.runtime.prepare(run_id)
                prepared = await self._tool(run_id, step_id, "prepare_workspace", prepare, {"task_id": run.get("task_id", "completion")})
                retrieval = self.runtime.retrieve(run["requirement"], run_id) if run.get("task_snapshot") else self.runtime.retrieve(run["requirement"])
                project, retrieval = await asyncio.gather(
                    self._tool(run_id, step_id, "read_project", self.runtime.read_project(run_id), {"run_id": run_id}),
                    self._tool(run_id, step_id, "knowledge_search", retrieval, {"query": run["requirement"]}))
                context.update(project=project, retrieval=retrieval, prepared=prepared)
                output = {"files": [f["path"] for f in project["files"]], "references": retrieval}
            elif op == "subworkflow":
                self.store.event(run_id, "status", "进入验证子工作流：测试与初步审查并行，随后汇总审查。", step_id,
                                 {"children": [n["id"] for n in node["children"]], "parallel": ["test", "inspect"]})
                await self._dag(run, node["children"], context, step_id + "/")
                output = {"test": context.get("test"), "inspect": context.get("inspect"), "review": context.get("review"),
                          "validation_scope": deepcopy(context.get("validation_scope"))}
            elif op == "test":
                # Assignment is logged before the trusted test runner is invoked.
                agent = self._select(run, node, step_id)
                step.update(agent_id=agent["id"], agent_name=agent["name"], agent_version=agent["version"])
                self.store.step(run_id, step)
                try:
                    test = await self._tool(run_id, step_id, "run_acceptance", self.runtime.test(run_id), {"run_id": run_id, "independent_tests": True})
                    test = {**test, "evidence_scope": deepcopy(context.get("validation_scope"))}
                    context["test"] = test
                    if test.get("status") == "blocked":
                        raise Blocked(test.get("reason") or "隔离测试环境不可用；未执行候选代码。")
                    if test.get("isolated") is not True:
                        raise Blocked("测试结果没有可靠隔离证明，不能作为验收结果。")
                    result = await self._invoke(run, node, step_id, agent, context)
                    output = {"test": test, "assessment": result["output"]}
                finally:
                    self._release(agent)
            else:
                result, agent = await self._dispatch(run, node, context, step_id)
                step.update(agent_id=agent["id"], agent_name=agent["name"], agent_version=agent["version"])
                output = result["output"]
                if node["id"] in ("inspect", "review"):
                    output = {**output, "evidence_scope": deepcopy(context.get("validation_scope"))}
                if op == "edit":
                    if run["workflow_id"] != "development":
                        raise RuntimeError("审查工作流禁止修改候选")
                    files = output.get("files")
                    if not isinstance(files, list) or not files:
                        raise ValueError("开发 Agent 未返回有效的 files 候选列表")
                    diff = await self._tool(run_id, step_id, "candidate_diff", self.runtime.apply_candidate(run_id, files), {"paths": [f.get("path") for f in files]})
                    context["diff"] = diff
                    context["project"] = await self._tool(run_id, step_id, "read_project", self.runtime.read_project(run_id), {"run_id": run_id})
                context[node["id"]] = output
            step.update(status="succeeded", output=output, finished_at=now())
            self.store.complete_step(run_id, step, context)
            return output
        except asyncio.CancelledError:
            current = next((s for s in self.store.steps(run_id) if s["id"] == step_id), {})
            for key in ("agent_id", "agent_name", "agent_version"):
                if key in current:
                    step[key] = current[key]
            step.update(status="blocked", error="节点被服务停止中断", finished_at=now())
            self.store.step(run_id, step)
            self.store.event(run_id, "error", "节点中断，可恢复。", step_id)
            raise
        except Exception as exc:
            current = next((s for s in self.store.steps(run_id) if s["id"] == step_id), {})
            for key in ("agent_id", "agent_name", "agent_version"):
                if key in current:
                    step[key] = current[key]
            step.update(status="blocked" if isinstance(exc, Blocked) else "failed", error=str(exc), finished_at=now())
            self.store.step(run_id, step)
            self.store.update_run(run_id, context=context)
            raise

    def _select(self, run, node, step_id, excluded=()):
        required_tools = {"test": {"run_acceptance"}, "edit": {"candidate_diff", "read_project"}}.get(node["operation"], set())
        candidates = [a for a in run["agent_versions"] if a["enabled"] and node["capability"] in a["capabilities"] and a["id"] not in excluded
                      and required_tools.issubset(a.get("tools", []))]
        if not candidates:
            raise Blocked(f"没有可用 Agent 匹配能力 {node['capability']}；本次运行保存的配置版本保持不变，请调整配置后新建运行。")
        ranked = sorted(candidates, key=lambda a: (self.loads.get(a["id"], 0), len(a["capabilities"]), a["id"]))
        agent = ranked[0]
        scores = [{"id": a["id"], "load": self.loads.get(a["id"], 0), "capability_count": len(a["capabilities"]), "version": a["version"]} for a in ranked]
        self.loads[agent["id"]] = self.loads.get(agent["id"], 0) + 1
        self.store.event(run["id"], "schedule", f"分配给 {agent['name']}：能力 {node['capability']} 匹配；优先选择低负载和专用能力 Agent。", step_id,
                         {"selected": agent["id"], "version": agent["version"], "priority": node["priority"],
                          "dependencies": node["depends_on"], "dependencies_satisfied": True,
                          "candidates": scores, "required_tools": sorted(required_tools), "excluded_after_failure": list(excluded)})
        return agent

    def _release(self, agent):
        self.loads[agent["id"]] = max(0, self.loads.get(agent["id"], 1) - 1)

    async def _dispatch(self, run, node, context, step_id):
        excluded = []
        last_error = None
        for retry in range(2):
            try:
                agent = self._select(run, node, step_id, excluded)
            except Blocked:
                if last_error:
                    raise last_error
                raise
            current_step = next(s for s in self.store.steps(run["id"]) if s["id"] == step_id)
            current_step.update(agent_id=agent["id"], agent_name=agent["name"], agent_version=agent["version"])
            self.store.step(run["id"], current_step)
            try:
                return await self._invoke(run, node, step_id, agent, context), agent
            except Exception as exc:
                last_error = exc
                excluded.append(agent["id"])
                self.store.event(run["id"], "error", f"{agent['name']} 调用失败；尝试其他匹配 Agent。", step_id,
                                 {"error": str(exc), "attempt": retry + 1})
            finally:
                self._release(agent)
        raise last_error

    async def _invoke(self, run, node, step_id, agent, context):
        task = {"mode": run["mode"], "requirement": run["requirement"], "kind": node["id"], "workflow_id": run["workflow_id"]}
        try:
            result = await self.runtime.invoke(agent, task, deepcopy(context))
        except BaseException as exc:
            metrics = {"model_calls": getattr(exc, "model_calls", 0), "tokens": getattr(exc, "tokens", 0),
                       "tool_calls": getattr(exc, "tool_calls", []),
                       "model_diagnostics": getattr(exc, "model_diagnostics", []),
                       "contract_retries": getattr(exc, "contract_retries", 0),
                       "metrics_complete": getattr(exc, "metrics_complete", run["mode"] == "mock")}
            self._record_usage(run, step_id, metrics)
            self.store.event(run["id"], "agent", "Agent 调用中断" if isinstance(exc, asyncio.CancelledError) else "Agent 调用失败", step_id,
                             {"agent_id": agent["id"], "agent_version": agent["version"], "model_mode": run["mode"],
                              "status": "failed", "error": str(exc), **metrics})
            raise
        self._record_usage(run, step_id, result if isinstance(result, dict) else {"metrics_complete": False})
        if not isinstance(result, dict) or not isinstance(result.get("output"), dict):
            raise ValueError("模型返回不符合结构化输出契约")
        self.store.event(run["id"], "agent", result["output"].get("summary", "Agent 返回结构化结果"), step_id,
                         {"agent_id": agent["id"], "agent_version": agent["version"], **result})
        return result

    def _record_usage(self, run, step_id, metrics):
        current = self.store.run(run["id"])
        calls, tokens = metrics.get("model_calls", 0), metrics.get("tokens", 0)
        complete = metrics.get("metrics_complete", True)
        if type(calls) is not int or calls < 0:
            calls, complete = 0, False
        if type(tokens) is not int or tokens < 0:
            tokens, complete = 0, False
        self.store.update_run(run["id"], model_calls=current.get("model_calls", 0) + calls,
                              model_tokens=current.get("model_tokens", 0) + tokens,
                              model_metrics_complete=current.get("model_metrics_complete", True) and complete,
                              agent_invocations=current.get("agent_invocations", 0) + 1)
        for call in metrics.get("tool_calls", []):
            if isinstance(call, dict):
                self.store.event(run["id"], "tool", "模型工具调用记录（由运行器生成）", step_id, {**call, "source": "runtime"})

    async def _tool(self, run_id, step_id, name, awaitable, inputs):
        self.store.event(run_id, "tool", f"调用工具：{name}", step_id, {"tool": name, "status": "running", "input": inputs})
        try:
            result = await awaitable
            outcome = result.get("status") if isinstance(result, dict) else None
            status = outcome if outcome in ("failed", "blocked") else "completed"
            self.store.event(run_id, "tool", f"工具返回：{name}" + (f" · {outcome}" if outcome else ""), step_id,
                             {"tool": name, "status": status, "outcome": outcome, "source": "runtime", "input": inputs, "output": result})
            return result
        except asyncio.CancelledError:
            self.store.event(run_id, "tool", f"工具中断：{name}", step_id,
                             {"tool": name, "status": "blocked", "source": "runtime", "input": inputs, "error": "服务停止中断工具调用"})
            raise
        except Exception as exc:
            self.store.event(run_id, "tool", f"工具失败：{name}", step_id, {"tool": name, "status": "failed", "input": inputs, "error": str(exc)})
            raise
