"""New API contract tests with fake model/sandbox; not live or Docker evidence."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from agentforge.app import create_app
from agentforge.autonomous_sandbox import validate_bundle
from test_engine import StubRuntime


BUNDLE = {"requirement": "Compute a generic value from numbers, unrelated to Todo",
          "files": {"metrics.py": "def total(values):\n    return sum(values)\n"},
          "acceptance": "import unittest\nfrom bridge import call\nclass Case(unittest.TestCase):\n    def test_total(self):\n        self.assertEqual(call('metrics', 'total', [1, 2]), 3)\n",
          "provenance": {"kind": "natural", "note": "Unit fixture only; never execute candidate on host"}}
HEALTH = {"model": {"configured": True}, "sandbox": {"available": True, "reason": "FAKE UNIT TRANSPORT"}}


class FakeSandbox:
    """All methods operate on text dictionaries; no source execution or Docker."""
    validate_bundle = staticmethod(validate_bundle)

    def __init__(self):
        self.projects = {}
        self.test_calls = 0
        self.result = {"status": "passed", "isolated": True, "exit_code": 0, "test_count": 1,
                       "stdout": "FAKE UNIT RESULT, NOT DOCKER", "stderr": ""}

    async def prepare(self, run_id, bundle):
        self.projects.setdefault(run_id, deepcopy(bundle["files"]))

    async def read(self, run_id):
        if run_id not in self.projects:
            raise FileNotFoundError("Candidate is not prepared yet")
        return deepcopy(self.projects[run_id])

    async def fingerprint(self, run_id):
        return hashlib.sha256(json.dumps(await self.read(run_id), sort_keys=True).encode()).hexdigest()

    async def test(self, run_id):
        self.test_calls += 1
        return deepcopy(self.result)


class FakeController:
    def __init__(self, *actions, before_decision=None):
        self.actions = iter(actions)
        self.before_decision = before_decision
        self.calls = 0

    async def decide(self, run, context):
        if self.before_decision:
            self.before_decision(self.calls, run)
        self.calls += 1
        return {"action": next(self.actions), "summary": "Explicit fake controller result"}


def make_app(tmp_path):
    app = create_app(tmp_path, tmp_path / "data", StubRuntime())
    engine = app.state.autonomous_engine
    engine.runtime.settings.update(api_key="UNIT-FAKE-DO-NOT-SEND", model="unit-fake-model",
                                   base_url="https://example.invalid/v1")
    engine.runtime.health = AsyncMock(return_value=deepcopy(HEALTH))
    engine.runtime._post = Mock(side_effect=AssertionError("This test must never send a real request"))
    engine.runtime._command = AsyncMock(side_effect=AssertionError("This test must never execute commands"))
    engine.sandbox = FakeSandbox()
    return app, engine


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def test_new_endpoints_and_store_leave_17_legacy_records_untouched(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        legacy = app.state.store
        for number in range(17):
            legacy.create_run({"id": f"legacy-{number}", "status": "succeeded", "label": f"Original evidence {number}"})
            legacy.event(f"legacy-{number}", "original", "Preserve immutable original evidence")
        before = deepcopy(legacy.runs())
        before_events = {r["id"]: legacy.events(r["id"]) for r in before}
        engine.start = Mock()
        async with client_for(app) as client:
            result = await client.post("/api/autonomous/runs", json={"bundle": BUNDLE})
            assert result.status_code == 201
            value = result.json()
            assert value["mode"] == "live" and value["engine"] == "model-directed-v1"
            assert value["task_graph"] == [] and value["controller_decisions"] == []
            assert "bundle" not in value
            run_id = value["id"]
            assert [r["id"] for r in (await client.get("/api/autonomous/runs")).json()] == [run_id]
            detail = (await client.get(f"/api/autonomous/runs/{run_id}")).json()
            assert detail["run"]["bundle"] == BUNDLE and detail["events"]
            assert (await client.get(f"/api/runs/{run_id}")).status_code == 404
            assert (await client.get("/api/autonomous/runs/legacy-0")).status_code == 404
            assert (await client.post(f"/api/autonomous/runs/{run_id}/apply", json={})).status_code == 404
        assert app.state.autonomous_store.path != legacy.path
        assert legacy.runs() == before
        assert {r["id"]: legacy.events(r["id"]) for r in before} == before_events
        engine.start.assert_called_once_with(run_id)
        engine.runtime._post.assert_not_called()
        engine.runtime._command.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("model,docker", [(False, True), (True, False), (False, False)])
def test_creation_requires_model_and_docker_without_mock_fallback(tmp_path, model, docker):
    async def scenario():
        app, engine = make_app(tmp_path)
        engine.runtime.health.return_value = {"model": {"configured": model}, "sandbox": {"available": docker}}
        engine.start = Mock()
        async with client_for(app) as client:
            response = await client.post("/api/autonomous/runs", json={"bundle": BUNDLE})
            assert response.status_code == 409
        assert app.state.autonomous_store.runs() == []
        engine.start.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    {"bundle": BUNDLE, "mode": "mock"}, {"bundle": BUNDLE, "workflow_id": "development"},
    {"bundle": BUNDLE, "budget": {"max_calls": 0}}, {"bundle": BUNDLE, "budget": {"max_calls": True}},
    {"bundle": BUNDLE, "budget": {"arbitrary": 1}}, {"bundle": {**BUNDLE, "files": {"../escape.py": "x"}}},
    {"bundle": {**BUNDLE, "provenance": {"kind": "pretend_natural"}}}, {"bundle": {**BUNDLE, "acceptance": ""}},
    {"bundle": {**BUNDLE, "role": "developer"}}, {"bundle": "not an object"},
])
def test_new_http_input_validation_rejects_invalid_requests(tmp_path, body):
    async def scenario():
        app, engine = make_app(tmp_path)
        engine.start = Mock()
        async with client_for(app) as client:
            assert (await client.post("/api/autonomous/runs", json=body)).status_code == 422
        assert app.state.autonomous_store.runs() == []
        engine.start.assert_not_called()
    asyncio.run(scenario())


def test_new_endpoints_keep_existing_origin_and_host_protection(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        async with client_for(app) as client:
            assert (await client.post("/api/autonomous/runs", headers={"Origin": "https://untrusted.invalid"}, json={"bundle": BUNDLE})).status_code == 403
            assert (await client.get("/api/autonomous/runs", headers={"Host": "untrusted.invalid"})).status_code == 400
            assert (await client.get("/api/autonomous/runs/missing")).status_code == 404
            assert (await client.get("/api/autonomous/runs/missing/candidate")).status_code == 404
        engine.runtime._post.assert_not_called()
    asyncio.run(scenario())


def test_candidate_is_explicitly_unavailable_before_prepare(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        engine.start = Mock()
        async with client_for(app) as client:
            run_id = (await client.post("/api/autonomous/runs", json={"bundle": BUNDLE})).json()["id"]
            assert (await client.get(f"/api/autonomous/runs/{run_id}/candidate")).status_code == 409
    asyncio.run(scenario())


def test_false_finish_claim_cannot_succeed_without_independent_verification(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        engine.model = FakeController("finish")
        async with client_for(app) as client:
            run_id = (await client.post("/api/autonomous/runs", json={"bundle": BUNDLE, "budget": {"max_steps": 1}})).json()["id"]
            await engine.tasks[run_id]
            detail = (await client.get(f"/api/autonomous/runs/{run_id}")).json()
            assert detail["run"]["status"] == "needs_human"
            assert detail["run"]["verified_hash"] is None
            assert any(e["type"] == "decision_rejected" for e in detail["events"])
        assert engine.sandbox.test_calls == 0
        engine.runtime._post.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_result", [
    {"isolated": False}, {"test_count": 0}, {"exit_code": 1}, {"status": "failed"},
])
def test_finish_gate_rejects_incomplete_or_failed_verification(tmp_path, invalid_result):
    async def scenario():
        app, engine = make_app(tmp_path)
        engine.model = FakeController("verify", "finish")
        engine.sandbox.result.update(invalid_result)
        async with client_for(app) as client:
            run_id = (await client.post("/api/autonomous/runs", json={"bundle": BUNDLE, "budget": {"max_steps": 2}})).json()["id"]
            await engine.tasks[run_id]
            detail = (await client.get(f"/api/autonomous/runs/{run_id}")).json()
            assert detail["run"]["status"] == "needs_human"
            assert detail["run"]["verified_hash"] is None
            assert detail["run"]["verifications"][0]["classification"] == "input_baseline"
        engine.runtime._post.assert_not_called()
    asyncio.run(scenario())


def test_changed_candidate_invalidates_previous_successful_fake_verification(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)

        def tamper_after_verification(index, run):
            if index == 1:
                engine.sandbox.projects[run["id"]]["metrics.py"] = "# external change after verification"

        engine.model = FakeController("verify", "finish", before_decision=tamper_after_verification)
        async with client_for(app) as client:
            run_id = (await client.post("/api/autonomous/runs", json={"bundle": BUNDLE, "budget": {"max_steps": 2}})).json()["id"]
            await engine.tasks[run_id]
            assert app.state.autonomous_store.run(run_id)["status"] == "needs_human"
    asyncio.run(scenario())


def test_two_run_limit_is_atomic_across_awaiting_health_checks(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        reached, release_health = 0, asyncio.Event()

        async def health_barrier():
            nonlocal reached
            reached += 1
            if reached == 3:
                release_health.set()
            await asyncio.wait_for(release_health.wait(), 2)
            return deepcopy(HEALTH)

        engine.runtime.health = health_barrier
        never_complete = asyncio.Event()

        def start_pending(run_id):
            engine.tasks[run_id] = asyncio.create_task(never_complete.wait())
            return engine.tasks[run_id]

        engine.start = start_pending
        try:
            async with client_for(app) as client:
                responses = await asyncio.gather(*(client.post("/api/autonomous/runs", json={"bundle": BUNDLE}) for _ in range(3)))
                assert sorted(r.status_code for r in responses) == [201, 201, 429]
                assert len(app.state.autonomous_store.runs()) == 2
        finally:
            for task in engine.tasks.values():
                task.cancel()
            await asyncio.gather(*engine.tasks.values(), return_exceptions=True)
    asyncio.run(scenario())


def test_lifespan_cancels_active_dynamic_run_and_preserves_interruption(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        entered = asyncio.Event()

        class WaitingController:
            async def decide(self, run, context):
                entered.set()
                await asyncio.Event().wait()

        engine.model = WaitingController()
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                run_id = (await client.post("/api/autonomous/runs", json={"bundle": BUNDLE})).json()["id"]
                await asyncio.wait_for(entered.wait(), 2)
                assert app.state.autonomous_store.run(run_id)["status"] == "running"
        assert engine.tasks[run_id].cancelled()
        assert app.state.autonomous_store.run(run_id)["status"] == "blocked"
        assert any(e["type"] == "terminal" for e in app.state.autonomous_store.events(run_id))
        engine.runtime._post.assert_not_called()
        engine.runtime._command.assert_not_called()
    asyncio.run(scenario())


def test_lifespan_marks_prior_dynamic_execution_interrupted_without_replay(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        run = engine.create_run(BUNDLE)
        app.state.autonomous_store.update_run(run["id"], status="running")
        async with app.router.lifespan_context(app):
            assert app.state.autonomous_store.run(run["id"])["status"] == "blocked"
            assert engine.tasks == {}
        engine.runtime._post.assert_not_called()
    asyncio.run(scenario())
