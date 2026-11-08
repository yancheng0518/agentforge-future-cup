"""Contract-retry unit tests with stub HTTP; no model or candidate execution.

Request bodies are copied at dispatch time because Runtime appends to its
message list. Temporary SQLite is reopened to verify persisted failure evidence.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.engine import Engine
from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.storage import Store


def response(content=None, *, calls=None, usage=7, finish_reason="stop"):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": finish_reason}], "usage": {"total_tokens": usage}}


def tool_call(number, name="read_project"):
    return {"id": "unit-call-" + str(number), "type": "function",
            "function": {"name": name, "arguments": "{}" if name == "read_project" else '{"query":"todo"}'}}


def review(passed=True):
    return json.dumps({"summary": "Unit transport response", "passed": passed, "issues": []})


class SequenceTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, payload):
        self.requests.append(deepcopy(payload))
        if not self.replies:
            raise AssertionError("Unexpected additional HTTP request beyond the declared response sequence")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return deepcopy(reply)


class ModelContractRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentforge-contract-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.secret = "unit-test-secret-contract-never-valid"
        self.settings = {"api_key": self.secret, "base_url": "https://example.invalid/v1", "model": "unit-model",
                         "timeout": 5, "min_request_interval": 0, "image": "unit-only-never-run"}
        with patch("agentforge.runtime._settings", return_value=self.settings):
            self.runtime = Runtime(self.root, self.root / "data")
        self.agent = {"id": "unit-reviewer", "version": 1, "role": "reviewer", "model": "inherit",
                      "tools": ["read_project", "knowledge_search"]}
        self.task = {"mode": "live", "kind": "review", "workflow_id": "development", "requirement": "Unit contract test"}
        self.context = {"project": {"files": [{"path": "todo.py", "content": "# text only; never executed\n"}]}}
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Contract unit tests prohibit network access"))
        network.start()
        self.addCleanup(network.stop)
        subprocess = patch("asyncio.create_subprocess_exec", side_effect=AssertionError("Contract unit tests prohibit host/container processes"))
        subprocess.start()
        self.addCleanup(subprocess.stop)

    async def invoke(self, replies, agent=None):
        transport = SequenceTransport(replies)
        with patch.object(self.runtime, "_post", side_effect=transport):
            result = await self.runtime.invoke(agent or self.agent, self.task, deepcopy(self.context))
        return result, transport.requests

    async def invoke_failure(self, replies, agent=None):
        transport = SequenceTransport(replies)
        with patch.object(self.runtime, "_post", side_effect=transport), self.assertRaises(RuntimeErrorSafe) as caught:
            await self.runtime.invoke(agent or self.agent, self.task, deepcopy(self.context))
        return caught.exception, transport.requests

    async def test_invalid_json_then_valid_preserves_diagnostic_usage_and_forbids_retry_tools(self):
        invalid = '{"summary":"unfinished"'
        result, requests = await self.invoke([response(invalid, usage=7), response(review(), usage=11)])
        self.assertEqual(result["output"]["passed"], True)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["contract_retries"], 1)
        self.assertEqual(result["tokens"], 18)
        self.assertTrue(result["metrics_complete"])
        self.assertEqual(len(result["model_diagnostics"]), 1)
        diagnostic = result["model_diagnostics"][0]
        self.assertEqual(diagnostic["error"]["kind"], "json_syntax")
        self.assertEqual(diagnostic["content"], invalid)
        self.assertEqual(diagnostic["content_sha256"], hashlib.sha256(invalid.encode()).hexdigest())
        self.assertTrue(diagnostic["retry_scheduled"])
        self.assertEqual(requests[0]["tool_choice"], "auto")
        self.assertEqual(requests[1]["tool_choice"], "none")
        self.assertEqual(len(requests[0]["messages"]), 2)
        self.assertEqual(requests[1]["messages"][-2], {"role": "assistant", "content": invalid})
        self.assertEqual(requests[1]["messages"][-1]["role"], "user")
        self.assertEqual(result["tool_calls"], [])

    async def test_reviewer_passed_requires_boolean_and_is_never_coerced(self):
        for invalid_passed in ("true", "false", "yes", 1, 0, None):
            with self.subTest(passed=invalid_passed):
                result, requests = await self.invoke([response(review(invalid_passed)), response(review(False))])
                self.assertEqual(len(requests), 2)
                self.assertIs(result["output"]["passed"], False)
                self.assertEqual(result["model_diagnostics"][0]["error"]["kind"], "schema")
                self.assertEqual(result["contract_retries"], 1)
        for valid_passed in (False, True):
            with self.subTest(valid_passed=valid_passed):
                result, requests = await self.invoke([response(review(valid_passed))])
                self.assertEqual(len(requests), 1)
                self.assertIs(result["output"]["passed"], valid_passed)
                self.assertEqual(result["contract_retries"], 0)

    async def test_repeated_invalid_json_or_schema_uses_at_most_two_http_requests(self):
        for content in ("not JSON", "[]", review("false"), '{"passed":true,"issues":"wrong"}'):
            with self.subTest(content=content):
                error, requests = await self.invoke_failure([response(content), response(content)])
                self.assertEqual(len(requests), 2)
                self.assertEqual(error.model_calls, 2)
                self.assertEqual(error.contract_retries, 1)
                self.assertEqual(error.tokens, 14)
                self.assertTrue(error.metrics_complete)
                self.assertEqual([d["request_number"] for d in error.model_diagnostics], [1, 2])
                self.assertEqual([d["retry_scheduled"] for d in error.model_diagnostics], [True, False])

    async def test_two_tool_requests_then_invalid_json_then_valid_stays_within_four_requests(self):
        replies = [response(calls=[tool_call(1)]), response(calls=[tool_call(2)]), response("invalid JSON"), response(review())]
        result, requests = await self.invoke(replies)
        self.assertEqual(len(requests), 4)
        self.assertEqual([r["tool_choice"] for r in requests], ["auto", "auto", "none", "none"])
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual(result["contract_retries"], 1)
        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertEqual(result["model_diagnostics"][0]["request_number"], 3)
        self.assertEqual(result["tokens"], 28)
        self.assertEqual(self.runtime.model_config()["automatic_tool_rounds"], 2)

    async def test_exhausted_four_request_budget_never_adds_another_contract_retry(self):
        error, requests = await self.invoke_failure([response(calls=[tool_call(1)]), response(calls=[tool_call(2)]),
                                                     response("invalid first"), response("invalid second")])
        self.assertEqual(len(requests), 4)
        self.assertEqual(error.model_calls, 4)
        self.assertEqual(error.contract_retries, 1)
        self.assertEqual(len(error.tool_calls), 2)
        self.assertEqual([d["request_number"] for d in error.model_diagnostics], [3, 4])
        self.assertEqual([d["retry_scheduled"] for d in error.model_diagnostics], [True, False])

    async def test_third_request_ignoring_tool_choice_none_is_rejected_without_execution_or_retry(self):
        replies = [response(calls=[tool_call(1)]), response(calls=[tool_call(2)]),
                   response(calls=[tool_call(3, "knowledge_search")])]
        with patch.object(self.runtime, "retrieve", new=AsyncMock(return_value={"must": "not execute"})) as retrieve:
            error, requests = await self.invoke_failure(replies)
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[-1]["tool_choice"], "none")
        retrieve.assert_not_awaited()
        self.assertEqual(error.model_calls, 3)
        self.assertEqual(error.contract_retries, 0)
        self.assertEqual(len(error.tool_calls), 2)
        self.assertEqual(error.model_diagnostics, [])

    async def test_tool_call_during_format_retry_is_rejected_without_execution(self):
        with patch.object(self.runtime, "retrieve", new=AsyncMock(return_value={"must": "not execute"})) as retrieve:
            error, requests = await self.invoke_failure([response("bad JSON"), response(calls=[tool_call(2, "knowledge_search")])])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1]["tool_choice"], "none")
        retrieve.assert_not_awaited()
        self.assertEqual(error.contract_retries, 1)
        self.assertEqual(len(error.model_diagnostics), 1)
        self.assertEqual(error.tool_calls, [])

    async def test_diagnostics_and_retry_prompt_redact_secret_and_exclude_reasoning_and_extra_usage(self):
        raw = json.dumps({"summary": self.secret, "passed": "true", "issues": []})
        bad = response(raw, finish_reason="stop-" + self.secret)
        bad["choices"][0]["message"]["reasoning_content"] = "PRIVATE_REASONING_MARKER " + self.secret
        bad["usage"].update(api_key=self.secret, provider_debug="PRIVATE_USAGE_MARKER", prompt_tokens=3, completion_tokens=4)
        result, requests = await self.invoke([bad, response(review())])
        diagnostic = result["model_diagnostics"][0]
        serialized = json.dumps(result["model_diagnostics"], ensure_ascii=False)
        self.assertNotIn(self.secret, serialized)
        self.assertNotIn("PRIVATE_REASONING_MARKER", serialized)
        self.assertNotIn("PRIVATE_USAGE_MARKER", serialized)
        self.assertNotIn(self.secret, json.dumps(requests[-1], ensure_ascii=False))
        self.assertTrue(diagnostic["redacted"])
        self.assertEqual(diagnostic["content"], raw.replace(self.secret, "[REDACTED]"))
        self.assertEqual(diagnostic["finish_reason"], "stop-[REDACTED]")
        self.assertEqual(diagnostic["content_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(diagnostic["content_characters"], len(raw))
        self.assertEqual(set(diagnostic["usage"]), {"total_tokens", "prompt_tokens", "completion_tokens"})

    async def test_unknown_usage_remains_unknown_after_successful_contract_retry(self):
        for usage in (None, {}, {"total_tokens": "7"}, {"total_tokens": True}, {"total_tokens": -1}):
            with self.subTest(usage=usage):
                first = response("invalid JSON")
                first["usage"] = usage
                result, _ = await self.invoke([first, response(review(), usage=11)])
                self.assertFalse(result["metrics_complete"])
                self.assertEqual(result["tokens"], 11)
                self.assertEqual(result["model_calls"], 2)
                self.assertEqual(result["contract_retries"], 1)
        last = response(review())
        last.pop("usage")
        result, _ = await self.invoke([response("invalid JSON", usage=7), last])
        self.assertFalse(result["metrics_complete"])
        self.assertEqual(result["tokens"], 7)

    async def test_retry_http_failure_keeps_prior_diagnostic_and_known_usage(self):
        for failure in (RuntimeErrorSafe("Unit provider HTTP 503"), TimeoutError("Unit timeout")):
            with self.subTest(failure=type(failure).__name__):
                error, requests = await self.invoke_failure([response("invalid JSON", usage=7), failure])
                self.assertEqual(len(requests), 2)
                self.assertEqual(error.model_calls, 2)
                self.assertEqual(error.tokens, 7)
                self.assertFalse(error.metrics_complete)
                self.assertEqual(error.contract_retries, 1)
                self.assertEqual(len(error.model_diagnostics), 1)
                self.assertEqual(error.model_diagnostics[0]["content"], "invalid JSON")
                self.assertTrue(error.model_diagnostics[0]["retry_scheduled"])

    async def test_engine_persists_retry_diagnostics_on_success_and_exception(self):
        for outcome in ("success", "schema_failure", "http_failure"):
            with self.subTest(outcome=outcome):
                store_path = self.root / (outcome + ".sqlite3")
                store = Store(store_path)
                engine = Engine(store, self.runtime)
                # Only exercise actual Engine invocation persistence. No prepare,
                # candidate writes, test runner, or workflow-success claim here.
                with patch.object(self.runtime, "snapshot_task", return_value=None):
                    run = engine.create_run("Unit contract evidence", mode="live")
                second = response(review(), usage=11) if outcome == "success" else response("invalid again", usage=11) if outcome == "schema_failure" else RuntimeErrorSafe("Unit HTTP 503")
                transport = SequenceTransport([response("invalid JSON", usage=7), second])
                with patch.object(self.runtime, "_post", side_effect=transport):
                    if outcome == "success":
                        result = await engine._invoke(run, {"id": "review"}, "round-0/verify/review", self.agent, {})
                    else:
                        with self.assertRaises(RuntimeErrorSafe) as caught:
                            await engine._invoke(run, {"id": "review"}, "round-0/verify/review", self.agent, {})
                reopened = Store(store_path)
                saved = reopened.run(run["id"])
                event = next(e for e in reopened.events(run["id"]) if e["type"] == "agent")
                self.assertEqual(event["data"]["model_calls"], 2)
                self.assertEqual(event["data"]["contract_retries"], 1)
                self.assertEqual(saved["model_calls"], 2)
                self.assertEqual(saved["model_tokens"], 7 if outcome == "http_failure" else 18)
                self.assertEqual(saved["model_metrics_complete"], outcome != "http_failure")
                self.assertEqual(saved["agent_invocations"], 1)
                if outcome == "success":
                    self.assertEqual(event["data"]["model_diagnostics"], result["model_diagnostics"])
                else:
                    self.assertEqual(event["data"]["status"], "failed")
                    self.assertEqual(event["data"]["model_diagnostics"], caught.exception.model_diagnostics)
                    self.assertEqual(event["data"]["metrics_complete"], caught.exception.metrics_complete)


if __name__ == "__main__":
    unittest.main()
