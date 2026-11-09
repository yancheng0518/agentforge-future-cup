"""Unit-only fake transport tests. No live model, Docker, or candidate execution."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from agentforge.autonomous_model import (AutonomousModel, ModelBudgetExceeded,
                                        ModelProtocolError, validate_decision, validate_work)
from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.storage import Store


def response(value, tokens=19, **message_fields):
    content = json.dumps(value) if not isinstance(value, str) else value
    result = {"choices": [{"message": {"content": content, **message_fields}, "finish_reason": "stop"}]}
    if tokens is not None:
        result["usage"] = {"total_tokens": tokens, "prompt_tokens": 10, "completion_tokens": tokens - 10}
    return result


def decision(action="verify", **extra):
    return {"action": action, "summary": "Evidence determines the next action", **extra}


def task(identifier="parse-observations"):
    return {"id": identifier, "title": "Parse sensor observations", "instructions": "Implement input normalization and report assumptions",
            "depends_on": [], "write_paths": ["sensors/parser.py"]}


class AutonomousModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentforge-autonomous-model-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = Runtime(self.root, self.root / "data")
        self.runtime.settings.update(api_key="FAKE-SECRET-NOT-A-REAL-KEY", model="unit-fake-model",
                                     base_url="https://example.invalid/v1", min_request_interval=0)
        self.network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Unit tests must never access network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.store = Store(self.root / "state.sqlite3")
        self.run = {"id": "run-1", "mode": "live", "requirement": "Process arbitrary sensor observations",
                    "budget": {"max_calls": 20, "max_tokens": 300000, "max_output_tokens": 4096}}
        self.store.create_run(self.run)
        self.adapter = AutonomousModel(self.runtime, self.store)
        self.runtime._post = Mock(return_value=response(decision()))

    def metrics(self):
        return self.store.run(self.run["id"]).get("model_metrics", {})

    def records(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.runtime._run_dir(self.run["id"]) / "model_requests").glob("*.json"))]

    async def wait_started(self, signal):
        for _ in range(1000):
            if signal.is_set():
                return
            await asyncio.sleep(0.001)
        self.fail("Fake transport did not start")

    async def test_main_generates_arbitrary_tasks_without_role_templates(self):
        output = decision("dispatch", tasks=[task()])
        self.runtime._post.return_value = response(output)
        self.assertEqual(await self.adapter.decide(self.run, {"project": {"sensors/parser.py": ""}}), output)
        payload = self.runtime._post.call_args.args[0]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIn("no predefined", payload["messages"][0]["content"])
        self.assertEqual(self.metrics()["model_calls"], 1)
        self.assertEqual(self.metrics()["tokens"], 19)
        self.assertTrue(self.metrics()["metrics_complete"])
        self.assertEqual(self.records()[0]["status"], "accepted")

    async def test_worker_receives_assignment_and_returns_multiple_generic_files(self):
        result = {"summary": "Implemented parser and statistics", "files": {"sensors/parser.py": "def parse(x): return x\n", "stats.py": "def count(x): return len(x)\n"}}
        self.runtime._post.return_value = response(result)
        self.assertEqual(await self.adapter.work(self.run, task(), {"project": {}}), result)
        sent = json.loads(self.runtime._post.call_args.args[0]["messages"][1]["content"])
        self.assertEqual(sent["context"]["assignment"]["id"], task()["id"])
        self.assertEqual(self.records()[0]["task_id"], task()["id"])

    async def test_analysis_only_worker_can_return_no_edits(self):
        result = {"summary": "The parser violates the empty-input requirement", "files": {}}
        self.runtime._post.return_value = response(result)
        self.assertEqual(await self.adapter.work(self.run, task(), {}), result)

    async def test_one_format_retry_retains_both_outputs_and_counts_as_format_only(self):
        self.runtime._post.side_effect = [response("not JSON"), response(decision("continue"))]
        output = await self.adapter.decide(self.run, {})
        self.assertEqual(output["action"], "continue")
        self.assertEqual(self.metrics()["model_calls"], 2)
        self.assertEqual(self.metrics()["contract_retries"], 1)
        self.assertEqual(self.metrics()["tokens"], 38)
        first, second = self.records()
        self.assertEqual(first["content"], "not JSON")
        self.assertEqual(first["status"], "protocol_failed")
        self.assertEqual(first["protocol_error"]["kind"], "json_syntax")
        self.assertFalse(first["format_retry"])
        self.assertTrue(second["format_retry"])
        self.assertIn("format retry", second["request"]["messages"][-1]["content"])

    async def test_second_invalid_schema_stops_and_preserves_failure(self):
        self.runtime._post.return_value = response({"action": "finish", "invented": "done"})
        with self.assertRaises(ModelProtocolError):
            await self.adapter.decide(self.run, {})
        self.assertEqual(self.metrics()["model_calls"], 2)
        self.assertEqual(self.metrics()["contract_retries"], 1)
        self.assertEqual([r["status"] for r in self.records()], ["protocol_failed", "protocol_failed"])
        self.assertFalse(self.records()[-1]["retry_scheduled"])

    async def test_transport_failure_does_not_trigger_concealed_retry(self):
        self.runtime._post.side_effect = RuntimeErrorSafe("Model HTTP 429")
        with self.assertRaisesRegex(RuntimeErrorSafe, "429"):
            await self.adapter.decide(self.run, {})
        self.assertEqual(self.runtime._post.call_count, 1)
        self.assertFalse(self.metrics()["metrics_complete"])
        self.assertEqual(self.metrics()["unknown_usage_calls"], 1)
        self.assertGreater(self.metrics()["budget_tokens"], 0)
        self.assertEqual(self.metrics()["tokens"], 0)  # Known tokens only; complete=false.
        self.assertEqual(self.records()[0]["status"], "transport_failed")

    async def test_missing_provider_usage_is_not_reported_as_known_zero(self):
        self.runtime._post.return_value = response(decision(), tokens=None)
        await self.adapter.decide(self.run, {})
        self.assertFalse(self.metrics()["metrics_complete"])
        self.assertFalse(self.records()[0]["usage_known"])
        self.assertEqual(self.metrics()["budget_tokens"], self.records()[0]["reserved_budget_tokens"])
        event = [v for v in self.store.events(self.run["id"]) if v["type"] == "model_response"][0]
        self.assertIsNone(event["data"]["tokens"])

    async def test_simultaneous_workers_share_request_limit_even_with_stale_run_argument(self):
        started, release = threading.Event(), threading.Event()
        self.run["budget"]["max_calls"] = 1

        def pending(payload):
            started.set()
            release.wait(3)
            return response({"summary": "Done", "files": {}})

        self.runtime._post.side_effect = pending
        first = asyncio.create_task(self.adapter.work(self.run, task("first"), {}))
        try:
            await self.wait_started(started)
            with self.assertRaises(ModelBudgetExceeded):
                await self.adapter.work(self.run, task("second"), {})
            self.assertEqual(self.metrics()["in_flight"], 1)
            self.assertEqual(self.records()[0]["status"], "in_flight")
        finally:
            release.set()
            await first
        self.assertEqual(self.metrics()["model_calls"], 1)
        self.assertEqual(self.metrics()["in_flight"], 0)
        self.assertEqual(self.runtime._post.call_count, 1)

    async def test_parallel_usage_accounting_does_not_lose_updates(self):
        self.runtime._post.side_effect = [response({"summary": "First", "files": {}}, tokens=30),
                                          response({"summary": "Second", "files": {}}, tokens=45)]
        await asyncio.gather(self.adapter.work(self.run, task("first"), {}), self.adapter.work(self.run, task("second"), {}))
        self.assertEqual(self.metrics()["tokens"], 75)
        self.assertEqual(self.metrics()["budget_tokens"], 75)
        self.assertEqual(self.metrics()["reserved_tokens"], 0)
        self.assertEqual(self.metrics()["in_flight"], 0)
        self.assertEqual(sorted(r["sequence"] for r in self.records()), [1, 2])

    async def test_insufficient_token_reservation_prevents_send(self):
        self.run["budget"]["max_tokens"] = 20
        with self.assertRaises(ModelBudgetExceeded):
            await self.adapter.decide(self.run, {})
        self.runtime._post.assert_not_called()

    async def test_reported_usage_over_budget_blocks_future_requests(self):
        self.runtime._post.return_value = response(decision(), tokens=400000)
        await self.adapter.decide(self.run, {})
        with self.assertRaises(ModelBudgetExceeded):
            await self.adapter.decide(self.run, {})
        self.assertEqual(self.metrics()["tokens"], 400000)
        self.assertEqual(self.runtime._post.call_count, 1)

    async def test_cancelled_http_call_is_preserved_with_unknown_remote_usage(self):
        started, release = threading.Event(), threading.Event()

        def pending(payload):
            started.set()
            release.wait(3)
            return response(decision())

        self.runtime._post.side_effect = pending
        current = asyncio.create_task(self.adapter.decide(self.run, {}))
        try:
            await self.wait_started(started)
            current.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await current
        finally:
            release.set()
        self.assertEqual(self.records()[0]["status"], "cancelled")
        self.assertFalse(self.metrics()["metrics_complete"])
        self.assertEqual(self.metrics()["in_flight"], 0)
        self.assertEqual(self.metrics()["unknown_usage_calls"], 1)

    async def test_secrets_and_provider_reasoning_never_enter_evidence(self):
        secret = self.runtime.settings["api_key"]
        self.run["requirement"] += secret
        self.runtime._post.return_value = response(decision(summary="Checked " + secret), reasoning_content="PRIVATE-REASONING-DO-NOT-STORE")
        result = await self.adapter.decide(self.run, {})
        evidence = json.dumps(self.records(), ensure_ascii=False) + json.dumps(self.store.events(self.run["id"]))
        self.assertNotIn(secret, evidence)
        self.assertNotIn("PRIVATE-REASONING-DO-NOT-STORE", evidence)
        self.assertEqual(result["summary"], "Checked [REDACTED]")

    async def test_unexpected_tool_calls_are_format_failures_and_never_executed(self):
        self.runtime._post.side_effect = [response(decision(), tool_calls=[{"function": {"name": "shell", "arguments": "bad"}}]), response(decision())]
        await self.adapter.decide(self.run, {})
        self.assertTrue(self.records()[0]["unexpected_tool_calls"])
        self.assertEqual(self.records()[0]["status"], "protocol_failed")

    async def test_oversized_context_is_rejected_without_silent_truncation_or_send(self):
        with self.assertRaisesRegex(RuntimeErrorSafe, "not silently truncated"):
            await self.adapter.decide(self.run, {"project": "x" * 180001})
        self.runtime._post.assert_not_called()

    async def test_frozen_model_mismatch_blocks_send(self):
        self.run["model_config"] = {"model": "a-different-model"}
        with self.assertRaisesRegex(RuntimeErrorSafe, "configuration changed"):
            await self.adapter.decide(self.run, {})
        self.runtime._post.assert_not_called()

    async def test_real_adapter_never_silently_switches_to_mock(self):
        self.run["mode"] = "mock"
        with self.assertRaisesRegex(RuntimeErrorSafe, "mock mode"):
            await self.adapter.decide(self.run, {})
        self.runtime._post.assert_not_called()

    async def test_unknown_exception_details_are_not_exposed(self):
        self.runtime._post.side_effect = ValueError("sensitive arbitrary server detail")
        with self.assertRaisesRegex(RuntimeErrorSafe, "ValueError"):
            await self.adapter.decide(self.run, {})
        self.assertNotIn("sensitive arbitrary server detail", json.dumps(self.records()))


class AutonomousProtocolTests(unittest.TestCase):
    def test_main_can_cancel_pending_work_or_continue_a_dependency_graph(self):
        self.assertEqual(validate_decision(decision("cancel", task_ids=["obsolete"])), decision("cancel", task_ids=["obsolete"]))
        self.assertEqual(validate_decision(decision("continue")), decision("continue"))

    def test_schema_does_not_accept_fake_fixed_role_or_unbounded_shapes(self):
        invalid = [decision("dispatch", tasks=[]), decision("dispatch", tasks=[task(), task()]),
                   decision("cancel", task_ids=[]), {**decision(), "passed": True},
                   {**task(), "role": "developer"}, decision("unknown")]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                validate_decision(item)

    def test_worker_requires_bounded_string_sources(self):
        for value in ({"summary": "x", "files": []}, {"summary": "x", "files": {"x.py": 1}},
                      {"summary": "x", "files": {"x.py": "a" * 131073}}, {"summary": "", "files": {}}):
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValueError):
                validate_work(value)
