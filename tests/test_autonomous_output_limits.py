"""Vendor payload and truncation regressions; fake transport, never paid calls."""
import asyncio
import json
from unittest.mock import Mock

import pytest

from test_autonomous_intake import environment, proposal, response
from agentforge.autonomous_model import ModelProtocolError


@pytest.mark.parametrize("base,model,expected", [
    ("https://aigw.omniedu.com/v1", "deepseek-v4.1-flash", {"thinking": {"type": "disabled"}}),
    ("https://aigw.omniedu.com/v1/", "deepseek-v4", {"thinking": {"type": "disabled"}}),
    ("https://aigw.omniedu.com/v1", "different-model", {}),
    ("https://example.invalid/v1", "deepseek-v4.1-flash", {}),
    ("https://aigw.omniedu.com.example.invalid/v1", "deepseek-v4.1-flash", {}),
])
def test_provider_option_is_frozen_and_sent_only_for_verified_vendor_model(tmp_path, base, model, expected):
    async def scenario():
        runtime, store, engine = environment(tmp_path)
        runtime.settings.update(base_url=base, model=model)
        run = engine.create_run_from_requirement("计算整数相加", {"max_tokens": 300000, "max_output_tokens": 8192})
        assert run["model_config"]["provider_options"] == expected
        runtime._post = Mock(return_value=response(proposal()))
        await engine.model.prepare(run)
        payload = runtime._post.call_args.args[0]
        assert payload.get("thinking") == expected.get("thinking")
        assert payload["max_tokens"] == 8192
        assert store.run(run["id"])["model_metrics"]["model_calls"] == 1
        record = json.loads(next((runtime._run_dir(run["id"]) / "model_requests").glob("*.json")).read_text(encoding="utf-8"))
        assert record["request"].get("thinking") == expected.get("thinking")
        runtime._command.assert_not_called()
    asyncio.run(scenario())


def test_empty_length_response_is_recorded_as_output_limit_then_bounded_retry(tmp_path):
    async def scenario():
        runtime, store, engine = environment(tmp_path)
        runtime.settings.update(base_url="https://aigw.omniedu.com/v1", model="deepseek-v4.1-flash")
        run = engine.create_run_from_requirement("计算整数相加", {"max_tokens": 300000, "max_output_tokens": 8192})
        truncated = {"choices": [{"message": {"content": "", "reasoning_content": "PRIVATE REASONING MUST NOT BE RETAINED"},
                                  "finish_reason": "length"}], "usage": {"total_tokens": 9000, "completion_tokens": 8192}}
        runtime._post = Mock(side_effect=[truncated, response(proposal(), 500)])
        assert (await engine.model.prepare(run))["status"] == "ready"
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((runtime._run_dir(run["id"]) / "model_requests").glob("*.json"))]
        assert records[0]["protocol_error"]["kind"] == "output_limit"
        assert records[0]["finish_reason"] == "length" and records[0]["content"] == ""
        assert records[1]["format_retry"] is True
        assert "PRIVATE REASONING" not in json.dumps(records)
        metrics = store.run(run["id"])["model_metrics"]
        assert metrics["model_calls"] == 2 and metrics["tokens"] == 9500 and metrics["contract_retries"] == 1
    asyncio.run(scenario())


def test_two_truncated_responses_stop_without_silent_budget_increase(tmp_path):
    async def scenario():
        runtime, store, engine = environment(tmp_path)
        run = engine.create_run_from_requirement("计算整数相加", {"max_tokens": 300000, "max_output_tokens": 8192})
        truncated = response(proposal(), 9000)
        truncated["choices"][0]["finish_reason"] = "length"
        runtime._post = Mock(side_effect=[truncated, truncated])
        with pytest.raises(ModelProtocolError, match="输出上限"):
            await engine.model.prepare(run)
        assert runtime._post.call_count == 2
        assert [call.args[0]["max_tokens"] for call in runtime._post.call_args_list] == [8192, 8192]
        metrics = store.run(run["id"])["model_metrics"]
        assert metrics["model_calls"] == 2 and metrics["tokens"] == 18000 and metrics["contract_retries"] == 1
        assert store.run(run["id"])["bundle"] is None
    asyncio.run(scenario())
