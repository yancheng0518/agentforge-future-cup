"""Version 1 model-directed orchestration, independent of the legacy task catalogue.

The controller decides the graph and its revisions. This executor only enforces
dependencies, file ownership, budgets and an independent evidence-based finish gate.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import re
import time
from uuid import uuid4

from .autonomous_model import (AutonomousModel, ModelBudgetExceeded, validate_intake, validate_web_intake, validate_review,
                               canonical_sha256, freeze_configuration, merge_acceptance, validate_project_tool)
from .autonomous_sandbox import AutonomousSandbox, SandboxIntegrityError, _sources, _path
from .runtime import RuntimeErrorSafe
from .storage import now
from .web_config import validate_web_config, web_runtime_sha256


DEFAULT_BUDGET = dict(max_calls=40, max_tokens=200000, max_output_tokens=4096,
                      max_steps=24, max_tasks=32, max_parallel=3,
                      max_verifications=8, wall_seconds=900)
LIMITS = dict(max_calls=(1, 100), max_tokens=(4096, 1000000),
              max_output_tokens=(512, 8192), max_steps=(1, 60),
              max_tasks=(1, 64), max_parallel=(1, 4),
              max_verifications=(1, 20), wall_seconds=(30, 3600))


def budget_config(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_BUDGET):
        raise ValueError("Unknown budget setting")
    result = {**DEFAULT_BUDGET, **value}
    for key, (low, high) in LIMITS.items():
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    return result


class DecisionRejected(ValueError):
    """Recoverable controller decision, recorded separately from format retries."""


def safe_path(path):
    try:
        return _path(path)
    except RuntimeErrorSafe as exc:
        raise DecisionRejected(str(exc)) from None


class AutonomousEngine:
    revision = "model-directed-v1"

    def __init__(self, store, runtime, model=None, sandbox=None):
        self.store, self.runtime = store, runtime
        self.model = model or AutonomousModel(runtime, store)
        self.sandbox = sandbox or AutonomousSandbox(runtime)
        self.tasks = {}
        self.running = set()
        self.completion_callback = None
        self.project_tool_callback = None
        self.project_cleanup_callback = None

    def create_run(self, bundle, budget=None):
        frozen = self.sandbox.validate_bundle(bundle)
        limits = budget_config(budget)
        run = dict(id=uuid4().hex, engine=self.revision, mode="live", status="queued",
                   requirement=frozen["requirement"], bundle=deepcopy(frozen), budget=limits,
                   created_at=now(), updated_at=now(), applied=False,
                   summary="等待主 Agent 分解任务", model_config=(self.model.config(limits)
                   if hasattr(self.model, "config") else self.runtime.model_config()),
                   controller_decisions=[], task_graph=[], verifications=[],
                   feedback=None, candidate_origin="baseline", verified_hash=None,
                   model_metrics=dict(model_calls=0, tokens=0, metrics_complete=True,
                                      contract_retries=0, in_flight=0, reserved_tokens=0))
        run["bundle_sha256"] = hashlib.sha256(json.dumps(frozen, ensure_ascii=False,
                                                         sort_keys=True).encode()).hexdigest()
        self.store.create_run(run)
        self.store.event(run["id"], "created", "建立独立的动态编排运行", data={
            "engine": self.revision, "bundle_sha256": run["bundle_sha256"],
            "provenance": frozen["provenance"], "budget": limits})
        if frozen['provenance'].get('web'):
            self._snapshot_implementation(run['id'])
        return run

    def create_run_from_requirement(self, requirement, budget=None):
        """Persist the user's request before any paid preparation call is sent."""
        if not isinstance(requirement, str) or not 1 <= len(requirement.strip()) <= 8000:
            raise ValueError("需求必须为 1 至 8000 字符")
        limits = budget_config(budget)
        run = dict(id=uuid4().hex, engine=self.revision, mode="live", status="queued",
                   requirement=requirement.strip(), bundle=None, bundle_sha256=None, budget=limits,
                   created_at=now(), updated_at=now(), applied=False,
                   summary="准备理解需求并生成可检查的验收标准",
                   model_config=(self.model.config(limits) if hasattr(self.model, "config") else self.runtime.model_config()),
                   controller_decisions=[], task_graph=[], verifications=[], feedback=None,
                   candidate_origin="baseline", verified_hash=None,
                   intake={"status": "pending", "acceptance_origin": "model_generated"},
                   intake_proposals=[], contract_reviews=[], contract_review=None,
                   quality_reviews=[], quality_review=None,
                   model_metrics=dict(model_calls=0, tokens=0, metrics_complete=True,
                                      contract_retries=0, in_flight=0, reserved_tokens=0))
        self.store.create_run(run)
        self.store.event(run["id"], "created", "已保存需求，等待真实模型整理接口与验收",
                         data={"engine": self.revision, "input_origin": "requirement",
                               "acceptance_origin": "model_generated", "budget": limits})
        try:
            self._snapshot_implementation(run["id"])
        except (OSError, RuntimeErrorSafe) as exc:
            self._save(run["id"], status="failed", summary="无法保存本次运行的实现快照：" + type(exc).__name__)
            self.store.event(run["id"], "error", "实现快照保存失败，未发送模型请求")
        return self.store.run(run["id"])

    def create_run_from_project(self, requirement, revision, budget=None, profiles=None, knowledge=None):
        """Freeze a selected project revision and versioned reference configuration."""
        if not isinstance(revision, dict) or not {"id", "project_id", "number", "files", "source_sha256"} <= set(revision):
            raise ValueError("Project revision needs id,project_id,number,files,source_sha256")
        frozen = json.loads(json.dumps(revision, ensure_ascii=False))
        for field in ("id", "project_id"):
            if not isinstance(frozen[field], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", frozen[field]):
                raise ValueError("Invalid project revision identity")
        if type(frozen["number"]) is not int or frozen["number"] < 0:
            raise ValueError("Invalid project revision number")
        files = _sources(frozen["files"], allow_empty=True)
        if canonical_sha256(files) != frozen["source_sha256"]:
            raise ValueError("Project source differs from its immutable revision hash")
        acceptance = frozen.get("acceptance")
        if acceptance is None:
            acceptance = ""
        if not isinstance(acceptance, str) or len(acceptance.encode()) > 65536:
            raise ValueError("Project regression acceptance exceeds 64 KiB or is invalid")
        frozen.update(files=files, acceptance=acceptance)
        if frozen.get('web') is not None:
            frozen['web'] = validate_web_config(frozen['web'], files)
        profile_snapshot, knowledge_snapshot = freeze_configuration(profiles, knowledge)
        run = self.create_run_from_requirement(requirement, budget)
        configuration_sha256 = canonical_sha256({"profiles": profile_snapshot, "knowledge": knowledge_snapshot})
        self._save(run["id"], project_context=frozen, project_id=frozen["project_id"], base_revision_id=frozen["id"],
                   profile_snapshot=profile_snapshot, knowledge_snapshot=knowledge_snapshot,
                   configuration_sha256=configuration_sha256, candidate_origin="imported_baseline",
                   summary="已冻结项目版本与配置，准备按现有源码实施需求")
        self.store.event(run["id"], "project_revision_frozen", "已冻结项目基线、Agent配置和知识资料",
                         data={"project_id": frozen["project_id"], "revision_id": frozen["id"], "number": frozen["number"],
                               "source_sha256": frozen["source_sha256"], "configuration_sha256": configuration_sha256,
                               "profiles": [{k: p[k] for k in ("id", "version", "sha256", "enabled")} for p in profile_snapshot],
                               "knowledge": [{k: d[k] for k in ("id", "version", "sha256")} for d in knowledge_snapshot],
                               **({'web_runtime_sha256': web_runtime_sha256(frozen['web']),
                                   'dependency_id': frozen['web']['dependency_id']} if frozen.get('web') else {})})
        return self.store.run(run["id"])

    def _snapshot_implementation(self, run_id):
        root, base = self.runtime.root, self.runtime._run_dir(run_id)
        manifest = {}
        files = [*sorted((root / "backend/agentforge").glob("*.py")),
                 *sorted((root / "sandbox/autonomous").glob("*.py")),
                 *sorted(path for path in (root / "sandbox/web").glob('*') if path.is_file())]
        for source in files:
            relative = source.relative_to(root)
            target = base / "implementation" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            with target.open("xb") as stream:
                stream.write(data)
            manifest[relative.as_posix()] = hashlib.sha256(data).hexdigest()
        with (base / "implementation-manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)

    def start(self, run_id):
        if run_id not in self.tasks or self.tasks[run_id].done():
            self.tasks[run_id] = asyncio.create_task(self.execute(run_id))
        return self.tasks[run_id]

    def _save(self, run_id, **fields):
        return self.store.update_run(run_id, **fields)

    def recover_interrupted(self):
        """A process restart cannot establish remote completion; never replay it."""
        with self.store.connect() as db:
            runs = [json.loads(row[0]) for row in db.execute("SELECT body FROM runs")]
        for run in runs:
            if run.get("engine") != self.revision or run["status"] not in ("queued", "running"):
                continue
            graph = deepcopy(run["task_graph"])
            for task in graph:
                if task["status"] == "running":
                    task.update(status="failed", error="Process interrupted; completion unconfirmed", finished_at=now())
                    self.store.step(run["id"], task)
            metrics = deepcopy(run["model_metrics"])
            previous = deepcopy(metrics)
            if metrics.get("in_flight", 0):
                metrics["budget_tokens"] = metrics.get("budget_tokens", 0) + metrics.get("reserved_tokens", 0)
                metrics["unknown_usage_calls"] = metrics.get("unknown_usage_calls", 0) + metrics["in_flight"]
                metrics.update(in_flight=0, reserved_tokens=0, metrics_complete=False)
            self._save(run["id"], status="blocked", task_graph=graph, model_metrics=metrics,
                       summary="进程中断，未确认远端请求是否完成；证据已保留，不自动重放。需新建运行。")
            self.store.event(run["id"], "interrupted_recovery", "恢复运行记录，未重新发送请求",
                             data={"metrics_before": previous, "metrics_after": metrics})

    async def _context(self, run_id):
        run = self.store.run(run_id)
        return dict(requirement=run["bundle"]["requirement"], project=await self.sandbox.read(run_id),
                    original_requirement=run["requirement"], quality_review=run.get("quality_review"),
                    tasks=run["task_graph"], last_verification=(run["verifications"][-1]
                    if run["verifications"] else None), feedback=run.get("feedback"),
                    decisions=run["controller_decisions"][-6:], budget=run["budget"],
                    usage=run["model_metrics"], provenance=run["bundle"]["provenance"],
                    project_revision=({k: run["project_context"][k] for k in ("id", "project_id", "number", "source_sha256")}
                                      if run.get("project_context") else None),
                    project_actions={"edit": run.get("project_context") is not None,
                                     "tools": ["read_file", "search_project", "http_request" if (run.get('project_context') or {}).get('web') else "run_application"]
                                     if run.get("project_context") is not None and callable(self.project_tool_callback) else []},
                    candidate_hash=await self.sandbox.fingerprint(run_id))

    async def _prepare_requirement(self, run_id):
        run = self.store.run(run_id)
        revision = run.get("project_context")
        web = (revision or {}).get('web')
        self._save(run_id, summary="主 Agent 正在整理接口和 AI 生成验收标准")
        self.store.event(run_id, "intake_started", "真实模型正在理解需求；尚未创建执行任务")
        review_feedback = None
        for proposal_index in range(1, 3):
            current = self.store.run(run_id)
            proposed = (await self.model.prepare(current) if review_feedback is None
                        else await self.model.prepare(current, feedback=review_feedback))
            result = (validate_web_intake if web else validate_intake)(proposed, baseline_files=revision["files"] if revision is not None else None)
            proposals = current.get("intake_proposals", []) + [{"index": proposal_index, "timestamp": now(), **deepcopy(result)}]
            self._save(run_id, intake_proposals=proposals)
            if result["status"] == "needs_input":
                self._save(run_id, intake={**result, "acceptance_origin": "model_generated"},
                           status="needs_human", summary=result["summary"])
                self.store.event(run_id, "intake_needs_input", result["summary"], data=result)
                return False
            self._save(run_id, summary="正在独立审查生成验收与原需求是否一致")
            self.store.event(run_id, "contract_review_started", "检查具体预期值、排序边界和函数组合一致性",
                             data={"proposal_index": proposal_index})
            review = validate_review(await self.model.review_contract(self.store.run(run_id), result))
            record = {**review, "index": proposal_index, "timestamp": now(), "origin": "model_contract_review",
                      "proposal_sha256": hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
            reviews = self.store.run(run_id).get("contract_reviews", []) + [record]
            self._save(run_id, contract_review=record, contract_reviews=reviews)
            self.store.event(run_id, "contract_review", "AI 验收契约一致性审查已记录", data=record)
            if review["passed"]:
                break
            review_feedback = {"original_requirement": run["requirement"], "review": record,
                               "previous_proposal": deepcopy(result),
                               "instruction": "修正生成的接口和验收，保留原需求语义；不要修改原需求以迎合错误测试。"}
            if proposal_index == 2:
                self._save(run_id, status="needs_human", summary="两次契约审查仍有冲突，未冻结测试或启动开发。" + review["summary"],
                           intake={"status": "needs_input", "acceptance_origin": "model_generated",
                                   "summary": review["summary"], "questions": review["issues"][:3]},
                           feedback={"kind": "contract_review", "review": record})
                return False
            self._save(run_id, summary="契约审查发现冲突，正在按原需求修订验收")
            self.store.event(run_id, "intake_revision", "保留首版契约与审查意见，重新生成一次", data=review_feedback)
        requirement = run["requirement"] + "\n\n主 Agent 整理的接口与验收约定：\n" + result["specification"]
        if result["assumptions"]:
            requirement += "\n\n补充假设：\n" + "\n".join(result["assumptions"])
        acceptance, suite_manifest = (merge_acceptance(revision.get("acceptance", ""), result["acceptance"])
                                      if revision is not None else (result["acceptance"], None))
        files = {**revision["files"], **result["files"]} if revision is not None else result["files"]
        provenance = {
                        "kind": "natural", "input_origin": "project_revision" if revision else "requirement", "acceptance_origin": "model_generated",
                        "description": "冻结项目源码与回归验收；新增AI函数验收不证明CLI或持久化交付" if revision else
                                       "真实模型根据用户需求生成的未实现脚手架与冻结验收；测试覆盖未经人工独立审定"}
        if web:
            provenance.update(web=deepcopy(web), description='冻结 Web 源码、依赖环境和真实 HTTP 验收；浏览器交互另行核验')
        frozen = self.sandbox.validate_bundle({'requirement': requirement, 'files': files, 'acceptance': acceptance, 'provenance': provenance})
        bundle_hash = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        intake = {"status": "completed", "summary": result["summary"], "specification": result["specification"],
                  "assumptions": result["assumptions"], "acceptance_origin": "model_generated",
                  "coverage_notice": "AI 生成的验收已冻结；通过这些测试不等于所有需求均经人工验证。"}
        if revision is not None:
            intake.update(coverage_notice="新增 AI 验收通过 JSON 函数接口执行，保留旧验收作为回归；CLI与/data持久化需单独受控交付验证。",
                          regression_manifest=suite_manifest, imported_source_sha256=revision["source_sha256"])
        if web:
            intake.update(coverage_notice='新增 AI HTTP 验收与旧回归在冻结依赖 Docker 内执行；浏览器交互和安全范围需另行验证。',
                          verification_kind='http', web_runtime_sha256=web_runtime_sha256(web))
        self._save(run_id, bundle=frozen, bundle_sha256=bundle_hash, intake=intake,
                   acceptance_manifest=suite_manifest,
                   summary="接口与验收已冻结，正在用 Docker 检查导入基线" if revision else "接口与验收已冻结，正在用 Docker 检查未实现基线")
        self.store.event(run_id, "intake_completed", "接口与 AI 生成验收已冻结", data={**intake, "bundle_sha256": bundle_hash})
        await self.sandbox.prepare(run_id, frozen)
        # A generated test suite must distinguish a scaffold from working code.
        # This is input validation, not a model-produced implementation failure.
        await self._verify(run_id)
        baseline = self.store.run(run_id)["verifications"][-1]
        valid_baseline = (baseline["status"] in ("passed", "failed") if revision is not None else baseline["status"] == "failed")
        if not valid_baseline or baseline.get("test_count", 0) < 3 or baseline.get("discovery_mismatch"):
            self._save(run_id, status="needs_human", verified_hash=None,
                       summary="生成的验收未能完成至少三项有效基线检查，已停止开发。请补充具体输入输出和边界条件。",
                       intake={**intake, "status": "needs_input", "questions": ["请补充具体输入输出示例和必须满足的边界条件。"]})
            self.store.event(run_id, "intake_rejected", "生成验收未能可靠区分未实现脚手架，不能据此宣称功能完成", data=baseline)
            return False
        self._save(run_id, summary="基线检查已记录，主 Agent 正在动态拆分任务")
        return True

    async def _review_quality(self, run_id, fingerprint):
        """AI-generated tests cannot be the sole completion authority for chat runs."""
        run = self.store.run(run_id)
        reviews = run.get("quality_reviews", [])
        existing = next((review for review in reversed(reviews) if review["candidate_hash"] == fingerprint), None)
        if existing is not None:
            self._save(run_id, quality_review=existing,
                       feedback={"kind": "quality_review", "review": existing, "cached": True})
            if not existing["passed"]:
                raise DecisionRejected("当前候选已被原需求语义审查拒绝；请修正或停止，不重复抽样审查同一候选。")
            return True
        if len(reviews) >= 2:
            raise ModelBudgetExceeded("最终语义审查最多覆盖两个不同候选；仍有问题时需要人工处理。")
        self._save(run_id, summary="Docker 测试通过，正在核查代码是否符合原始需求")
        self.store.event(run_id, "quality_review_started", "测试通过后继续核查原始需求，不以测试结果代替语义正确性",
                         data={"candidate_hash": fingerprint})
        context = await self._context(run_id)
        context.update(specification=run["intake"].get("specification"),
                       assumptions=run["intake"].get("assumptions", []),
                       frozen_acceptance=run["bundle"]["acceptance"])
        review = validate_review(await self.model.review_final(self.store.run(run_id), context))
        record = {**review, "candidate_hash": fingerprint, "index": len(reviews) + 1,
                  "origin": "model_semantic_review", "timestamp": now()}
        web = run['bundle']['provenance'].get('web')
        if web:
            record['web_runtime_sha256'] = web_runtime_sha256(web)
        self._save(run_id, quality_review=record, quality_reviews=reviews + [record],
                   feedback={"kind": "quality_review", "review": record})
        self.store.event(run_id, "quality_review", "对照原始需求的语义审查已记录", data=record)
        if await self.sandbox.fingerprint(run_id) != fingerprint:
            self._save(run_id, verified_hash=None)
            raise DecisionRejected("候选在语义审查期间变化，旧审查不能作为完成依据；请重新验收。")
        if not review["passed"]:
            self._save(run_id, summary="测试已通过，但原需求语义审查发现问题；主 Agent 将调整计划")
            return False
        return True

    def _dispatch(self, run, decision):
        graph = deepcopy(run["task_graph"])
        additions = decision.get("tasks")
        if not isinstance(additions, list) or not additions:
            raise DecisionRejected("dispatch requires nonempty tasks")
        if len(graph) + len(additions) > run["budget"]["max_tasks"]:
            raise DecisionRejected("Total task budget exceeded")
        known = {t["id"] for t in graph}
        for task in additions:
            required = {"id", "title", "instructions", "depends_on", "write_paths"}
            if not isinstance(task, dict) or not required <= set(task) or set(task) - required - {"profile_id"}:
                raise DecisionRejected("Task fields must be id,title,instructions,depends_on,write_paths")
            selected_profile = None
            if "profile_id" in task:
                if not isinstance(task["profile_id"], str):
                    raise DecisionRejected("profile_id must refer to a frozen enabled profile")
                selected_profile = next((p for p in run.get("profile_snapshot", []) if p["id"] == task["profile_id"] and p["enabled"]), None)
                if selected_profile is None:
                    raise DecisionRejected("Unknown or disabled frozen profile_id")
            identity = task["id"]
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", identity) or identity in known:
                raise DecisionRejected("Task IDs must be unique, bounded and cannot be reused")
            if any(not isinstance(task[k], str) or not task[k].strip() or len(task[k]) > cap
                   for k, cap in (("title", 200), ("instructions", 12000))):
                raise DecisionRejected("Task title/instructions missing or too long")
            for key in ("depends_on", "write_paths"):
                if not isinstance(task[key], list) or any(not isinstance(x, str) for x in task[key]) or len(set(task[key])) != len(task[key]):
                    raise DecisionRejected(f"{key} must contain unique strings")
            for path in task["write_paths"]:
                safe_path(path)
            known.add(identity)
            graph.append({**deepcopy(task), "status": "pending", "agent_id": "worker-" + uuid4().hex[:12],
                          **({"profile_version": selected_profile["version"], "profile_sha256": selected_profile["sha256"]} if selected_profile else {})})
        by_id = {t["id"]: t for t in graph}
        new_ids = {t["id"] for t in additions}
        visiting, visited = set(), set()

        def visit(identity):
            if identity in visiting:
                raise DecisionRejected("Dependency cycle")
            if identity in visited:
                return
            visiting.add(identity)
            for dependency in by_id[identity]["depends_on"]:
                if dependency not in by_id:
                    raise DecisionRejected("Unknown dependency: " + dependency)
                if identity in new_ids and by_id[dependency]["status"] in ("failed", "cancelled"):
                    raise DecisionRejected("Cannot depend on a failed or cancelled task")
                visit(dependency)
            visiting.remove(identity)
            visited.add(identity)

        for identity in by_id:
            visit(identity)
        self._save(run["id"], task_graph=graph)
        self.store.event(run["id"], "plan_revision", "主 Agent 增加实际执行任务",
                         data={"added": additions, "summary": decision["summary"]})

    async def _wave(self, run_id):
        run = self.store.run(run_id)
        graph = deepcopy(run["task_graph"])
        successful = {t["id"] for t in graph if t["status"] == "succeeded"}
        chosen, owned = [], set()
        for task in graph:
            if task["status"] != "pending" or not set(task["depends_on"]) <= successful:
                continue
            if {path.casefold() for path in task["write_paths"]} & owned:
                continue
            chosen.append(task)
            owned.update(path.casefold() for path in task["write_paths"])
            if len(chosen) == run["budget"]["max_parallel"]:
                break
        if not chosen:
            raise DecisionRejected("No ready tasks: cancel/replace failed dependencies or dispatch new tasks")
        context = await self._context(run_id)
        for task in chosen:
            task.update(status="running", started_at=now())
            self.store.step(run_id, task)
        self._save(run_id, task_graph=graph)
        self.store.event(run_id, "dispatch", "向本次任务创建的子 Agent 分配就绪任务",
                         data={"tasks": [{"id": t["id"], "agent_id": t["agent_id"],
                                          "write_paths": t["write_paths"]} for t in chosen]})
        # Workers see the same frozen candidate. Only disjoint write sets share a wave.
        results = await asyncio.gather(*(self.model.work(run, deepcopy(task), deepcopy(context))
                                         for task in chosen), return_exceptions=True)
        summaries = []
        for task, result in zip(chosen, results):
            try:
                if isinstance(result, BaseException):
                    raise result
                patch = await self.sandbox.apply(run_id, result["files"], task["write_paths"])
                task.update(status="succeeded", result={"summary": result["summary"],
                            "changed_paths": sorted(result["files"]), "patch": patch})
                if result["files"]:
                    self._save(run_id, candidate_origin="natural_candidate" if run.get("project_context") else "model", verified_hash=None)
            except asyncio.CancelledError:
                raise
            except SandboxIntegrityError as exc:
                task.update(status="failed", error=str(exc), finished_at=now(), integrity_failure=True)
                self.store.step(run_id, task)
                self._save(run_id, task_graph=graph)
                self.store.event(run_id, "integrity_failure", "候选完整性未获确认，停止运行", task["id"], task)
                raise
            except Exception as exc:
                # Safe transport exceptions never contain HTTP credentials/bodies.
                message = str(exc) if isinstance(exc, (RuntimeErrorSafe, ValueError)) else type(exc).__name__
                task.update(status="failed", error=message)
            task["finished_at"] = now()
            self.store.step(run_id, task)
            self._save(run_id, task_graph=graph)
            self.store.event(run_id, "task_result", "子 Agent 结果已保存", task["id"], deepcopy(task))
            summaries.append({"id": task["id"], "status": task["status"],
                              "result": task.get("result"), "error": task.get("error")})
        self._save(run_id, task_graph=graph, feedback={"kind": "execution_results", "results": summaries})
        self.store.snapshot(run_id, "wave", {"task_graph": graph,
                            "candidate_hash": await self.sandbox.fingerprint(run_id)})

    async def _verify(self, run_id):
        run = self.store.run(run_id)
        if len(run["verifications"]) >= run["budget"]["max_verifications"]:
            raise ModelBudgetExceeded("Independent verification budget exhausted")
        before = await self.sandbox.fingerprint(run_id)
        baseline = run["candidate_origin"] in ("baseline", "imported_baseline")
        seeded = run["bundle"]["provenance"]["kind"] == "seeded_defect"
        classification = ("seeded_defect_baseline" if seeded else "input_baseline") if baseline else (
            "seeded_experiment_candidate" if seeded else "natural_candidate")
        if baseline and run.get("project_context"):
            classification = "imported_baseline"
        scope = {"index": len(run["verifications"]) + 1, "candidate_hash": before,
                 "candidate_origin": run["candidate_origin"], "classification": classification,
                 "acceptance_origin": "model_generated" if run.get("intake")
                     else run["bundle"]["provenance"].get("acceptance_origin", "maintainer_provided")}
        # This begins preparation, not proof that a Docker container started.
        # Only the completed runtime receipt can establish isolated execution.
        self.store.event(run_id, "verification_started", "已请求独立验收，正在准备隔离环境",
                         data={**scope, "stage": "preparing", "container_confirmed": False})
        interruption_reason = "exception"
        try:
            result = await self.sandbox.test(run_id)
            after = await self.sandbox.fingerprint(run_id)
            if before != after:
                interruption_reason = "candidate_changed"
                raise RuntimeErrorSafe("Candidate changed during independent verification")
        except asyncio.CancelledError:
            self.store.event(run_id, "verification_interrupted", "验收被取消，未获得可用于完成判定的完整回执",
                             data={**scope, "stage": "interrupted", "status": "blocked",
                                   "reason": "cancelled", "completion_unknown": True})
            raise
        except Exception as exc:
            self.store.event(run_id, "verification_interrupted", "验收异常中断，未获得可用于完成判定的完整回执",
                             data={**scope, "stage": "interrupted", "status": "blocked",
                                   "reason": interruption_reason, "error_type": type(exc).__name__,
                                   "completion_unknown": True})
            raise
        result = {**result, "candidate_hash": before, "candidate_origin": run["candidate_origin"],
                  "classification": classification, "index": len(run["verifications"]) + 1,
                  "timestamp": now()}
        web = run['bundle']['provenance'].get('web')
        if web and (result.get('web_runtime_sha256') != web_runtime_sha256(web)
                    or result.get('image') != web['image_id'] or result.get('dependency_id') != web['dependency_id']
                    or result.get('lock_sha256') != web['lock_sha256']):
            result.update(status='blocked', reason='Web 验收依赖环境与冻结配置不一致。')
        expected_tests = (run.get("acceptance_manifest") or {}).get("total_test_count")
        if expected_tests is not None:
            result["expected_test_count"] = expected_tests
            if result.get("test_count") != expected_tests and result.get("status") != "blocked":
                result.update(status="failed", discovery_mismatch=True,
                              reason="Frozen regression/new acceptance discovery count mismatch")
        if run.get("intake"):
            result["acceptance_origin"] = "model_generated"
        verifications = run["verifications"] + [result]
        passed = (result["status"] == "passed" and result["isolated"] is True
                  and result["exit_code"] == 0 and result.get("test_count", 0) > 0)
        self._save(run_id, verifications=verifications, verified_hash=before if passed else None,
                   feedback={"kind": "verification", "result": result})
        if web:
            self._save(run_id, verified_runtime_sha256=web_runtime_sha256(web) if passed else None)
        self.store.event(run_id, "verification", "独立 Docker 验收已记录", data=result)
        if result["status"] == "blocked":
            raise RuntimeErrorSafe("Docker verification blocked; no host fallback")

    async def _action(self, run_id, decision):
        run = self.store.run(run_id)
        action = decision["action"]
        if action == "edit":
            await self._main_edit(run, decision)
        elif action == "tool":
            await self._project_tool(run, decision)
        elif action == "dispatch":
            self._dispatch(run, decision)
            await self._wave(run_id)
        elif action == "continue":
            await self._wave(run_id)
        elif action == "cancel":
            ids = decision.get("task_ids", [])
            graph = deepcopy(run["task_graph"])
            eligible = {t["id"] for t in graph if t["status"] == "pending"}
            if not ids or not set(ids) <= eligible:
                raise DecisionRejected("Only existing pending tasks can be cancelled")
            for task in graph:
                if task["id"] in ids:
                    task.update(status="cancelled", finished_at=now())
                    self.store.step(run_id, task)
            self._save(run_id, task_graph=graph, feedback={"kind": "cancelled", "task_ids": ids})
            self.store.event(run_id, "plan_revision", "主 Agent 撤销待执行任务", data=decision)
        elif action == "verify":
            await self._verify(run_id)
        elif action == "finish":
            if any(t["status"] in ("pending", "running") for t in run["task_graph"]):
                raise DecisionRejected("Pending tasks must be completed or explicitly cancelled before finishing")
            fingerprint = await self.sandbox.fingerprint(run_id)
            if not run["verified_hash"] or fingerprint != run["verified_hash"]:
                raise DecisionRejected("Finish requires current candidate to pass independent Docker verification")
            web = run['bundle']['provenance'].get('web')
            if web and run.get('verified_runtime_sha256') != web_runtime_sha256(web):
                raise DecisionRejected('Finish requires verification of the current frozen Web dependency runtime')
            if run.get("intake") and not await self._review_quality(run_id, fingerprint):
                return
            self._save(run_id, status="succeeded", summary=decision["summary"])
        elif action == "stop":
            self._save(run_id, status="needs_human", summary=decision["summary"])
        else:
            raise DecisionRejected("Unknown controller action")

    async def _main_edit(self, run, decision):
        if run.get("project_context") is None:
            raise DecisionRejected("Direct main-Agent edits are available only for project runs")
        if any(task["status"] in ("pending", "running") for task in run["task_graph"]):
            raise DecisionRejected("Complete or cancel all active delegated tasks before a direct edit")
        try:
            files = _sources(decision.get("files"))
            for path in files:
                safe_path(path)
        except (RuntimeErrorSafe, ValueError) as exc:
            raise DecisionRejected(str(exc)) from None
        original = await self.sandbox.read(run["id"])
        try:
            _sources({**original, **files})
        except RuntimeErrorSafe as exc:
            raise DecisionRejected(str(exc)) from None
        before = await self.sandbox.fingerprint(run["id"])
        changed = sorted(path for path in files if files[path] != original.get(path))
        if changed:
            # Invalidate before mutation so a cancelled/failed apply cannot retain
            # evidence for the bytes that existed before the attempted edit.
            self._save(run["id"], verified_hash=None)
        patch = await self.sandbox.apply(run["id"], files, list(files))
        after = await self.sandbox.fingerprint(run["id"])
        # Apply returns trusted evidence; model summaries cannot set verification state.
        feedback = {"kind": "main_edit", "summary": decision["summary"], "changed_paths": changed,
                    "before_sha256": before, "after_sha256": after, "patch": deepcopy(patch)}
        updates = {"feedback": feedback}
        if before != after:
            updates.update(candidate_origin="natural_candidate", verified_hash=None)
        self._save(run["id"], **updates)
        self.store.event(run["id"], "main_edit", "主 Agent 已直接修改候选源码", data=deepcopy(feedback))

    async def _project_tool(self, run, decision):
        if run.get("project_context") is None or not callable(self.project_tool_callback):
            raise DecisionRejected("Project tools require a project run and an installed host callback")
        try:
            arguments = validate_project_tool(decision.get("tool"), decision.get("arguments"))
        except ValueError as exc:
            raise DecisionRejected(str(exc)) from None
        name, run_id = decision["tool"], run["id"]
        before = await self.sandbox.fingerprint(run_id)
        scope = {"tool": name, "arguments": deepcopy(arguments), "candidate_sha256": before,
                 "verification_evidence": False}
        self.store.event(run_id, "tool_started", "主 Agent 请求受控项目工具", data={**scope, "status": "running"})
        try:
            returned = await self.project_tool_callback(run_id, name, deepcopy(arguments))
            if not isinstance(returned, dict):
                raise RuntimeErrorSafe("Project tool callback must return a result object")
            # Detach callback-owned objects and reject oversized/unserializable output.
            encoded = json.dumps(returned, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > 65536:
                raise RuntimeErrorSafe("Project tool result exceeds 64 KiB")
            result = json.loads(encoded)
            if result.get("cancelled") is True or result.get("status") == "cancelled":
                raise asyncio.CancelledError("Project tool reported cancellation")
            if result.get("timed_out") is True or result.get("status") in ("timeout", "timed_out"):
                raise TimeoutError("Project tool exceeded its execution deadline")
            if "candidate_sha256" in result and result["candidate_sha256"] != before:
                raise RuntimeErrorSafe("Project tool result belongs to a different candidate hash")
            after = await self.sandbox.fingerprint(run_id)
            if after != before:
                self._save(run_id, verified_hash=None)
                raise SandboxIntegrityError("Project observation tool changed candidate source; verification was invalidated")
            outcome = result.get("status")
            feedback = {"kind": "tool_result", **scope,
                        "status": outcome if outcome in ("blocked", "failed") else "completed", "result": result}
            if outcome in ("blocked", "failed"):
                feedback["recoverable"] = True
            self._save(run_id, feedback=feedback)
            self.store.event(run_id, "tool_result", "主 Agent 工具结果已记录，等待下一步判断", data=deepcopy(feedback))
        except (asyncio.CancelledError, TimeoutError, ModelBudgetExceeded) as exc:
            self._save(run_id, verified_hash=None)
            self.store.event(run_id, "tool_interrupted", "项目工具被中断，未作为普通失败继续执行",
                             data={**scope, "status": "blocked", "error_type": type(exc).__name__, "completion_unknown": True})
            raise
        except SandboxIntegrityError:
            self.store.event(run_id, "integrity_failure", "项目工具改变了候选源码，停止运行", data=scope)
            raise
        except Exception as exc:
            # Read/search/CLI failures can guide a new decision; budget/cancel cannot.
            if await self.sandbox.fingerprint(run_id) != before:
                self._save(run_id, verified_hash=None)
                self.store.event(run_id, "integrity_failure", "失败的项目工具改变了候选源码，停止运行", data=scope)
                raise SandboxIntegrityError("Failed project tool changed candidate source") from None
            message = str(exc) if isinstance(exc, (RuntimeErrorSafe, ValueError, FileNotFoundError)) else type(exc).__name__
            feedback = {"kind": "tool_result", **scope, "status": "failed", "error": message, "recoverable": True}
            self._save(run_id, feedback=feedback)
            self.store.event(run_id, "tool_result", "项目工具失败，错误已反馈给主 Agent", data=deepcopy(feedback))

    async def _loop(self, run_id):
        run = self.store.run(run_id)
        if run.get("intake", {}).get("status") == "pending":
            if not await self._prepare_requirement(run_id):
                return
            run = self.store.run(run_id)
        await self.sandbox.prepare(run_id, run["bundle"])
        if (run['bundle']['provenance'].get('web') and run['bundle']['provenance']['kind'] == 'seeded_defect'
                and not run['verifications']):
            await self._verify(run_id)
            if self.store.run(run_id)['verifications'][-1]['status'] != 'failed':
                self._save(run_id, status='needs_human', summary='预设 Web 缺陷未触发冻结验收失败，不能继续作为修正控制。')
                return
        for index in range(run["budget"]["max_steps"]):
            current = self.store.run(run_id)
            decision = await self.model.decide(current, await self._context(run_id))
            record = {"index": index + 1, "timestamp": now(), **decision}
            self._save(run_id, controller_decisions=current["controller_decisions"] + [record])
            self.store.event(run_id, "controller_decision", "主 Agent 决策", data=record)
            try:
                await self._action(run_id, decision)
            except DecisionRejected as exc:
                feedback = {"kind": "decision_rejected", "reason": str(exc), "decision": record}
                self._save(run_id, feedback=feedback)
                self.store.event(run_id, "decision_rejected", str(exc), data=feedback)
            if self.store.run(run_id)["status"] != "running":
                return
        raise ModelBudgetExceeded("Controller decision budget exhausted")

    async def execute(self, run_id):
        run = self.store.run(run_id)
        if not run or run.get("engine") != self.revision:
            raise ValueError("Not an autonomous run")
        if run_id in self.running or run["status"] != "queued":
            return
        self.running.add(run_id)
        started = time.monotonic()
        self._save(run_id, status="running", summary="主 Agent 正在编排")
        try:
            configuration = (self.model.config(run["budget"]) if hasattr(self.model, "config")
                             else self.runtime.model_config())
            if configuration != run["model_config"]:
                raise RuntimeErrorSafe("Frozen model configuration changed")
            async with asyncio.timeout(run["budget"]["wall_seconds"]):
                await self._loop(run_id)
        except (ModelBudgetExceeded, TimeoutError) as exc:
            self._save(run_id, status="needs_human", summary=str(exc) or "Run wall-time budget exhausted")
        except asyncio.CancelledError:
            self._save(run_id, status="blocked", summary="运行被中断；保留证据，不自动重放模型调用")
            raise
        except Exception as exc:
            summary = str(exc) if isinstance(exc, (RuntimeErrorSafe, ValueError)) else type(exc).__name__
            self._save(run_id, status="failed", summary=summary)
        finally:
            self.running.discard(run_id)
            if self.project_cleanup_callback is not None:
                try:
                    await self.project_cleanup_callback(run_id)
                except Exception as exc:
                    self.store.event(run_id, 'tool_cleanup_failed', '工具服务清理未确认，保留 TTL 清理和记录', data={'error_type': type(exc).__name__})
            final_run = self.store.run(run_id)
            interrupted = deepcopy(final_run["task_graph"])
            for task in interrupted:
                if task["status"] == "running":
                    task.update(status="failed", error="Execution interrupted; completion unconfirmed", finished_at=now())
                    self.store.step(run_id, task)
                    self.store.event(run_id, "task_interrupted", "子任务未获完成确认", task["id"], task)
            self._save(run_id, task_graph=interrupted)
            self._save(run_id, duration_seconds=round(time.monotonic() - started, 3))
            if self.completion_callback is not None:
                try:
                    publication = await self.completion_callback(run_id)
                    if publication is not None:
                        self._save(run_id, publication=deepcopy(publication))
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (RuntimeErrorSafe, ValueError)) else type(exc).__name__
                    publication = {"status": "failed", "error": message, "candidate_preserved": True}
                    self._save(run_id, publication=publication)
                    self.store.event(run_id, "publication_failed", "项目版本发布失败；候选与验证证据已保留", data=publication)
            self.store.event(run_id, "terminal", "动态编排运行结束", data={
                "status": self.store.run(run_id)["status"], "summary": self.store.run(run_id)["summary"],
                "publication": self.store.run(run_id).get("publication")})
            if final_run.get("intake"):
                try:
                    (self.runtime._run_dir(run_id) / "evidence.json").write_text(
                        json.dumps(self.export(run_id), ensure_ascii=False, indent=2), encoding="utf-8")
                except (OSError, RuntimeErrorSafe):
                    self.store.event(run_id, "export_error", "最终文件导出失败；SQLite 记录仍保留")

    def export(self, run_id):
        return {"run": self.store.run(run_id), "events": self.store.events(run_id),
                "steps": self.store.steps(run_id), "snapshots": self.store.snapshots(run_id, include_context=True)}
