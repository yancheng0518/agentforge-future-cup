"""Semantic review gates with scripted model responses; no live execution evidence."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from test_autonomous_chat_integration import (setup_app, PREPARED, REQUEST, GOOD, action, dispatch)
from agentforge.autonomous_model import literal_order_facts, validate_review


PASS = {"passed": True, "issues": [], "summary": "UNIT FAKE semantic agreement"}
FAIL = {"passed": False, "issues": ["UNIT FAKE: expected value contradicts the original request"],
        "summary": "UNIT FAKE conflict; passing tests cannot override the user"}


def test_contract_conflict_regenerates_once_before_freezing_and_preserves_both(tmp_path):
    async def scenario():
        conflicting = deepcopy(PREPARED)
        conflicting["acceptance"] = conflicting["acceptance"].replace(", 5)", ", 99)")
        responses = [conflicting, FAIL, PREPARED, PASS, dispatch("implement", "implement original requirement"),
                     {"summary": "UNIT FAKE implementation", "files": {"tally.py": GOOD}},
                     action("verify"), action("finish"), PASS]
        _, engine, requests, queue = setup_app(tmp_path, responses)
        run = engine.create_run_from_requirement(REQUEST, {"max_tokens": 300000})
        await engine.execute(run["id"])
        result = engine.store.run(run["id"])
        assert result["status"] == "succeeded" and queue == []
        assert result["bundle"]["acceptance"] == PREPARED["acceptance"]
        assert result["intake_proposals"][0]["acceptance"] == conflicting["acceptance"]
        assert [review["passed"] for review in result["contract_reviews"]] == [False, True]
        assert result["quality_review"]["passed"] is True
        assert result["model_metrics"]["model_calls"] == 9
        reparsed = json.loads(requests[2]["messages"][1]["content"])
        assert reparsed["context"]["review_feedback"]["review"]["issues"] == FAIL["issues"]
        assert reparsed["context"]["review_feedback"]["previous_proposal"] == conflicting
        assert reparsed["context"]["review_feedback"]["original_requirement"] == REQUEST
        roles = [json.loads(path.read_text(encoding="utf-8"))["role"]
                 for path in sorted((engine.runtime._run_dir(run["id"]) / "model_requests").glob("*.json"))]
        assert roles[:4] == ["intake", "contract_review", "intake", "contract_review"]
        assert roles[-1] == "final_review"
    asyncio.run(scenario())


def test_two_contract_conflicts_stop_without_freezing_or_running_workers(tmp_path):
    async def scenario():
        _, engine, _, queue = setup_app(tmp_path, [PREPARED, FAIL, PREPARED, FAIL])
        run = engine.create_run_from_requirement(REQUEST, {"max_tokens": 300000})
        await engine.execute(run["id"])
        result = engine.store.run(run["id"])
        assert result["status"] == "needs_human" and queue == []
        assert result["bundle"] is None and result["bundle_sha256"] is None
        assert len(result["contract_reviews"]) == 2 and result["contract_review"]["passed"] is False
        assert result["task_graph"] == [] and result["verifications"] == []
        assert engine.sandbox.bundles == {} and engine.sandbox.apply_calls == []
        assert result["intake"]["status"] == "needs_input"
    asyncio.run(scenario())


def test_test_pass_cannot_override_failed_semantic_review_and_same_hash_is_not_resampled(tmp_path):
    async def scenario():
        responses = [PREPARED, PASS, dispatch("implement", "fulfil request"),
                     {"summary": "UNIT FAKE", "files": {"tally.py": GOOD}}, action("verify"),
                     action("finish"), FAIL, action("finish"), action("stop")]
        _, engine, requests, queue = setup_app(tmp_path, responses)
        run = engine.create_run_from_requirement(REQUEST, {"max_tokens": 300000})
        await engine.execute(run["id"])
        result = engine.store.run(run["id"])
        assert result["status"] == "needs_human" and queue == []
        assert result["verifications"][-1]["status"] == "passed"
        assert result["quality_review"]["passed"] is False
        assert len(result["quality_reviews"]) == 1
        assert sum("current candidate against the ORIGINAL" in request["messages"][0]["content"] for request in requests) == 1
        after_failure = json.loads(requests[7]["messages"][1]["content"])["context"]
        assert after_failure["quality_review"]["issues"] == FAIL["issues"]
    asyncio.run(scenario())


def test_semantic_failure_allows_a_different_repaired_candidate_to_be_reviewed(tmp_path):
    async def scenario():
        revised = GOOD + "# UNIT simulated semantic correction\n"
        responses = [PREPARED, PASS, dispatch("implement", "fulfil request"),
                     {"summary": "UNIT FAKE", "files": {"tally.py": GOOD}}, action("verify"),
                     action("finish"), FAIL, dispatch("semantic-fix", "fix review issues"),
                     {"summary": "UNIT FAKE correction", "files": {"tally.py": revised}},
                     action("verify"), action("finish"), PASS]
        _, engine, _, queue = setup_app(tmp_path, responses)
        def result(passed):
            return {"status": "passed" if passed else "failed", "exit_code": 0 if passed else 1,
                    "isolated": True, "test_count": 3, "stdout": "UNIT FAKE only", "stderr": ""}
        engine.sandbox.test = AsyncMock(side_effect=[result(False), result(True), result(True)])
        run = engine.create_run_from_requirement(REQUEST, {"max_tokens": 300000})
        await engine.execute(run["id"])
        result_run = engine.store.run(run["id"])
        assert result_run["status"] == "succeeded" and queue == []
        assert [review["passed"] for review in result_run["quality_reviews"]] == [False, True]
        assert len({review["candidate_hash"] for review in result_run["quality_reviews"]}) == 2
        assert result_run["quality_review"]["candidate_hash"] == result_run["verified_hash"]
    asyncio.run(scenario())


def test_final_review_budget_stops_third_different_candidate_without_model_request(tmp_path):
    async def scenario():
        _, engine, requests, _ = setup_app(tmp_path, [])
        run = engine.create_run_from_requirement(REQUEST)
        engine.store.update_run(run["id"], quality_reviews=[
            {**FAIL, "candidate_hash": "a"}, {**FAIL, "candidate_hash": "b"}])
        from agentforge.autonomous_model import ModelBudgetExceeded
        with pytest.raises(ModelBudgetExceeded):
            await engine._review_quality(run["id"], "c")
        assert requests == []
    asyncio.run(scenario())


def test_unicode_facts_are_codepoint_order_not_pinyin_or_gbk():
    facts = literal_order_facts("examples = ['餐饮', '交通']\nraise AssertionError('This source must not execute')")
    assert facts["sorted_literals"].index("交通") < facts["sorted_literals"].index("餐饮")
    assert facts["code_points"]["交通"] == [20132, 36890]
    assert facts["code_points"]["餐饮"] == [39184, 39278]


@pytest.mark.parametrize("value", [
    {"passed": True, "issues": ["contradiction"], "summary": "bad approval"},
    {"passed": False, "issues": [], "summary": "no evidence"},
    {"passed": "true", "issues": [], "summary": "not a boolean"},
])
def test_review_contract_rejects_incoherent_approval(value):
    with pytest.raises(ValueError):
        validate_review(value)
