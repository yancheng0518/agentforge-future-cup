"""Review request/policy regressions only; fake transport does not prove model judgement."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import Mock

from test_autonomous_intake import environment, proposal, response
from agentforge.autonomous_model import CONTRACT_REVIEW_PROMPT, FINAL_REVIEW_PROMPT, INTAKE_PROMPT


def test_revision_has_original_request_and_previous_proposal_without_changing_budget(tmp_path):
    async def scenario():
        runtime, store, engine = environment(tmp_path)
        runtime.settings.update(base_url="https://aigw.omniedu.com/v1", model="deepseek-v4.1-flash")
        original = "实现整数相加，不要求校验 JSON 之外的输入类型。"
        run = engine.create_run_from_requirement(original, {"max_calls": 40, "max_tokens": 300000,
                                                            "max_output_tokens": 8192})
        before = deepcopy(proposal())
        feedback = {"original_requirement": original, "previous_proposal": before,
                    "review": {"passed": False, "issues": ["UNIT fake concrete conflict"], "summary": "UNIT simulated blocking issue"}}
        runtime._post = Mock(return_value=response(proposal()))
        await engine.model.prepare(run, feedback=feedback)
        sent = runtime._post.call_args.args[0]
        context = json.loads(sent["messages"][1]["content"])
        assert context["requirement"] == original
        assert context["context"]["review_feedback"] == feedback
        assert sent["max_tokens"] == 8192 and sent["thinking"] == {"type": "disabled"}
        assert store.run(run["id"])["budget"] == run["budget"]
        assert run["model_config"]["adapter_revision"] == "autonomous-7-web-projects"
        assert feedback["previous_proposal"] == before
        assert "MINIMAL, internally consistent assumptions" in sent["messages"][0]["content"]
        assert "3 to 12 focused" in sent["messages"][0]["content"]
    asyncio.run(scenario())


def test_contract_review_keeps_core_correctness_checks_but_excludes_optional_wishlist(tmp_path):
    async def scenario():
        runtime, _, engine = environment(tmp_path)
        run = engine.create_run_from_requirement("金额结果准确，组合接口与单独函数一致，字符串按 Unicode 升序。")
        prepared = proposal()
        prepared["acceptance"] += "\n# Literal comparison sample: '交通', '餐饮'\n"
        runtime._post = Mock(return_value=response({"passed": True, "issues": [], "summary": "UNIT fake limited approval"}))
        await engine.model.review_contract(run, prepared)
        sent = runtime._post.call_args.args[0]
        context = json.loads(sent["messages"][1]["content"])["context"]
        assert context["original_requirement"] == run["requirement"]
        assert context["proposed_contract"] == prepared
        prompt = sent["messages"][0]["content"]
        assert "ONLY blocking issues" in prompt
        assert "Never put correct/OK observations" in prompt
        assert "bytes, arbitrary Python objects" in prompt
        assert "wrong numeric totals/rounding" in prompt and "component and composed" in prompt
        assert "passed=true and issues=[]" in prompt
        runtime._command.assert_not_called()
    asyncio.run(scenario())


def test_final_review_preserves_required_semantics_and_source_level_mutation_check():
    assert "original-requirement violations" in FINAL_REVIEW_PROMPT
    assert "Verify required immutability from the source" in FINAL_REVIEW_PROMPT
    assert "actual sorting, composition, validation or monetary correctness" in FINAL_REVIEW_PROMPT
    assert "MINIMAL, internally consistent assumptions" in INTAKE_PROMPT
    assert "not invent additional requirements" in INTAKE_PROMPT
    assert "central mandatory behavior missing" in CONTRACT_REVIEW_PROMPT
