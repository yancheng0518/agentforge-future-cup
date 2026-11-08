"""Auditable experimental controllers; production API/workflows are unchanged.

All model work uses Runtime's live transport and all candidate execution remains
inside Runtime.test. Request caps are shared per run, not per Agent. The direct
baseline has no planner/tester/reviewer model calls and makes no review claim.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import re
import threading
from uuid import uuid4

from .definitions import AGENTS, WORKFLOWS, node
from .engine import Blocked, Engine, candidate_fingerprint
from .runtime import Runtime, RuntimeErrorSafe
from .storage import now


_invocation = ContextVar("agentforge_comparison_invocation", default=None)
ARMS = ("single_direct", "fixed_serial", "dynamic_parallel")
FIXED_ASSIGNMENTS = {"plan": "planner", "develop": "developer", "test": "tester",
                     "inspect": "reviewer", "review": "reviewer", "fix": "fixer"}
SINGLE_PROMPT = ("直接根据冻结需求和当前候选完成 todo.py，独立思考实现方案。"
                 "如果有真实测试失败反馈，根据反馈修正完整候选。保持兼容，不修改独立验收；"
                 "只返回 JSON files 列表和 summary。测试由可信运行器执行，不能声称进行了独立审查。")


def arm_definition(arm, agents=None):
    """Freeze experimental workflows without creating a run or making calls."""
    arm = {"parallel": "dynamic_parallel", "serial": "fixed_serial"}.get(arm, arm)
    if arm not in ARMS:
        raise ValueError("Unknown comparison arm")
    versions = sorted(deepcopy(AGENTS if agents is None else agents), key=lambda a: a["id"])
    workflow = deepcopy(WORKFLOWS["development"])
    workflow.update(comparison_arm=arm, comparison_revision=1)
    if arm == "fixed_serial":
        workflow["description"] = "固定角色映射；规划、开发、静态初审、测试、汇总审查串行；最多两次修正。"
        verify = next(n for n in workflow["nodes"] if n["operation"] == "subworkflow")
        by_id = {n["id"]: n for n in verify["children"]}
        by_id["inspect"]["depends_on"] = []
        by_id["test"]["depends_on"] = ["inspect"]
        by_id["review"]["depends_on"] = ["test", "inspect"]
        verify["children"] = [by_id[k] for k in ("inspect", "test", "review")]
    elif arm == "single_direct":
        developer = next(a for a in versions if a["id"] == "developer")
        versions = [{**developer, "id": "single_direct", "name": "单 Agent 直接开发",
                     "role": "developer", "prompt": SINGLE_PROMPT, "capabilities": ["develop", "fix"],
                     "tools": ["read_project", "knowledge_search", "candidate_diff"]}]
        workflow = {"id": "development", "name": "单 Agent 直接开发对照", "version": 1,
                    "comparison_arm": arm, "comparison_revision": 1, "max_repairs": 2,
                    "description": "一个开发 Agent 生成与修正；可信 Docker 验收，无模型规划、测试解读或独立审查。",
                    "nodes": [node("prepare", "读取项目与知识", None, "prepare"),
                              node("develop", "直接开发", "develop", "edit", ("prepare",)),
                              node("test", "可信独立验收", None, "test"),
                              node("gate", "独立测试门控", None, "condition"),
                              node("fix", "同一 Agent 反馈修正", "fix", "edit", kind="loop", conditional=True)]}
    return {"workflow": workflow, "agent_versions": versions, "description": workflow["description"]}


class BudgetExceeded(RuntimeErrorSafe):
    pass


class AuditPersistenceError(RuntimeErrorSafe):
    pass


class BudgetRuntime(Runtime):
    """Per-run, thread-safe transport budget with invocation-level accounting."""

    def __init__(self, root, data_dir, *, max_requests=24, max_payload_bytes=180000,
                 max_tokens=4096, image_id=None, limits=None):
        super().__init__(root, data_dir)
        if limits:
            max_requests = limits.get("max_requests", max_requests)
            max_payload_bytes = limits.get("max_payload_bytes", max_payload_bytes)
            max_tokens = limits.get("max_output_tokens", max_tokens)
        if type(max_requests) is not int or not 1 <= max_requests <= 100:
            raise ValueError("max_requests must be an integer from 1 through 100")
        if type(max_payload_bytes) is not int or max_payload_bytes < 100:
            raise ValueError("max_payload_bytes must be an integer of at least 100")
        if max_tokens != 4096:
            raise ValueError("The shared Runtime adapter requires max_tokens=4096")
        if image_id is not None:
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                raise ValueError("Comparison image must be an immutable local image ID")
            self.settings["image"] = image_id
        self.limits = {"max_requests": max_requests, "max_payload_bytes": max_payload_bytes,
                       "max_output_tokens": max_tokens}
        self._frozen_settings = {k: self.settings.get(k) for k in ("model", "base_url", "timeout", "image", "min_request_interval")}
        self._budget_lock = threading.RLock()
        self._budgets = {}
        self._audit_failed = False
        self._unpersisted_artifacts = {}

    def model_config(self):
        return {**super().model_config(), "comparison_budget": deepcopy(self.limits),
                "comparison_adapter_revision": 1, "sandbox_image": self.settings["image"]}

    def register_run(self, run_id):
        self._run_dir(run_id)  # Validate identifiers without executing a candidate.
        with self._budget_lock:
            self._budgets.setdefault(run_id, {"run_id": run_id, "limits": deepcopy(self.limits),
                                             "requests": [], "denials": [], "invocations": []})

    @staticmethod
    def _usage(response):
        usage = response.get("usage") if isinstance(response, dict) else None
        return {k: v for k, v in (usage if isinstance(usage, dict) else {}).items()
                if k in ("prompt_tokens", "completion_tokens", "total_tokens") and type(v) is int and v >= 0}

    def _invocation_metrics(self, invocation):
        with self._budget_lock:
            entries = [r for r in self._budgets[invocation["run_id"]]["requests"]
                       if r["invocation_id"] == invocation["invocation_id"]]
            return {"model_calls": len(entries),
                    "tokens": sum(r.get("usage", {}).get("total_tokens", 0) for r in entries),
                    "metrics_complete": all(r["status"] == "completed" and r["metrics_complete"] for r in entries),
                    "invocation_id": invocation["invocation_id"],
                    "request_ids": [r["request_id"] for r in entries]}

    def _redacted(self, value):
        secret = self.settings.get("api_key", "")
        if isinstance(value, str):
            return value.replace(secret, "[REDACTED]") if secret else value
        if isinstance(value, dict):
            return {key: self._redacted(item) for key, item in value.items()
                    if key.lower() not in {"authorization", "api_key", "api-key", "reasoning", "reasoning_content", "headers"}}
        if isinstance(value, list):
            return [self._redacted(item) for item in value]
        return value

    def _write_request(self, record, payload, response=None):
        folder = self.data_dir / "requests" / record["run_id"]
        for path in (self.data_dir / "requests", folder):
            if path.is_symlink():
                raise RuntimeErrorSafe("Request archive symlinks are forbidden")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (record["request_id"] + ".json")
        if path.is_symlink():
            raise RuntimeErrorSafe("Request archive symlinks are forbidden")
        safe_response = None
        if isinstance(response, dict):
            safe_response = {k: response[k] for k in ("id", "model", "created", "usage") if k in response}
            safe_response["choices"] = []
            for choice in response.get("choices", []) if isinstance(response.get("choices"), list) else []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                safe_response["choices"].append({"finish_reason": choice.get("finish_reason"),
                                                 "message": {k: message[k] for k in ("role", "content", "tool_calls") if k in message} if isinstance(message, dict) else None})
        content = self._redacted({"schema": "agentforge-comparison-request-v1", "request": {k: v for k, v in record.items() if k not in {"artifact_sha256", "artifact_path"}},
                                  "payload": payload, "response": safe_response,
                                  "reasoning_retained": False, "authorization_retained": False})
        raw = json.dumps(content, ensure_ascii=False, indent=2).encode()
        temporary = path.with_suffix(".pending")
        try:
            temporary.write_bytes(raw)
            temporary.replace(path)
        except OSError:
            self._unpersisted_artifacts[record["request_id"]] = content
            raise
        self._unpersisted_artifacts.pop(record["request_id"], None)
        record.update(artifact_path=path.relative_to(self.data_dir).as_posix(), artifact_sha256=hashlib.sha256(raw).hexdigest())

    def budget_record(self, run_id):
        self.register_run(run_id)
        with self._budget_lock:
            value = deepcopy(self._budgets[run_id])
            emergency = {key: deepcopy(value) for key, value in self._unpersisted_artifacts.items()
                         if value.get("request", {}).get("run_id") == run_id}
        entries = value["requests"]
        return {**value, "max_requests": self.limits["max_requests"],
                "sent_requests": len(entries), "denied_requests": len(value["denials"]),
                "model_tokens_known": sum(r.get("usage", {}).get("total_tokens", 0) for r in entries),
                "metrics_complete": all(r["status"] == "completed" and r["metrics_complete"] for r in entries),
                "audit_complete": not emergency and not any(r.get("audit_error") for r in entries)
                                  and not any(d["reason"] == "request_archive_failed" for d in value["denials"]),
                "unpersisted_artifacts": emergency,
                "transport_semantics": "An entry reserves a slot immediately before entering the live transport; failures and limiter waits count. Denials never enter the transport."}

    ledger = budget_record

    async def invoke(self, agent, task, context):
        if task.get("mode") != "live":
            raise RuntimeErrorSafe("Comparison runs require live mode; no mock fallback")
        run_id = context.get("run_id")
        self.register_run(run_id)
        invocation = {"run_id": run_id, "invocation_id": uuid4().hex,
                      "node_id": context.get("_comparison_node_id", task.get("kind")),
                      "node_kind": task.get("kind"), "agent_id": agent.get("id"),
                      "started_at": now(), "status": "pending"}
        with self._budget_lock:
            self._budgets[run_id]["invocations"].append(invocation)
        token = _invocation.set(invocation)
        try:
            result = await super().invoke(agent, task, context)
            metrics = self._invocation_metrics(invocation)
            result.update(metrics)
            invocation.update(status="completed", finished_at=now(), **metrics)
            return result
        except BaseException as exc:
            metrics = self._invocation_metrics(invocation)
            for key, value in metrics.items():
                setattr(exc, key, value)
            invocation.update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                              finished_at=now(), **metrics)
            raise
        finally:
            _invocation.reset(token)

    def _post(self, payload):
        invocation = _invocation.get()
        if not invocation:
            raise RuntimeErrorSafe("Comparison transport requires a registered invocation")
        # Runtime appends retries to its messages list after a response. Keep
        # this request's exact transport/archive bytes independent of that list.
        payload = deepcopy(payload)
        body = json.dumps(payload).encode()
        messages = json.dumps(payload.get("messages"), ensure_ascii=False).encode()
        with self._budget_lock:
            budget = self._budgets[invocation["run_id"]]
            reason = None
            if self._audit_failed:
                reason = "prior_request_archive_failure"
            elif any(self.settings.get(k) != v for k, v in self._frozen_settings.items()):
                reason = "frozen_configuration_changed"
            elif payload.get("model") != self._frozen_settings["model"]:
                reason = "model_override_forbidden"
            elif payload.get("max_tokens") != self.limits["max_output_tokens"] or payload.get("temperature") != 0.1:
                reason = "request_parameters_changed"
            elif len(body) > self.limits["max_payload_bytes"]:
                reason = "payload_byte_limit"
            elif len(budget["requests"]) >= self.limits["max_requests"]:
                reason = "request_limit"
            if reason:
                budget["denials"].append({"timestamp": now(), "invocation_id": invocation["invocation_id"],
                                          "node_id": invocation["node_id"], "node_kind": invocation["node_kind"],
                                          "reason": reason, "sent": False, "payload_bytes": len(body)})
                raise BudgetExceeded("Comparison request denied before HTTP: " + reason)
            record = {"run_id": invocation["run_id"], "sequence": len(budget["requests"]) + 1, "request_id": uuid4().hex,
                      "invocation_id": invocation["invocation_id"], "node_id": invocation["node_id"],
                      "node_kind": invocation["node_kind"], "agent_id": invocation["agent_id"],
                      "model": payload["model"], "sent": True, "status": "pending",
                      "payload_sha256": hashlib.sha256(body).hexdigest(), "payload_bytes": len(body),
                      "messages_bytes": len(messages), "max_tokens": payload["max_tokens"],
                      "temperature": payload["temperature"], "tool_choice": payload.get("tool_choice"),
                      "started_at": now(), "usage": {}, "metrics_complete": False, "finish_reason": None}
            try:
                self._write_request(record, payload)
            except (OSError, RuntimeErrorSafe):
                self._audit_failed = True
                if record["request_id"] in self._unpersisted_artifacts:
                    self._unpersisted_artifacts[record["request_id"]]["request"]["sent"] = False
                budget["denials"].append({"timestamp": now(), "invocation_id": invocation["invocation_id"],
                                          "node_id": invocation["node_id"], "node_kind": invocation["node_kind"],
                                          "reason": "request_archive_failed", "sent": False})
                raise RuntimeErrorSafe("Cannot preserve pending request; no HTTP sent") from None
            budget["requests"].append(record)
        try:
            response = super()._post(payload)
        except BaseException as exc:
            with self._budget_lock:
                record.update(status="failed", finished_at=now(), error_type=type(exc).__name__)
                try:
                    self._write_request(record, payload)
                except (OSError, RuntimeErrorSafe):
                    self._audit_failed = True
                    record.update(audit_error="request_archive_write_failed", artifact_status="failed")
                    raise AuditPersistenceError("Cannot preserve failed request; further model requests are disabled") from None
            raise
        usage = self._usage(response)
        try:
            finish = response["choices"][0].get("finish_reason")
        except (KeyError, TypeError, IndexError):
            finish = None
        with self._budget_lock:
            record.update(status="completed", finished_at=now(), usage=usage,
                          metrics_complete="total_tokens" in usage,
                          finish_reason=finish if isinstance(finish, str) and finish in {"stop", "length", "tool_calls", "content_filter", "function_call"} else None)
            try:
                self._write_request(record, payload, response)
            except (OSError, RuntimeErrorSafe):
                self._audit_failed = True
                record.update(audit_error="request_archive_write_failed", artifact_status="failed")
                raise AuditPersistenceError("HTTP response received but its archive failed; known usage retained, further model requests disabled") from None
        return response


class _AuditedEngine(Engine):
    async def _invoke(self, run, node, step_id, agent, context):
        view = deepcopy(context)
        if node["id"] == "inspect":
            for field in ("test", "review"):
                view.pop(field, None)
        elif node["id"] == "test":
            for field in ("inspect", "review"):
                view.pop(field, None)
        view["_comparison_node_id"] = step_id
        task = {"mode": run["mode"], "requirement": run["requirement"], "kind": node["id"], "workflow_id": run["workflow_id"]}
        try:
            result = await self.runtime.invoke(agent, task, view)
        except BaseException as exc:
            metrics = {"model_calls": getattr(exc, "model_calls", 0), "tokens": getattr(exc, "tokens", 0),
                       "tool_calls": getattr(exc, "tool_calls", []), "model_diagnostics": getattr(exc, "model_diagnostics", []),
                       "contract_retries": getattr(exc, "contract_retries", 0),
                       "metrics_complete": getattr(exc, "metrics_complete", False),
                       "invocation_id": getattr(exc, "invocation_id", None), "request_ids": getattr(exc, "request_ids", [])}
            self._record_usage(run, step_id, metrics)
            self.store.event(run["id"], "agent", "Agent 调用中断" if isinstance(exc, asyncio.CancelledError) else "Agent 调用失败", step_id,
                             {"agent_id": agent["id"], "agent_version": agent["version"], "model_mode": "live",
                              "status": "failed", "error": str(exc), **metrics})
            raise
        self._record_usage(run, step_id, result if isinstance(result, dict) else {"metrics_complete": False})
        if not isinstance(result, dict) or not isinstance(result.get("output"), dict):
            raise ValueError("模型返回不符合结构化输出契约")
        self.store.event(run["id"], "agent", result["output"].get("summary", "Agent 返回结构化结果"), step_id,
                         {"agent_id": agent["id"], "agent_version": agent["version"], **result})
        return result

    def configure_run(self, requirement, task_id="completion", task_snapshot=None):
        if task_snapshot is not None and self.runtime.snapshot_task(task_id) != task_snapshot:
            raise ValueError("Task differs from the preregistered snapshot")
        return self.create_run(requirement, "development", "live", task_id)


class ComparisonEngine(_AuditedEngine):
    def __init__(self, store, runtime, arm="dynamic_parallel"):
        super().__init__(store, runtime)
        self.arm = {"parallel": "dynamic_parallel", "serial": "fixed_serial"}.get(arm, arm)
        if self.arm not in {"dynamic_parallel", "fixed_serial"}:
            raise ValueError("ComparisonEngine requires a multi-Agent arm")

    def create_run(self, requirement, workflow_id="development", mode="live", task_id="completion"):
        if workflow_id != "development" or mode != "live":
            raise ValueError("Comparison accepts only live development runs")
        run = super().create_run(requirement, workflow_id, mode, task_id)
        definition = arm_definition(self.arm, run["agent_versions"])
        return self.store.update_run(run["id"], comparison_arm=self.arm, workflow=definition["workflow"], agent_versions=definition["agent_versions"],
                                     independent_review=True, fault_injection=False,
                                     comparison_assignments=deepcopy(FIXED_ASSIGNMENTS) if self.arm == "fixed_serial" else "dynamic")

    def _select(self, run, node, step_id, excluded=()):
        if self.arm == "dynamic_parallel":
            return super()._select(run, node, step_id, excluded)
        expected = FIXED_ASSIGNMENTS.get(node["id"])
        required = {"test": {"run_acceptance"}, "edit": {"candidate_diff", "read_project"}}.get(node["operation"], set())
        agent = next((a for a in run["agent_versions"] if a["id"] == expected), None)
        if not agent or not agent["enabled"] or node["capability"] not in agent["capabilities"] or not required.issubset(agent.get("tools", [])) or expected in excluded:
            raise Blocked("固定串行映射不可用；不重新分派其他 Agent。")
        self.loads[expected] = self.loads.get(expected, 0) + 1
        self.store.event(run["id"], "schedule", "按预注册固定映射分配；无动态替补。", step_id,
                         {"selected": expected, "version": agent["version"], "policy": "fixed_serial",
                          "priority": node["priority"], "dependencies": node["depends_on"],
                          "dependencies_satisfied": True, "required_tools": sorted(required)})
        return agent

    async def _dispatch(self, run, node, context, step_id):
        if self.arm == "dynamic_parallel":
            return await super()._dispatch(run, node, context, step_id)
        agent = self._select(run, node, step_id)
        step = next(s for s in self.store.steps(run["id"]) if s["id"] == step_id)
        step.update(agent_id=agent["id"], agent_name=agent["name"], agent_version=agent["version"])
        self.store.step(run["id"], step)
        try:
            return await self._invoke(run, node, step_id, agent, context), agent
        finally:
            self._release(agent)

    async def _node(self, run, node, context, step_id):
        if node["operation"] != "subworkflow":
            return await super()._node(run, node, context, step_id)
        previous = next((s for s in self.store.steps(run["id"]) if s["id"] == step_id), None)
        if previous and previous["status"] == "succeeded":
            self._restore_output(node, previous.get("output"), context)
            self.store.event(run["id"], "snapshot", "复用已完成验证子工作流。", step_id)
            return previous.get("output")
        step = {"id": step_id, "node_id": node["id"], "label": node["label"], "status": "running",
                "attempt": context["attempt"], "started_at": now(), "output": None}
        self.store.step(run["id"], step)
        serial = self.arm == "fixed_serial"
        self.store.event(run["id"], "status", "验证固定串行：初审 → 测试 → 汇总审查。" if serial else "验证并行：测试与初审并行，随后汇总审查。", step_id,
                         {"children": [n["id"] for n in node["children"]], "parallel": [] if serial else ["test", "inspect"], "comparison_arm": self.arm})
        try:
            await self._dag(run, node["children"], context, step_id + "/")
            output = {k: deepcopy(context.get(k)) for k in ("test", "inspect", "review", "validation_scope")}
            step.update(status="succeeded", output=output, finished_at=now())
            self.store.complete_step(run["id"], step, context)
            return output
        except BaseException as exc:
            step.update(status="blocked" if isinstance(exc, (Blocked, asyncio.CancelledError)) else "failed",
                        error="执行中断" if isinstance(exc, asyncio.CancelledError) else str(exc), finished_at=now())
            self.store.step(run["id"], step)
            raise


class SingleDirectEngine(_AuditedEngine):
    def create_run(self, requirement, workflow_id="development", mode="live", task_id="completion"):
        if workflow_id != "development" or mode != "live":
            raise ValueError("Direct comparison accepts only live development runs")
        run = super().create_run(requirement, workflow_id, mode, task_id)
        definition = arm_definition("single_direct", run["agent_versions"])
        return self.store.update_run(run["id"], comparison_arm="single_direct", agent_versions=definition["agent_versions"],
                                     workflow=definition["workflow"], independent_review=False, fault_injection=False,
                                     comparison_assignments={"develop": "single_direct", "fix": "single_direct"})

    async def _dispatch(self, run, node, context, step_id):
        agent = self._select(run, node, step_id)
        step = next(s for s in self.store.steps(run["id"]) if s["id"] == step_id)
        step.update(agent_id=agent["id"], agent_name=agent["name"], agent_version=agent["version"])
        self.store.step(run["id"], step)
        try:
            return await self._invoke(run, node, step_id, agent, context), agent
        finally:
            self._release(agent)

    @staticmethod
    def _complete_test(test):
        passed = test.get("status") == "passed"
        marker = "AGENTFORGE_ACCEPTANCE=" + json.dumps({"tests": 10, "passed": passed})
        return (test.get("isolated") is True and test.get("status") in {"passed", "failed"}
                and type(test.get("exit_code")) is int and test["exit_code"] == (0 if passed else 1)
                and marker in test.get("stdout", "") and re.search(r"Ran 10 tests? in ", test.get("stderr", "")) is not None)

    async def execute(self, run_id):
        if run_id in self.running:
            return
        run = self.store.run(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] == "succeeded":
            return
        self.running.add(run_id)
        context = deepcopy(run.get("context", {}))
        context.update(run_id=run_id, requirement=run["requirement"], attempt=run.get("attempt", 0), task_contract=deepcopy(run.get("task")))
        self.store.update_run(run_id, status="running", summary="单 Agent 直接开发：无独立模型审查。")
        self.store.event(run_id, "status", "单 Agent 直接开发开始；只用独立 Docker 测试作为原生门控。", data={"independent_review": False})
        try:
            if run.get("model_config") != self.runtime.model_config():
                raise Blocked("模型或预算条件与预注册不一致。")
            nodes = {n["id"]: n for n in run["workflow"]["nodes"]}
            await self._node(run, nodes["prepare"], context, "prepare")
            await self._node(run, nodes["develop"], context, "develop")
            for attempt in range(context["attempt"], 3):
                context["attempt"] = attempt
                self.store.update_run(run_id, attempt=attempt)
                if attempt:
                    await self._node(run, nodes["fix"], context, f"round-{attempt}/fix")
                diff = await self.runtime.diff(run_id)
                fingerprint = candidate_fingerprint(diff)
                verify_id = f"round-{attempt}/verify"
                self._begin_validation(context, verify_id, fingerprint)
                context.update(diff=diff, project=await self.runtime.read_project(run_id))
                step_id = verify_id + "/test"
                step = {"id": step_id, "node_id": "test", "label": "可信独立验收（无测试模型）", "status": "running",
                        "attempt": attempt, "started_at": now(), "output": None}
                self.store.step(run_id, step)
                try:
                    test = await self._tool(run_id, step_id, "run_acceptance", self.runtime.test(run_id), {"run_id": run_id, "independent_tests": True})
                    test = {**test, "evidence_scope": deepcopy(context["validation_scope"])}
                    context["test"] = test
                    if test.get("status") == "blocked" or not self._complete_test(test):
                        raise Blocked("独立验收未完整运行；不将其算作自然代码失败。")
                    step.update(status="succeeded", output={"test": test}, finished_at=now())
                    self.store.complete_step(run_id, step, context)
                except BaseException as exc:
                    step.update(status="blocked" if isinstance(exc, (Blocked, asyncio.CancelledError)) else "failed", finished_at=now(),
                                error="执行中断" if isinstance(exc, asyncio.CancelledError) else str(exc))
                    self.store.step(run_id, step)
                    raise
                passed = test["status"] == "passed"
                gate_id = f"round-{attempt}/gate"
                self.store.step(run_id, {"id": gate_id, "node_id": "gate", "label": "独立测试门控", "status": "succeeded",
                                         "attempt": attempt, "output": {"passed": passed, "test_status": test["status"], "independent_review": False}})
                self.store.event(run_id, "validation", "独立测试通过；未执行独立模型审查。" if passed else "独立测试失败。", gate_id,
                                 {"passed": passed, "test": test, "attempt": attempt, "independent_review": False})
                if passed:
                    if candidate_fingerprint(await self.runtime.diff(run_id)) != fingerprint:
                        raise Blocked("测试期间候选变化；旧验证不能用于当前候选。")
                    self.store.update_run(run_id, status="succeeded", summary="单 Agent 直接开发：独立 Docker 验收通过，无独立模型审查；候选未应用。",
                                          verified_hash=fingerprint, context=context)
                    break
                if attempt == 2:
                    self.store.update_run(run_id, status="needs_human", summary="两轮修正后独立测试仍失败；候选未应用。", context=context)
                    break
                context["feedback"] = {"test": deepcopy(test), "issues": ["独立验收失败，请依据 stdout/stderr 修正候选。"], "next_attempt": attempt + 1}
                self.store.event(run_id, "feedback", "真实测试失败反馈交回同一个开发 Agent。", gate_id, deepcopy(context["feedback"]))
                self.store.update_run(run_id, context=context)
            self.store.event(run_id, "status", self.store.run(run_id)["summary"])
        except Blocked as exc:
            self.store.update_run(run_id, status="blocked", summary=str(exc), context=context)
            self.store.event(run_id, "error", str(exc), data={"recoverable": False})
        except asyncio.CancelledError:
            self.store.update_run(run_id, status="blocked", summary="对照试次被中断，保留失败与已知用量，不自动重跑。", context=context)
            self.store.event(run_id, "error", "对照试次中断。", data={"recoverable": False})
            raise
        except Exception as exc:
            self.store.update_run(run_id, status="failed", summary=f"执行失败：{type(exc).__name__}: {exc}", context=context)
            self.store.event(run_id, "error", str(exc), data={"recoverable": False})
        finally:
            self.running.discard(run_id)
