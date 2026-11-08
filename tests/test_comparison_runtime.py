"""Comparison controls with fake transport and fake Docker; no live evidence."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import threading
import time
from unittest.mock import patch

import pytest

from agentforge.comparison import (AuditPersistenceError, BudgetExceeded, BudgetRuntime, ComparisonEngine,
                                   SingleDirectEngine, arm_definition)
from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.storage import Store


ROOT = Path(__file__).resolve().parents[1]
SECRET = "comparison-test-not-a-real-secret"
REVIEWER = {"id": "reviewer", "role": "reviewer", "version": 1, "tools": ["read_project"], "model": "default"}


def response(content='{"summary":"ok","passed":true,"issues":[]}', usage=True, tools=None):
    message = {"role": "assistant", "content": content, "reasoning_content": "PRIVATE-REASONING-MUST-NOT-BE-ARCHIVED"}
    if tools:
        message["tool_calls"] = tools
    value = {"choices": [{"message": message, "finish_reason": "stop"}]}
    if usage:
        value["usage"] = {"total_tokens": 7, "prompt_tokens": 4, "completion_tokens": 3}
    return value


@pytest.fixture
def workspace(tmp_path):
    for name in ("examples", "knowledge", "acceptance", "benchmarks"):
        shutil.copytree(ROOT / name, tmp_path / name)
    settings = {"api_key": SECRET, "key_source": "unit_test", "model": "fake-model",
                "base_url": "https://example.invalid/v1", "timeout": 5, "min_request_interval": 0,
                "image": "fake-image:never-run", "docker_backend": "native"}
    with patch("agentforge.runtime._settings", return_value=settings), \
            patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No actual network")), \
            patch("asyncio.create_subprocess_exec", side_effect=AssertionError("No host execution or Docker")):
        yield tmp_path


def test_atomic_shared_request_limit_with_parallel_invocations(workspace):
    runtime = BudgetRuntime(workspace, workspace / "data", max_requests=1)
    started = threading.Event()
    release = threading.Event()

    def transport(payload):
        started.set()
        assert release.wait(3)
        return response()

    async def scenario():
        with patch.object(Runtime, "_post", side_effect=transport) as post:
            first = asyncio.create_task(runtime.invoke(REVIEWER, {"mode": "live", "kind": "inspect"}, {"run_id": "race"}))
            assert await asyncio.to_thread(started.wait, 2)
            try:
                with pytest.raises(BudgetExceeded) as caught:
                    await runtime.invoke(REVIEWER, {"mode": "live", "kind": "review"}, {"run_id": "race"})
                assert caught.value.model_calls == 0
                assert caught.value.request_ids == []
                assert caught.value.metrics_complete is True
            finally:
                release.set()
            result = await first
            assert result["model_calls"] == 1 and result["tokens"] == 7
            assert post.call_count == 1
        budget = runtime.budget_record("race")
        assert budget["sent_requests"] == 1 and budget["denied_requests"] == 1
        assert budget["denials"][0]["reason"] == "request_limit"
        assert budget["metrics_complete"] is True
        assert len({i["invocation_id"] for i in budget["invocations"]}) == 2
    asyncio.run(scenario())


def test_payload_limit_uses_wire_json_bytes_and_never_sends(workspace):
    runtime = BudgetRuntime(workspace, workspace / "data", max_payload_bytes=100)
    async def scenario():
        with patch.object(Runtime, "_post") as post, pytest.raises(BudgetExceeded) as caught:
            await runtime.invoke(REVIEWER, {"mode": "live"}, {"run_id": "large", "requirement": "中文" * 100})
        post.assert_not_called()
        assert caught.value.model_calls == 0
        budget = runtime.budget_record("large")
        assert budget["sent_requests"] == 0
        assert budget["denials"][0]["payload_bytes"] > 1200
        assert budget["denials"][0]["reason"] == "payload_byte_limit"
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["agent_model", "base_url", "temperature", "image"])
def test_frozen_request_configuration_cannot_be_overridden(workspace, change):
    runtime = BudgetRuntime(workspace, workspace / "data")
    agent = deepcopy(REVIEWER)
    if change == "agent_model":
        agent["model"] = "different-model"
    elif change in ("base_url", "image"):
        runtime.settings[change] = "https://different.invalid/v1" if change == "base_url" else "different"
    async def scenario():
        with patch.object(Runtime, "_post") as post:
            if change == "temperature":
                original = runtime._post
                def altered(payload):
                    return original({**payload, "temperature": 0.9})
                with patch.object(runtime, "_post", side_effect=altered), pytest.raises(BudgetExceeded):
                    await runtime.invoke(agent, {"mode": "live"}, {"run_id": "config"})
            else:
                with pytest.raises(BudgetExceeded):
                    await runtime.invoke(agent, {"mode": "live"}, {"run_id": "config"})
        post.assert_not_called()
        assert runtime.budget_record("config")["sent_requests"] == 0
    asyncio.run(scenario())


def test_retry_usage_and_private_request_archive_are_complete_and_redacted(workspace):
    runtime = BudgetRuntime(workspace, workspace / "data")
    async def scenario():
        with patch.object(Runtime, "_post", side_effect=[response("prose " + SECRET), response()]) as post:
            result = await runtime.invoke(REVIEWER, {"mode": "live", "kind": "inspect"}, {"run_id": "retry", "_comparison_node_id": "round-0/verify/inspect"})
        assert result["model_calls"] == 2 and result["tokens"] == 14
        assert result["contract_retries"] == 1 and len(result["model_diagnostics"]) == 1
        budget = runtime.budget_record("retry")
        assert [r["request_id"] for r in budget["requests"]] == result["request_ids"]
        for record in budget["requests"]:
            raw = (runtime.data_dir / record["artifact_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == record["artifact_sha256"]
            assert SECRET.encode() not in raw
            assert b"PRIVATE-REASONING" not in raw
            artifact = json.loads(raw)
            assert artifact["request"]["node_id"] == "round-0/verify/inspect"
            assert record["payload_bytes"] == len(json.dumps(post.call_args_list[record["sequence"]-1].args[0]).encode())
            assert artifact["payload"]["model"] == "fake-model"
            assert artifact["response"]["choices"][0]["finish_reason"] == "stop"
    asyncio.run(scenario())


def test_failed_http_consumes_budget_but_denial_does_not(workspace):
    runtime = BudgetRuntime(workspace, workspace / "data", max_requests=1)
    async def scenario():
        with patch.object(Runtime, "_post", side_effect=RuntimeErrorSafe("simulated HTTP 503")) as post:
            for expected in (1, 0):
                with pytest.raises(RuntimeErrorSafe) as caught:
                    await runtime.invoke(REVIEWER, {"mode": "live"}, {"run_id": "failed"})
                assert caught.value.model_calls == expected
                assert caught.value.metrics_complete is (expected == 0)
            assert post.call_count == 1
        record = runtime.budget_record("failed")
        assert record["sent_requests"] == record["denied_requests"] == 1
        assert record["requests"][0]["status"] == "failed"
        assert record["model_tokens_known"] == 0 and not record["metrics_complete"]
    asyncio.run(scenario())


def test_earlier_unknown_usage_is_not_erased_by_retry_success(workspace):
    runtime = BudgetRuntime(workspace, workspace / "data")
    async def scenario():
        with patch.object(Runtime, "_post", side_effect=[response("bad", usage=False), response()]):
            result = await runtime.invoke(REVIEWER, {"mode": "live"}, {"run_id": "unknown"})
        assert result["model_calls"] == 2 and result["tokens"] == 7
        assert not result["metrics_complete"]
        assert not runtime.budget_record("unknown")["metrics_complete"]
    asyncio.run(scenario())


class FakeDockerRuntime(BudgetRuntime):
    def __init__(self, *args, always_fail=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.always_fail = always_fail
        self.active_test = False
        self.overlap = False
        self.model_contexts = []
        self.tests_seen = 0

    async def invoke(self, agent, task, context):
        self.model_contexts.append((agent["id"], task["kind"], deepcopy(context)))
        if task["kind"] == "inspect":
            self.overlap |= self.active_test
        return await super().invoke(agent, task, context)

    async def test(self, run_id):
        self.tests_seen += 1
        self.active_test = True
        await asyncio.sleep(0.03)
        self.active_test = False
        source = (await self.read_project(run_id))["files"][0]["content"]
        passed = "fixed" in source and not self.always_fail
        return {"status": "passed" if passed else "failed", "exit_code": 0 if passed else 1, "isolated": True,
                "stdout": "AGENTFORGE_ACCEPTANCE=" + json.dumps({"tests": 10, "passed": passed}) + "\n",
                "stderr": "Ran 10 tests in 0.001s\n" + ("OK" if passed else "FAILED (failures=1)"),
                "reason": "EXPLICIT FAKE DOCKER FOR UNIT TEST", "duration_ms": 30,
                "task_id": "completion", "acceptance_sha256": self.snapshot_task()["acceptance_sha256"]}


def fake_model(payload):
    data = json.loads(payload["messages"][1]["content"])
    kind, context = data["task"]["kind"], data["context"]
    if kind in ("develop", "fix"):
        value = {"summary": "UNIT STUB", "files": [{"path": "todo.py", "content": "# fixed\n" if kind == "fix" else "# bad\n"}]}
    elif kind in ("inspect", "review"):
        passed = "fixed" in context["project"]["files"][0]["content"]
        value = {"summary": "UNIT STUB", "passed": passed, "issues": [] if passed else ["stub failure"]}
    elif kind == "plan":
        value = {"summary": "UNIT STUB", "tasks": []}
    else:
        value = {"summary": "UNIT STUB", "tests": []}
    return response(json.dumps(value))


@pytest.mark.parametrize("arm,overlap", [("fixed_serial", False), ("dynamic_parallel", True)])
def test_multi_arm_order_contexts_and_frozen_agent_definitions(workspace, arm, overlap):
    runtime = FakeDockerRuntime(workspace, workspace / "data")
    store = Store(workspace / "test.sqlite3")
    engine = ComparisonEngine(store, runtime, arm)
    run = engine.configure_run("complete and filter")
    definition = arm_definition(arm)
    assert run["workflow"] == definition["workflow"] and run["agent_versions"] == definition["agent_versions"]
    async def scenario():
        with patch.object(Runtime, "_post", side_effect=fake_model):
            await engine.execute(run["id"])
    asyncio.run(scenario())
    saved = store.run(run["id"])
    assert saved["status"] == "succeeded" and saved["attempt"] == 1
    assert runtime.overlap is overlap
    for _, kind, context in runtime.model_contexts:
        if kind == "inspect":
            assert "test" not in context and "review" not in context
        if kind == "test":
            assert "inspect" not in context and "review" not in context
    events = store.events(run["id"])
    if arm == "fixed_serial":
        first = next(e for e in events if e["type"] == "agent" and e["node_id"] == "round-0/verify/inspect")
        test = next(e for e in events if e["type"] == "tool" and e["node_id"] == "round-0/verify/test")
        assert first["id"] < test["id"]
        assert all(e["data"]["policy"] == "fixed_serial" for e in events if e["type"] == "schedule")
    agents = [e["data"] for e in events if e["type"] == "agent"]
    assert all(e["invocation_id"] and len(e["request_ids"]) == e["model_calls"] for e in agents)
    assert saved["model_calls"] == runtime.budget_record(run["id"])["sent_requests"]


@pytest.mark.parametrize("always_fail,expected,attempt", [(False, "succeeded", 1), (True, "needs_human", 2)])
def test_single_direct_uses_only_one_agent_and_true_test_feedback(workspace, always_fail, expected, attempt):
    runtime = FakeDockerRuntime(workspace, workspace / "data", always_fail=always_fail)
    store = Store(workspace / "single.sqlite3")
    engine = SingleDirectEngine(store, runtime)
    run = engine.configure_run("complete and filter")
    assert run["agent_versions"] == arm_definition("single_direct")["agent_versions"]
    async def scenario():
        with patch.object(Runtime, "_post", side_effect=fake_model):
            await engine.execute(run["id"])
    asyncio.run(scenario())
    saved = store.run(run["id"])
    assert saved["status"] == expected and saved["attempt"] == attempt
    assert runtime.tests_seen == attempt + 1
    assert {a for a, _, _ in runtime.model_contexts} == {"single_direct"}
    assert [k for _, k, _ in runtime.model_contexts] == ["develop"] + ["fix"] * attempt
    assert all(c["feedback"]["test"]["status"] == "failed" for _, kind, c in runtime.model_contexts if kind == "fix")
    assert not saved["applied"] and saved["independent_review"] is False
    gates = [e for e in store.events(run["id"]) if e["type"] == "validation"]
    assert gates and all("review" not in e["data"] and e["data"]["independent_review"] is False for e in gates)


def test_fixed_serial_does_not_fallback_and_exception_ids_persist(workspace):
    runtime = FakeDockerRuntime(workspace, workspace / "data")
    store = Store(workspace / "fixed.sqlite3")
    engine = ComparisonEngine(store, runtime, "fixed_serial")
    run = engine.configure_run("complete and filter")
    async def scenario():
        with patch.object(Runtime, "_post", side_effect=RuntimeErrorSafe("simulated failure")):
            await engine.execute(run["id"])
    asyncio.run(scenario())
    assert store.run(run["id"])["status"] == "failed"
    events = [e for e in store.events(run["id"]) if e["type"] == "agent"]
    assert len(events) == 1 and events[0]["data"]["status"] == "failed"
    assert events[0]["data"]["invocation_id"]
    assert len(events[0]["data"]["request_ids"]) == 1
    assert runtime.budget_record(run["id"])["sent_requests"] == 1


def test_arm_definitions_are_pure_and_multi_agents_identical():
    serial = arm_definition("fixed_serial")
    parallel = arm_definition("dynamic_parallel")
    assert serial["agent_versions"] == parallel["agent_versions"]
    serial["agent_versions"][0]["prompt"] = "changed local copy"
    assert arm_definition("fixed_serial")["agent_versions"] == parallel["agent_versions"]


def test_received_response_and_usage_survive_archive_failure(workspace):
    runtime = BudgetRuntime(workspace, workspace / "data")
    original = Path.write_bytes
    writes = 0
    def failing_second_write(path, content):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated disk error")
        return original(path, content)
    async def scenario():
        with patch.object(Runtime, "_post", return_value=response()) as post, patch.object(Path, "write_bytes", failing_second_write):
            with pytest.raises(AuditPersistenceError) as caught:
                await runtime.invoke(REVIEWER, {"mode": "live"}, {"run_id": "archive-failure"})
            assert caught.value.model_calls == 1 and caught.value.tokens == 7 and caught.value.metrics_complete
            budget = runtime.budget_record("archive-failure")
            assert not budget["audit_complete"] and budget["metrics_complete"]
            record = budget["requests"][0]
            assert record["status"] == "completed" and record["usage"]["total_tokens"] == 7
            emergency = budget["unpersisted_artifacts"][record["request_id"]]
            assert emergency["response"]["choices"][0]["message"]["content"].startswith('{"summary"')
            assert "reasoning_content" not in emergency["response"]["choices"][0]["message"]
            with pytest.raises(BudgetExceeded) as second:
                await runtime.invoke(REVIEWER, {"mode": "live"}, {"run_id": "archive-failure"})
            assert second.value.model_calls == 0
            assert post.call_count == 1
    asyncio.run(scenario())
