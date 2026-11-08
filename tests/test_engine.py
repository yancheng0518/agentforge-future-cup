"""Engine unit tests with an explicitly simulated runner; NOT sandbox evidence."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agentforge.engine import Engine
from agentforge.storage import Store


class StubRuntime:
    """In-memory protocol stub. Does not import or execute candidate Python."""
    def __init__(self, *, blocked=False, always_fail=False):
        self.blocked, self.always_fail = blocked, always_fail
        self.configured = False
        self.fixed = False
        self.counts = {"prepare": 0, "develop": 0, "fix": 0, "test": 0, "apply": 0, "inspect": 0}
        self.active_test = False
        self.overlapped = False
        self.conflict = False
        self.tampered = False
        self.fail_agents = set()

    async def health(self):
        return {"model": {"configured": self.configured}, "sandbox": {"available": not self.blocked, "reason": "explicit test stub"}, "mode": "mock"}

    async def prepare(self, run_id):
        self.counts["prepare"] += 1
        return {"workspace": "STUB_ONLY", "base_hash": "STUB_ONLY"}

    async def read_project(self, run_id):
        return {"files": [{"path": "todo.py", "content": "fixed" if self.fixed else "base"}]}

    async def retrieve(self, query):
        return {"matches": [{"path": "stub.md", "excerpt": "stub requirement", "score": 1}]}

    async def invoke(self, agent, task, context):
        if agent["id"] in self.fail_agents:
            raise TimeoutError("simulated model timeout")
        kind = task["kind"]
        if kind in ("develop", "fix"):
            self.counts[kind] += 1
            output = {"files": [{"path": "todo.py", "content": "fixed" if kind == "fix" else "bad"}]}
        elif kind in ("inspect", "review"):
            if kind == "inspect":
                self.counts["inspect"] += 1
                self.overlapped |= self.active_test
                await asyncio.sleep(0.01)
            output = {"passed": self.fixed and not self.always_fail, "issues": [] if self.fixed and not self.always_fail else ["missing filter"], "summary": "explicit simulated review"}
        else:
            output = {"summary": "explicit simulated agent"}
        return {"output": output, "model_calls": 0, "model_mode": "test_stub", "tool_calls": []}

    async def apply_candidate(self, run_id, files):
        self.fixed = files[0]["content"] == "fixed"
        return await self.diff(run_id)

    async def diff(self, run_id):
        return {"files": [{"path": "todo.py", "before": "base", "after": "tampered" if self.tampered else ("fixed" if self.fixed else "bad"), "diff": "stub diff"}], "unified_diff": "stub diff"}

    async def test(self, run_id):
        self.counts["test"] += 1
        self.active_test = True
        await asyncio.sleep(0.02)
        self.active_test = False
        passed = self.fixed and not self.always_fail
        return {"status": "blocked" if self.blocked else ("passed" if passed else "failed"), "isolated": not self.blocked, "exit_code": None if self.blocked else (0 if passed else 1), "reason": "explicit stub isolation result", "stdout": "STUB ONLY"}

    async def apply(self, run_id):
        if self.conflict:
            raise ValueError("base conflict")
        self.counts["apply"] += 1
        return {"applied": True, "files": ["todo.py"]}


def make_engine(tmp_path, **options):
    store = Store(tmp_path / "test.sqlite3")
    runtime = StubRuntime(**options)
    return Engine(store, runtime), store, runtime


def test_feedback_loop_and_parallel_validation(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path)
        run = engine.create_run("complete and filter")
        await engine.execute(run["id"])
        saved = store.run(run["id"])
        assert saved["status"] == "succeeded"
        assert saved["attempt"] == 1
        assert next(s for s in store.steps(run["id"]) if s["id"] == "round-1/fix")["attempt"] == 1
        assert runtime.counts["test"] == 2 and runtime.counts["fix"] == 1
        assert runtime.overlapped
        assert store.snapshots(run["id"])
        assert saved["verified_hash"]
        assert any(e["type"] == "feedback" for e in store.events(run["id"]))
    asyncio.run(scenario())


def test_two_repairs_limit(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path, always_fail=True)
        run = engine.create_run("complete and filter")
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "needs_human"
        assert runtime.counts["fix"] == 2 and runtime.counts["test"] == 3
    asyncio.run(scenario())


def test_blocked_resume_preserves_completed_nodes_and_versions(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path, blocked=True)
        run = engine.create_run("complete and filter")
        before = deepcopy(run["agent_versions"])
        agent = store.agents()[0]
        store.save_agent({**agent, "prompt": "changed after run creation"})
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "blocked"
        assert any(s["id"].endswith("/inspect") and s["status"] == "succeeded" for s in store.steps(run["id"]))
        assert not store.run(run["id"]).get("verified_hash")
        runtime.blocked = False
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "succeeded"
        assert runtime.counts["prepare"] == runtime.counts["develop"] == 1
        assert store.run(run["id"])["agent_versions"] == before
    asyncio.run(scenario())


def test_scheduler_priority_load_and_tool_permissions(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path)
        for agent in store.agents():
            if agent["id"] in ("tester", "reviewer"):
                store.save_agent({**agent, "enabled": False})
        for id in ("worker_a", "worker_b"):
            store.save_agent(dict(id=id, name=id, role="reviewer", capabilities=["test", "review"], enabled=True, prompt="", model="default", tools=["run_acceptance", "read_project"]))
        run = engine.create_run("complete and filter")
        await engine.execute(run["id"])
        schedules = [e for e in store.events(run["id"]) if e["type"] == "schedule" and e["node_id"].startswith("round-0/verify/")]
        assert schedules[0]["node_id"].endswith("/test")
        assert schedules[1]["node_id"].endswith("/inspect")
        assert schedules[0]["data"]["selected"] != schedules[1]["data"]["selected"]
        assert schedules[0]["data"]["priority"] > schedules[1]["data"]["priority"]
        assert any(c["load"] == 1 for c in schedules[1]["data"]["candidates"])
        # A capability label alone cannot grant candidate write access.
        dev = next(a for a in store.agents() if a["id"] == "developer")
        store.save_agent({**dev, "tools": ["read_project"]})
        run2 = engine.create_run("another task")
        await engine.execute(run2["id"])
        assert store.run(run2["id"])["status"] == "blocked"
        assert "develop" in store.run(run2["id"])["summary"]
    asyncio.run(scenario())


def test_unknown_dependency_and_timeout_are_explicit(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path)
        run = engine.create_run("test")
        workflow = run["workflow"]
        workflow["nodes"][1]["depends_on"] = ["nonexistent"]
        store.update_run(run["id"], workflow=workflow)
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "failed"
        assert "nonexistent" in store.run(run["id"])["summary"]
        run2 = engine.create_run("test")
        runtime.fail_agents.add("planner")
        await engine.execute(run2["id"])
        assert store.run(run2["id"])["status"] == "failed"
        assert "TimeoutError" in store.run(run2["id"])["summary"]
        assert all(load == 0 for load in engine.loads.values())
    asyncio.run(scenario())


def test_review_template_never_edits_candidate(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path, blocked=True)
        run = engine.create_run("review current code", "review")
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "blocked"
        assert runtime.counts["develop"] == runtime.counts["fix"] == 0
        assert any(s["id"].endswith("/inspect") and s["output"] for s in store.steps(run["id"]))
    asyncio.run(scenario())


def test_failed_agent_is_redispatched_to_another_match(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path)
        template = next(a for a in store.agents() if a["id"] == "planner")
        store.save_agent({**template, "id": "a_timeout", "name": "timeout planner"})
        runtime.fail_agents.add("a_timeout")
        run = engine.create_run("complete and filter")
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "succeeded"
        schedules = [e for e in store.events(run["id"]) if e["type"] == "schedule" and e["node_id"] == "plan"]
        assert [e["data"]["selected"] for e in schedules] == ["a_timeout", "planner"]
        assert schedules[1]["data"]["excluded_after_failure"] == ["a_timeout"]
        assert all(load == 0 for load in engine.loads.values())
    asyncio.run(scenario())


def test_changed_candidate_invalidates_cached_validation_on_resume(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path, blocked=True)
        run = engine.create_run("complete and filter")
        await engine.execute(run["id"])
        assert runtime.counts["inspect"] == 1
        runtime.blocked, runtime.fixed = False, True
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "succeeded"
        assert runtime.counts["inspect"] == 2, "Changed candidate must not reuse old inspection"
        assert runtime.counts["prepare"] == runtime.counts["develop"] == 1
        assert any("旧验证节点失效" in e["message"] for e in store.events(run["id"]))
    asyncio.run(scenario())


def test_model_claimed_test_success_cannot_override_runner(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path, always_fail=True)
        original = runtime.invoke
        async def misleading(agent, task, context):
            result = await original(agent, task, context)
            if task["kind"] in ("test", "review", "inspect"):
                result["output"].update(passed=True, issues=[], test={"status": "passed", "isolated": True, "exit_code": 0},
                                        tool_calls=[{"name": "run_acceptance", "status": "passed"}])
            return result
        runtime.invoke = misleading
        run = engine.create_run("complete and filter")
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "needs_human"
        assert store.run(run["id"])["context"]["test"]["status"] == "failed"
        assert not any(e["type"] == "tool" and e["data"].get("name") == "run_acceptance" for e in store.events(run["id"]))
    asyncio.run(scenario())


def test_failed_model_usage_and_partial_tools_are_retained(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path)
        class CountedFailure(RuntimeError):
            model_calls = 2
            tokens = 17
            metrics_complete = True
            tool_calls = [{"name": "read_project", "status": "completed", "output": {"files": []}}]
        async def failing(agent, task, context):
            raise CountedFailure("second provider request failed")
        runtime.invoke = failing
        run = engine.create_run("task", mode="live")
        await engine.execute(run["id"])
        saved = store.run(run["id"])
        assert saved["status"] == "failed"
        assert saved["model_calls"] == 2 and saved["model_tokens"] == 17
        assert saved["model_metrics_complete"] is True
        assert any(e["type"] == "tool" and e["data"].get("name") == "read_project" for e in store.events(run["id"]))
        assert any(e["type"] == "agent" and e["data"].get("status") == "failed" for e in store.events(run["id"]))
    asyncio.run(scenario())


def test_unknown_failure_usage_is_explicitly_incomplete(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path)
        runtime.fail_agents.add("planner")
        run = engine.create_run("task", mode="live")
        await engine.execute(run["id"])
        saved = store.run(run["id"])
        assert saved["model_calls"] == 0
        assert saved["model_metrics_complete"] is False
    asyncio.run(scenario())


def test_checkpoint_commit_rolls_back_all_evidence_on_failure(tmp_path, monkeypatch):
    import agentforge.storage as storage_module
    engine, store, runtime = make_engine(tmp_path)
    run = engine.create_run("task")
    original_encode = storage_module.encode
    def fail_snapshot(value):
        if isinstance(value, dict) and set(value) == {"timestamp", "node_id", "context"}:
            raise ValueError("simulated storage interruption")
        return original_encode(value)
    monkeypatch.setattr(storage_module, "encode", fail_snapshot)
    import pytest
    with pytest.raises(ValueError):
        store.complete_step(run["id"], {"id": "plan", "status": "succeeded"}, {"plan": "new output"})
    assert store.run(run["id"])["context"] == {}
    assert store.steps(run["id"]) == []
    assert store.snapshots(run["id"]) == []


def test_new_round_does_not_present_stale_test_as_current_evidence(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path)
        observed = []
        original = runtime.invoke
        async def observing(agent, task, context):
            if task["kind"] == "inspect":
                observed.append(deepcopy(context))
            return await original(agent, task, context)
        runtime.invoke = observing
        run = engine.create_run("complete and filter")
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "succeeded"
        second = next(context for context in observed if context["attempt"] == 1)
        assert "test" not in second and "review" not in second and "inspect" not in second
        assert second["previous_validation"]["test"]["status"] == "failed"
        assert second["previous_validation"]["scope"]["attempt"] == 0
        assert second["validation_scope"]["attempt"] == 1
        final = store.run(run["id"])["context"]
        assert final["test"]["evidence_scope"] == final["review"]["evidence_scope"]
    asyncio.run(scenario())


def test_resume_rehydrates_completed_test_for_failed_review(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path)
        original = runtime.invoke
        review_failed = False
        async def fail_once(agent, task, context):
            nonlocal review_failed
            if task["kind"] == "review" and not review_failed:
                review_failed = True
                raise TimeoutError("simulated review timeout after test completion")
            if task["kind"] == "review":
                assert context["test"]["evidence_scope"] == context["validation_scope"]
                assert context["inspect"]["evidence_scope"] == context["validation_scope"]
            return await original(agent, task, context)
        runtime.invoke = fail_once
        run = engine.create_run("complete and filter")
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "failed"
        assert runtime.counts["test"] == 1
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "succeeded"
        assert runtime.counts["test"] == 2, "First-round completed test must not run again during resume"
        assert runtime.counts["inspect"] == 2
    asyncio.run(scenario())


def test_resume_after_fix_checkpoint_advances_to_next_round(tmp_path):
    async def scenario():
        engine, store, runtime = make_engine(tmp_path)
        original = engine._node
        interrupted = False
        async def interrupt_after_fix(run, node, context, step_id):
            nonlocal interrupted
            result = await original(run, node, context, step_id)
            if step_id == "round-1/fix" and not interrupted:
                interrupted = True
                raise asyncio.CancelledError()
            return result
        engine._node = interrupt_after_fix
        run = engine.create_run("complete and filter")
        try:
            await engine.execute(run["id"])
        except asyncio.CancelledError:
            pass
        assert store.run(run["id"])["attempt"] == 0
        assert any(s["id"] == "round-1/fix" and s["status"] == "succeeded" for s in store.steps(run["id"]))
        await engine.execute(run["id"])
        saved = store.run(run["id"])
        assert saved["status"] == "succeeded" and saved["attempt"] == 1
        assert runtime.counts["fix"] == 1 and runtime.counts["test"] == 2
        assert saved["context"]["test"]["evidence_scope"]["attempt"] == 1
    asyncio.run(scenario())
