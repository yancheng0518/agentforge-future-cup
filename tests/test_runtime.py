"""Platform unit tests only: no LLM network traffic or candidate execution.

Every test uses a temporary project and fake model/container transports. Passing
these tests is not evidence of a real model call or a real isolated acceptance run.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "backend"))
from agentforge.runtime import Runtime, RuntimeErrorSafe, _settings  # noqa: E402


def model_response(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"message": message}], "usage": {"total_tokens": 7}}


def tool_call(name, arguments="{}", call_id="test-call"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentforge-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        for name in ("examples", "knowledge", "acceptance"):
            shutil.copytree(WORKSPACE / name, self.root / name)
        self.settings = {
            "api_key": "unit-test-secret-never-valid",
            "key_source": "unit_test",
            "base_url": "https://example.invalid/v1",
            "model": "fake-model",
            "timeout": 5,
            "image": "agentforge-unit-test:never-run",
        }
        with patch("agentforge.runtime._settings", return_value=self.settings):
            self.runtime = Runtime(self.root, self.root / "data")
        # Any accidental outbound request fails without reaching the network.
        self.network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Unit tests must not access the network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.source = (self.root / "examples/todo/todo.py").read_text(encoding="utf-8")
        self.acceptance_hash = hashlib.sha256((self.root / "acceptance/test_todo.py").read_bytes()).hexdigest()
        self.reviewer = {"role": "reviewer", "model": "inherit", "tools": ["read_project", "knowledge_search"]}

    async def test_run_identifiers_cannot_escape_workspace(self):
        for run_id in ("../outside", "..", "a/b", "a\\b", "C:\\outside", "", "a" * 81):
            with self.subTest(run_id=run_id), self.assertRaises(RuntimeErrorSafe):
                await self.runtime.prepare(run_id)

    async def test_prepare_copies_target_and_is_idempotent(self):
        manifest = await self.runtime.prepare("run-1")
        candidate = Path(manifest["workspace"]) / "todo.py"
        self.assertEqual(candidate.read_text(encoding="utf-8"), self.source)
        self.assertEqual(manifest["base_hash"], hashlib.sha256(self.source.encode()).hexdigest())
        candidate.write_text("# candidate edit\n", encoding="utf-8")
        self.assertEqual(await self.runtime.prepare("run-1"), manifest)
        self.assertEqual(candidate.read_text(encoding="utf-8"), "# candidate edit\n")

    async def test_candidate_patch_is_allowlisted_and_acceptance_unchanged(self):
        await self.runtime.prepare("run-1")
        for name in ("../todo.py", "acceptance/test_todo.py", "../acceptance/test_todo.py", "/tmp/todo.py", "todo.py/../x"):
            with self.subTest(path=name), self.assertRaises(RuntimeErrorSafe):
                await self.runtime.apply_candidate("run-1", [{"path": name, "content": "changed"}])
        with self.assertRaises(RuntimeErrorSafe):
            await self.runtime.apply_candidate("run-1", [{"path": "todo.py", "content": "x"}, {"path": "test.py", "content": "x"}])
        self.assertEqual(hashlib.sha256((self.root / "acceptance/test_todo.py").read_bytes()).hexdigest(), self.acceptance_hash)
        self.assertEqual((self.root / "examples/todo/todo.py").read_text(encoding="utf-8"), self.source)

    async def test_candidate_size_limit(self):
        await self.runtime.prepare("run-1")
        with self.assertRaises(RuntimeErrorSafe):
            await self.runtime.apply_candidate("run-1", [{"path": "todo.py", "content": "中" * 30000}])

    async def test_diff_is_real_and_candidate_is_never_executed(self):
        await self.runtime.prepare("run-1")
        sentinel = self.root / "SHOULD_NOT_EXIST"
        source = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n"
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("Host subprocess prohibited")):
            result = await self.runtime.apply_candidate("run-1", [{"path": "todo.py", "content": source}])
            read = await self.runtime.read_project("run-1")
        self.assertFalse(sentinel.exists())
        self.assertIn("--- a/todo.py", result["unified_diff"])
        self.assertIn("+++ b/todo.py", result["unified_diff"])
        self.assertEqual(read["files"][0]["content"], source)
        self.assertEqual((self.root / "examples/todo/todo.py").read_text(encoding="utf-8"), self.source)

    async def test_missing_docker_blocks_instead_of_host_fallback(self):
        await self.runtime.prepare("run-1")
        with patch("agentforge.runtime.shutil.which", return_value=None), patch.object(self.runtime, "_command", new_callable=AsyncMock) as command:
            health = await self.runtime.health()
            result = await self.runtime.test("run-1")
        self.assertFalse(health["sandbox"]["available"])
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["exit_code"])
        self.assertFalse(result["isolated"])
        command.assert_not_awaited()

    async def test_fake_container_command_keeps_acceptance_readonly_and_no_network(self):
        await self.runtime.prepare("run-1")
        with patch.object(self.runtime, "health", AsyncMock(return_value={"sandbox": {"available": True}})), patch.object(self.runtime, "_command", AsyncMock(side_effect=[(0, 'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": true}\n', ""), (0, "", "")])) as command:
            result = await self.runtime.test("run-1")
        args = command.await_args_list[0].args[0]
        self.assertEqual(args[:2], ["docker", "run"])
        self.assertEqual(args[args.index("--network") + 1], "none")
        for flag in ("--read-only", "--cap-drop", "--security-opt", "--memory", "--pids-limit", "--user"):
            self.assertIn(flag, args)
        mounts = [args[i + 1] for i, item in enumerate(args) if item == "--mount"]
        self.assertTrue(any("target=/candidate,readonly" in item for item in mounts))
        self.assertTrue(any("target=/acceptance,readonly" in item for item in mounts))
        self.assertEqual(args[-4:], ["python", "-I", "-B", "/acceptance/runner.py"])
        self.assertEqual(command.await_args_list[1].args[0][:3], ["docker", "rm", "-f"])
        self.assertEqual(result["status"], "passed")  # Mock response, never a real execution claim.
        self.assertEqual(result["acceptance_sha256"], self.acceptance_hash)

    async def test_zero_exit_without_complete_acceptance_is_failure(self):
        await self.runtime.prepare("run-1")
        with patch.object(self.runtime, "health", AsyncMock(return_value={"sandbox": {"available": True}})), patch.object(self.runtime, "_command", AsyncMock(side_effect=[(0, "all good", ""), (0, "", "")])):
            result = await self.runtime.test("run-1")
        self.assertEqual(result["status"], "failed")

    async def test_container_launch_failure_is_explicitly_blocked(self):
        await self.runtime.prepare("run-1")
        with patch.object(self.runtime, "health", AsyncMock(return_value={"sandbox": {"available": True}})), patch.object(self.runtime, "_command", AsyncMock(side_effect=[(125, "", "cannot launch"), (0, "", "")])):
            result = await self.runtime.test("run-1")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["isolated"])

    async def test_container_timeout_attempts_cleanup(self):
        await self.runtime.prepare("run-1")
        with patch.object(self.runtime, "health", AsyncMock(return_value={"sandbox": {"available": True}})), patch.object(self.runtime, "_command", AsyncMock(side_effect=[TimeoutError(), (0, "", "")])) as command:
            result = await self.runtime.test("run-1")
        self.assertEqual(result["status"], "failed")
        self.assertIn("25", result["reason"])
        self.assertEqual(command.await_args_list[-1].args[0][:3], ["docker", "rm", "-f"])

    async def test_mock_model_is_labeled_and_makes_no_model_calls(self):
        with patch.object(self.runtime, "_post", side_effect=AssertionError("Mock must not call model")):
            first = await self.runtime.invoke({"role": "developer"}, {"mode": "mock"}, {})
            fixed = await self.runtime.invoke({"role": "fixer"}, {"mode": "mock"}, {"attempt": 1})
        self.assertEqual(first["model_mode"], "mock")
        self.assertEqual(first["model_calls"], 0)
        self.assertTrue(first["output"]["preset_fault"])
        self.assertFalse(fixed["output"]["preset_fault"])

    async def test_fake_llm_tool_round_trip_records_real_context_and_usage(self):
        await self.runtime.prepare("run-1")
        project = await self.runtime.read_project("run-1")
        outputs = [model_response(calls=[tool_call("read_project")]), model_response(json.dumps({"summary": "review", "passed": False, "issues": ["missing completed"]}))]
        with patch.object(self.runtime, "_post", side_effect=outputs) as post:
            result = await self.runtime.invoke(self.reviewer, {"mode": "live"}, {"project": project})
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["tokens"], 14)
        self.assertEqual(result["tool_calls"][0]["output"], project)
        second = post.call_args_list[1].args[0]
        self.assertEqual(second["messages"][-1]["role"], "tool")
        self.assertEqual(second["model"], "fake-model")

    async def test_failed_model_request_keeps_attempt_and_marks_usage_unknown(self):
        failures = [RuntimeErrorSafe("模型 HTTP 403"), TimeoutError("simulated timeout")]
        for failure in failures:
            with self.subTest(error=type(failure).__name__), patch.object(self.runtime, "_post", side_effect=failure):
                with self.assertRaises(RuntimeErrorSafe) as caught:
                    await self.runtime.invoke(self.reviewer, {"mode": "live"}, {})
            self.assertEqual(caught.exception.model_calls, 1)
            self.assertEqual(caught.exception.tokens, 0)
            self.assertFalse(caught.exception.metrics_complete)

    async def test_later_request_failure_preserves_known_usage_and_tool_trace(self):
        responses = [model_response(calls=[tool_call("read_project")]), TimeoutError("simulated timeout")]
        with patch.object(self.runtime, "_post", side_effect=responses):
            with self.assertRaises(RuntimeErrorSafe) as caught:
                await self.runtime.invoke(self.reviewer, {"mode": "live"}, {"project": {"files": []}})
        self.assertEqual(caught.exception.model_calls, 2)
        self.assertEqual(caught.exception.tokens, 7)
        self.assertFalse(caught.exception.metrics_complete)
        self.assertEqual(caught.exception.tool_calls[0]["name"], "read_project")

    async def test_missing_or_invalid_usage_is_incomplete_even_on_success(self):
        content = json.dumps({"summary": "review", "passed": True, "issues": []})
        for usage in (None, {}, {"total_tokens": "7"}, {"total_tokens": -1}, {"total_tokens": True}):
            response = model_response(content)
            response["usage"] = usage
            with self.subTest(usage=usage), patch.object(self.runtime, "_post", return_value=response):
                result = await self.runtime.invoke(self.reviewer, {"mode": "live"}, {})
            self.assertEqual(result["model_calls"], 1)
            self.assertEqual(result["tokens"], 0)
            self.assertFalse(result["metrics_complete"])

    async def test_later_valid_usage_does_not_erase_an_earlier_unknown_round(self):
        first = model_response(calls=[tool_call("read_project")])
        first.pop("usage")
        last = model_response(json.dumps({"summary": "review", "passed": True, "issues": []}))
        with patch.object(self.runtime, "_post", side_effect=[first, last]):
            result = await self.runtime.invoke(self.reviewer, {"mode": "live"}, {})
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["tokens"], 7)
        self.assertFalse(result["metrics_complete"])

    async def test_static_and_final_review_receive_distinct_evidence_duties(self):
        response = model_response(json.dumps({"summary": "review", "passed": False, "issues": []}))
        context = {"project": {"files": []}, "validation_scope": {"id": "round-0/verify"}}
        prompts = {}
        for kind in ("inspect", "review"):
            with patch.object(self.runtime, "_post", return_value=response) as post:
                await self.runtime.invoke(self.reviewer, {"mode": "live", "kind": kind}, context)
            messages = post.call_args.args[0]["messages"]
            prompts[kind] = messages[0]["content"]
            request = json.loads(messages[1]["content"])
            self.assertEqual(request["task"]["kind"], kind)
            self.assertNotIn("test", request["context"], "Prompt construction must not invent test evidence")
        self.assertIn("静态初审 inspect", prompts["inspect"])
        self.assertIn("尚未返回的测试结果不构成代码缺陷", prompts["inspect"])
        self.assertIn("最终汇总 review", prompts["review"])
        self.assertIn("status=passed、isolated=true、exit_code=0", prompts["review"])
        self.assertIn("测试未完成或失败时必须返回 passed=false", prompts["review"])

    async def test_model_cannot_request_shell_or_file_write_tool(self):
        for name in ("run_shell", "write_file", "delete_project"):
            with self.subTest(tool=name), patch.object(self.runtime, "_post", return_value=model_response(calls=[tool_call(name)])), self.assertRaises(RuntimeErrorSafe):
                await self.runtime.invoke(self.reviewer, {"mode": "live"}, {})

    async def test_tool_parameters_must_match_schema(self):
        cases = [("read_project", '{"path":"../../secret"}'), ("knowledge_search", "[]"), ("knowledge_search", '{"query":1}'), ("knowledge_search", '{"query":"todo","path":"secret"}')]
        for name, arguments in cases:
            with self.subTest(tool=name, arguments=arguments), patch.object(self.runtime, "_post", return_value=model_response(calls=[tool_call(name, arguments)])) as post:
                with self.assertRaises(RuntimeErrorSafe):
                    await self.runtime.invoke(self.reviewer, {"mode": "live"}, {})
                self.assertEqual(post.call_count, 1, "Invalid tools must fail immediately, not after exhausting the model budget")

    async def test_tool_call_budget_is_bounded(self):
        with patch.object(self.runtime, "_post", return_value=model_response(calls=[tool_call("read_project")])) as post:
            with self.assertRaises(RuntimeErrorSafe):
                await self.runtime.invoke(self.reviewer, {"mode": "live"}, {})
        self.assertEqual(post.call_count, 3, "Tool requests after forced final output must not execute")

    async def test_agent_tool_permissions_are_enforced(self):
        with patch.object(self.runtime, "_post", return_value=model_response(calls=[tool_call("read_project")])) as post:
            with self.assertRaises(RuntimeErrorSafe):
                await self.runtime.invoke({"role": "reviewer", "tools": []}, {"mode": "live"}, {})
        payload = post.call_args.args[0]
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertEqual(post.call_count, 1)

    async def test_settings_do_not_guess_provider_from_unrelated_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unrelated-key", "DEEPSEEK_MODEL": "unrelated-model"}, clear=True), patch("agentforge.runtime._user_env", side_effect=AssertionError("Do not read unrelated user credentials")):
            settings = _settings(self.root)
        self.assertEqual(settings["api_key"], "")
        self.assertEqual(settings["model"], "")
        self.assertEqual(settings["base_url"], "https://aigw.omniedu.com/v1")
        self.assertEqual(settings["key_source"], "unconfigured")

    async def test_project_settings_override_defaults_without_exposing_key(self):
        (self.root / ".env").write_text("AGENTFORGE_LLM_API_KEY=unit-test-key\nAGENTFORGE_LLM_MODEL=enabled-model\nAGENTFORGE_LLM_BASE_URL=https://example.invalid/v1/\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            settings = _settings(self.root)
        self.assertEqual(settings["model"], "enabled-model")
        self.assertEqual(settings["base_url"], "https://example.invalid/v1")
        self.assertEqual(settings["api_key"], "unit-test-key")

    async def test_invalid_model_json_and_role_contract_are_rejected(self):
        for content in ("not json", "[]", '{"passed":"yes","issues":[]}'):
            with self.subTest(content=content), patch.object(self.runtime, "_post", return_value=model_response(content)), self.assertRaises(RuntimeErrorSafe):
                await self.runtime.invoke({"role": "reviewer"}, {"mode": "live"}, {})

    async def test_http_errors_never_expose_provider_body_or_key(self):
        secret = self.settings["api_key"]
        for status, hint in ((401, "权限"), (403, "权限"), (429, "限流"), (503, "服务渠道")):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("https://example.invalid", status, "secret " + secret, {}, io.BytesIO(secret.encode()))
                opener = Mock()
                opener.open.side_effect = error
                with patch("urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(RuntimeErrorSafe) as caught:
                        self.runtime._post({"messages": []})
                self.assertIn(str(status), str(caught.exception))
                self.assertIn(hint, str(caught.exception))
                self.assertNotIn(secret, str(caught.exception))

    async def test_model_http_and_missing_key_fail_before_network(self):
        self.settings["base_url"] = "http://example.invalid/v1"
        with self.assertRaises(RuntimeErrorSafe):
            self.runtime._post({})
        self.settings["api_key"] = ""
        with self.assertRaises(RuntimeErrorSafe):
            self.runtime._post({})

    async def test_apply_refuses_baseline_conflict(self):
        await self.runtime.prepare("run-1")
        await self.runtime.apply_candidate("run-1", [{"path": "todo.py", "content": "# candidate\n"}])
        target = self.root / "examples/todo/todo.py"
        target.write_text("# user edit\n", encoding="utf-8")
        with self.assertRaises(RuntimeErrorSafe):
            await self.runtime.apply("run-1")
        self.assertEqual(target.read_text(encoding="utf-8"), "# user edit\n")

    async def test_apply_updates_only_target_after_explicit_caller_action(self):
        await self.runtime.prepare("run-1")
        await self.runtime.apply_candidate("run-1", [{"path": "todo.py", "content": "# candidate\n"}])
        result = await self.runtime.apply("run-1")
        self.assertTrue(result["applied"])
        self.assertEqual((self.root / "examples/todo/todo.py").read_text(encoding="utf-8"), "# candidate\n")
        self.assertEqual(hashlib.sha256((self.root / "acceptance/test_todo.py").read_bytes()).hexdigest(), self.acceptance_hash)

    async def test_apply_rejects_candidate_symlink(self):
        await self.runtime.prepare("run-1")
        candidate = self.root / "data/runs/run-1/candidate/todo.py"
        original = Path.is_symlink
        # A virtual symlink avoids depending on Windows Developer Mode/admin rights.
        with patch.object(Path, "is_symlink", lambda path: path == candidate or original(path)):
            with self.assertRaises(RuntimeErrorSafe):
                await self.runtime.apply("run-1")

    async def test_knowledge_retrieval_has_traceable_file_paths(self):
        result = await self.runtime.retrieve("completed 筛选")
        self.assertEqual(result["method"], "local_keyword")
        self.assertTrue(result["matches"])
        for match in result["matches"]:
            self.assertTrue(match["path"].startswith("knowledge/"))
            self.assertTrue((self.root / match["path"]).is_file())
            self.assertLessEqual(len(match["excerpt"]), 5000)


if __name__ == "__main__":
    unittest.main()
