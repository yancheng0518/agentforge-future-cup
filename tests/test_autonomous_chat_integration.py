"""Requirement-only HTTP integration with fake transport and fake Docker.

The real model protocol, store, routes and orchestration run here. The transport
returns scripted JSON; the sandbox reads strings without importing candidate code.
These tests are not live model, Docker, or natural-repair evidence.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.app import create_app
from agentforge.autonomous_sandbox import validate_bundle
from test_engine import StubRuntime


REQUEST = "创建一个 Python 工具，提供 tally.total(values)，返回整数列表总和；空列表为 0，支持负数。"
SCAFFOLD = "def total(values):\n    raise NotImplementedError('Implement total')\n"
BAD = "def total(values):\n    return 0\n"
GOOD = "def total(values):\n    return sum(values)\n"
ACCEPTANCE = """import unittest
from bridge import call
class Totals(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(call('tally', 'total', []), 0)
    def test_positive(self):
        self.assertEqual(call('tally', 'total', [2, 3]), 5)
    def test_negative(self):
        self.assertEqual(call('tally', 'total', [-4, 1]), -3)
"""
PREPARED = {"status": "ready", "summary": "UNIT FAKE preparation",
            "specification": "提供 tally.total(values)，整数列表求和、空列表零、允许负数。",
            "assumptions": ["本次输入为整数列表；不引入第三方依赖。"],
            "files": {"tally.py": SCAFFOLD}, "acceptance": ACCEPTANCE}
REVIEW_OK = {"passed": True, "issues": [], "summary": "UNIT FAKE semantic review; not real model evidence"}


class TextOnlySandbox:
    """Intentional simulation: isolated=True is a fake gate input, not evidence."""
    validate_bundle = staticmethod(validate_bundle)

    def __init__(self):
        self.files = {}
        self.bundles = {}
        self.test_results = []
        self.apply_calls = []
        self.passing_sources = {GOOD}

    async def prepare(self, run_id, bundle):
        self.bundles[run_id] = deepcopy(bundle)
        self.files[run_id] = deepcopy(bundle["files"])
        return {"simulation": True}

    async def read(self, run_id):
        if run_id not in self.files:
            raise FileNotFoundError("Unit candidate unprepared")
        return deepcopy(self.files[run_id])

    async def fingerprint(self, run_id):
        return hashlib.sha256(json.dumps(await self.read(run_id), ensure_ascii=False,
                                         sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    async def apply(self, run_id, files, allowed_paths):
        assert set(files) <= set(allowed_paths)
        self.apply_calls.append(deepcopy(files))
        self.files[run_id].update(files)
        return {"simulation": True, "candidate_sha256": await self.fingerprint(run_id)}

    async def test(self, run_id):
        passed = self.files[run_id].get("tally.py") in self.passing_sources
        result = {"status": "passed" if passed else "failed", "exit_code": 0 if passed else 1,
                  "isolated": True, "test_count": 3, "stdout": "UNIT SIMULATION, NOT DOCKER",
                  "stderr": "" if passed else "UNIT SIMULATED candidate mismatch"}
        self.test_results.append(deepcopy(result))
        return result


def dispatch(identity, instructions):
    return {"action": "dispatch", "summary": "UNIT FAKE decision", "tasks": [
        {"id": identity, "title": identity, "instructions": instructions,
         "depends_on": [], "write_paths": ["tally.py"]}]}


def action(name):
    return {"action": name, "summary": "UNIT FAKE " + name}


def setup_app(tmp_path, responses):
    app = create_app(ROOT, tmp_path / "data", StubRuntime())
    engine = app.state.autonomous_engine
    engine.runtime.settings.update(api_key="UNIT-FAKE-NEVER-SEND", model="unit-fake-model",
                                   base_url="https://example.invalid/v1", min_request_interval=0)
    engine.runtime.health = AsyncMock(return_value={"model": {"configured": True},
                                                    "sandbox": {"available": True}})
    engine.runtime._command = AsyncMock(side_effect=AssertionError("No command or Docker permitted in unit test"))
    recorded = []
    queue = list(responses)

    def post(payload):
        recorded.append(deepcopy(payload))
        if not queue:
            raise AssertionError("Unexpected fake model request")
        value = queue.pop(0)
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return {"choices": [{"message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}], "usage": {"total_tokens": 7}}

    engine.runtime._post = Mock(side_effect=post)
    engine.sandbox = TextOnlySandbox()
    return app, engine, recorded, queue


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def test_requirement_http_to_dynamic_feedback_repair_and_export(tmp_path):
    async def scenario():
        responses = [PREPARED, REVIEW_OK, dispatch("implement-total", "实现 tally.total"),
                     {"summary": "UNIT FAKE first implementation", "files": {"tally.py": BAD}},
                     action("verify"), dispatch("repair-total", "根据验收反馈修正求和"),
                     {"summary": "UNIT FAKE correction", "files": {"tally.py": GOOD}},
                     action("verify"), action("finish"), REVIEW_OK]
        app, engine, requests, queue = setup_app(tmp_path, responses)
        legacy = app.state.store
        legacy.create_run({"id": "legacy-unchanged", "status": "succeeded", "requirement": "Original evidence"})
        legacy.event("legacy-unchanged", "original", "Retain old evidence")
        before = deepcopy(legacy.runs()), deepcopy(legacy.events("legacy-unchanged"))
        async with client_for(app) as client:
            response = await client.post("/api/autonomous/requests", json={
                "requirement": REQUEST, "budget": {"max_tokens": 300000}})
            assert response.status_code == 201, response.text
            created = response.json()
            assert created["requirement"] == REQUEST
            assert created["mode"] == "live" and created["intake"]["status"] == "pending"
            run_id = created["id"]
            await engine.tasks[run_id]
            detail = (await client.get(f"/api/autonomous/runs/{run_id}")).json()
            result = detail["run"]
            assert result["status"] == "succeeded", result["summary"]
            assert result["requirement"] == REQUEST
            assert result["intake"]["status"] == "completed"
            assert result["intake"]["acceptance_origin"] == "model_generated"
            assert result["bundle"]["acceptance"] == ACCEPTANCE
            assert result["bundle"]["provenance"]["kind"] == "natural"
            assert [task["id"] for task in result["task_graph"]] == ["implement-total", "repair-total"]
            assert len({task["agent_id"] for task in result["task_graph"]}) == 2
            assert [(test["classification"], test["status"]) for test in result["verifications"]] == [
                ("input_baseline", "failed"), ("natural_candidate", "failed"), ("natural_candidate", "passed")]
            assert result["contract_review"]["passed"] is True
            assert result["quality_review"]["passed"] is True
            assert result["quality_review"]["candidate_hash"] == result["verified_hash"]
            assert result["model_metrics"]["model_calls"] == 10
            assert result["model_metrics"]["tokens"] == 70
            assert result["model_metrics"]["contract_retries"] == 0
            candidate = (await client.get(f"/api/autonomous/runs/{run_id}/candidate")).json()
            assert candidate["files"] == {"tally.py": GOOD}
            assert candidate["sha256"] == result["verified_hash"]
            assert [r["id"] for r in (await client.get("/api/autonomous/runs")).json()] == [run_id]
            assert (await client.get(f"/api/runs/{run_id}")).status_code == 404
            assert len(detail["steps"]) == 2
            assert len([event for event in detail["events"] if event["type"] == "verification"]) == 3
        assert (legacy.runs(), legacy.events("legacy-unchanged")) == before
        assert len(requests) == 10 and queue == []
        assert all("tally" in json.dumps(payload, ensure_ascii=False) for payload in requests)
        records = list((engine.runtime._run_dir(run_id) / "model_requests").glob("*.json"))
        assert len(records) == 10
        assert "UNIT-FAKE-NEVER-SEND" not in "".join(path.read_text(encoding="utf-8") for path in records)
        engine.runtime._command.assert_not_called()
    asyncio.run(scenario())


def test_intake_clarification_keeps_request_without_starting_workers(tmp_path):
    async def scenario():
        prepared = {"status": "needs_input", "summary": "需要明确现有系统输入接口",
                    "questions": ["期望读取什么输入并返回什么输出？"]}
        app, engine, requests, queue = setup_app(tmp_path, [prepared])
        async with client_for(app) as client:
            created = (await client.post("/api/autonomous/requests", json={"requirement": "帮我接入现有系统"})).json()
            await engine.tasks[created["id"]]
            detail = (await client.get(f"/api/autonomous/runs/{created['id']}")).json()
            result = detail["run"]
            assert result["status"] == "needs_human"
            assert result["intake"]["status"] == "needs_input"
            assert result["intake"]["questions"] == prepared["questions"]
            assert result["bundle"] is None and result["task_graph"] == []
            assert result["controller_decisions"] == [] and result["verifications"] == []
            assert result["model_metrics"]["model_calls"] == 1
            assert (await client.get(f"/api/autonomous/runs/{created['id']}/candidate")).status_code == 409
        assert len(requests) == 1 and queue == []
        assert engine.sandbox.test_results == [] and engine.sandbox.apply_calls == []
        engine.runtime._command.assert_not_called()
    asyncio.run(scenario())


def test_invalid_intake_retries_remain_protocol_failures_not_code_repairs(tmp_path):
    async def scenario():
        app, engine, requests, queue = setup_app(tmp_path, ["not JSON", "still not JSON"])
        async with client_for(app) as client:
            response = await client.post("/api/autonomous/requests", json={"requirement": REQUEST})
            assert response.status_code == 201
            run_id = response.json()["id"]
            await engine.tasks[run_id]
            result = (await client.get(f"/api/autonomous/runs/{run_id}")).json()["run"]
            assert result["status"] == "failed"
            assert result["bundle"] is None
            assert result["verifications"] == [] and result["task_graph"] == []
            assert result["model_metrics"]["model_calls"] == 2
            assert result["model_metrics"]["contract_retries"] == 1
        assert len(requests) == 2 and queue == []
        assert engine.sandbox.test_results == []
        engine.runtime._command.assert_not_called()
    asyncio.run(scenario())


def test_intake_call_spends_shared_budget_before_any_controller_request(tmp_path):
    async def scenario():
        app, engine, requests, queue = setup_app(tmp_path, [PREPARED])
        async with client_for(app) as client:
            response = await client.post("/api/autonomous/requests", json={
                "requirement": REQUEST, "budget": {"max_calls": 1, "max_tokens": 300000}})
            assert response.status_code == 201
            run_id = response.json()["id"]
            await engine.tasks[run_id]
            result = (await client.get(f"/api/autonomous/runs/{run_id}")).json()["run"]
            assert result["status"] == "needs_human"
            assert result["intake"]["status"] == "pending"
            assert len(result["intake_proposals"]) == 1
            assert result["contract_reviews"] == [] and result["verifications"] == []
            assert result["model_metrics"]["model_calls"] == 1
            assert result["task_graph"] == []
        assert len(requests) == 1 and queue == []
        assert engine.sandbox.apply_calls == []
    asyncio.run(scenario())


def test_passing_generated_tests_do_not_bypass_semantic_review_and_replanning(tmp_path):
    async def scenario():
        rejected = {"passed": False, "issues": ["返回固定零不符合原始求和需求。"],
                    "summary": "UNIT FAKE semantic rejection despite passing generated tests"}
        responses = [PREPARED, REVIEW_OK, dispatch("first-total", "实现求和"),
                     {"summary": "UNIT FAKE wrong implementation", "files": {"tally.py": BAD}},
                     action("verify"), action("finish"), rejected,
                     dispatch("repair-original-semantics", "按原始需求修正，不迎合测试缺陷"),
                     {"summary": "UNIT FAKE semantic correction", "files": {"tally.py": GOOD}},
                     action("verify"), action("finish"), REVIEW_OK]
        app, engine, requests, queue = setup_app(tmp_path, responses)
        # Intentionally weak fake test oracle: both implementations pass. This
        # is a labelled unit scenario, not a planted defect in any live run.
        engine.sandbox.passing_sources.add(BAD)
        async with client_for(app) as client:
            response = await client.post("/api/autonomous/requests", json={
                "requirement": REQUEST, "budget": {"max_tokens": 300000}})
            assert response.status_code == 201
            run_id = response.json()["id"]
            await engine.tasks[run_id]
            result = (await client.get(f"/api/autonomous/runs/{run_id}")).json()["run"]
            assert result["status"] == "succeeded", result["summary"]
            assert [review["passed"] for review in result["quality_reviews"]] == [False, True]
            assert result["quality_reviews"][0]["candidate_hash"] != result["quality_reviews"][1]["candidate_hash"]
            assert result["quality_review"]["candidate_hash"] == result["verified_hash"]
            assert [test["status"] for test in result["verifications"]] == ["failed", "passed", "passed"]
            assert [task["id"] for task in result["task_graph"]] == ["first-total", "repair-original-semantics"]
            assert result["model_metrics"]["model_calls"] == 12
        assert len(requests) == 12 and queue == []
        # The next main-Agent request contains the actual semantic failure.
        assert "返回固定零不符合原始求和需求" in json.dumps(requests[7], ensure_ascii=False)
        engine.runtime._command.assert_not_called()
    asyncio.run(scenario())
