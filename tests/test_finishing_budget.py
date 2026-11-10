"""Host budget scheduling using fake transport/text-only sandbox; no live execution."""
import asyncio
from copy import deepcopy
import json
import threading
from unittest.mock import Mock

import pytest

from agentforge.autonomous import DecisionRejected, budget_config
from agentforge.autonomous_model import ModelBudgetExceeded
from agentforge.finishing_budget import FinishingBudgetReached, freeze_policy
from test_autonomous_chat_integration import (
    setup_app, REQUEST, GOOD, BAD, ACCEPTANCE, PREPARED, REVIEW_OK, action, dispatch,
)


def ready_run(tmp_path, replies=(), *, source=GOOD, budget=None):
    _, engine, requests, queue = setup_app(tmp_path, list(replies))
    run = engine.create_run({"requirement": REQUEST, "files": {"tally.py": source},
                             "acceptance": ACCEPTANCE,
                             "provenance": {"kind": "natural", "description": "UNIT FAKE ONLY"}}, budget)
    engine.store.update_run(run["id"], intake={"status": "completed", "specification": REQUEST},
                           quality_reviews=[], quality_review=None, candidate_origin="natural_candidate")
    return engine, engine.store.run(run["id"]), requests, queue


def spend(engine, run, calls=38, tokens=100):
    engine.store.update_run(run["id"], model_metrics={
        "model_calls": calls, "tokens": tokens, "budget_tokens": tokens,
        "in_flight": 0, "reserved_tokens": 0, "unknown_usage_calls": 0,
        "metrics_complete": True, "contract_retries": 0,
    })


def test_calls_reserve_completes_through_docker_and_review_with_retry(tmp_path):
    engine, run, requests, queue = ready_run(tmp_path, ["invalid JSON", REVIEW_OK])
    spend(engine, run)
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "succeeded" and not queue
    assert len(engine.sandbox.test_results) == 1
    assert final["quality_review"]["passed"]
    assert final["quality_review"]["candidate_hash"] == final["verified_hash"]
    assert final["model_metrics"]["model_calls"] == 40
    assert final["model_metrics"]["contract_retries"] == 1
    assert len(requests) == 2 and final["controller_decisions"] == []
    assert final["finishing_state"]["phase"] == "finalizing"
    assert [event["type"] for event in engine.store.events(run["id"])].count("budget_finalized") == 1


def test_failed_verification_preserves_candidate_without_spending_review(tmp_path):
    engine, run, requests, _ = ready_run(tmp_path, source=BAD)
    spend(engine, run)
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "needs_human" and final["verified_hash"] is None
    assert final["verifications"][-1]["status"] == "failed"
    assert engine.sandbox.files[run["id"]]["tally.py"] == BAD
    assert requests == [] and final["quality_reviews"] == []


def test_failed_semantic_review_never_becomes_success(tmp_path):
    rejection = {"passed": False, "issues": ["UNIT disagreement"], "summary": "UNIT rejected"}
    engine, run, requests, _ = ready_run(tmp_path, [rejection])
    spend(engine, run)
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "needs_human" and final["verified_hash"]
    assert final["quality_review"]["passed"] is False and len(requests) == 1
    assert not any(event["type"] == "budget_finalized" for event in engine.store.events(run["id"]))


def test_insufficient_actual_review_prompt_budget_does_not_send(tmp_path):
    engine, run, requests, _ = ready_run(tmp_path)
    spend(engine, run, tokens=run["budget"]["max_tokens"] - 1000)
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "needs_human" and requests == []
    assert final["verifications"][-1]["status"] == "passed"
    assert final["model_metrics"]["model_calls"] == 38
    assert "token budget" in final["summary"]


@pytest.mark.parametrize("status", ["pending", "running"])
def test_unfinished_delegation_prevents_automatic_finish(tmp_path, status):
    engine, run, requests, _ = ready_run(tmp_path)
    spend(engine, run)
    engine.store.update_run(run["id"], task_graph=[{"id": "remaining", "status": status}])
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "needs_human" and not requests
    assert not final["verifications"]
    assert final["task_graph"][0]["status"] == ("pending" if status == "pending" else "failed")


def test_wall_reserve_switches_at_safe_boundary(tmp_path, monkeypatch):
    engine, run, requests, _ = ready_run(tmp_path, [REVIEW_OK])
    monkeypatch.setattr(engine, "_seconds_left", lambda run_id: 20)
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "succeeded" and len(requests) == 1
    assert final["finishing_state"]["reason"] == "seconds_reserve"


def test_old_run_without_policy_uses_original_controller_path(tmp_path):
    engine, new, requests, _ = ready_run(tmp_path, [action("stop")])
    old = {key: deepcopy(value) for key, value in new.items() if key != "finishing_budget"}
    old["id"] = "legacy-without-policy"
    engine.store.create_run(old)
    spend(engine, old)
    asyncio.run(engine.execute(old["id"]))
    final = engine.store.run(old["id"])
    assert final["status"] == "needs_human" and len(requests) == 1
    assert "finishing_budget" not in final and "finishing_state" not in final
    assert final["controller_decisions"][0]["action"] == "stop"


def test_tiny_budget_scales_allowance_without_increasing_limits():
    budget = budget_config({"max_calls": 1, "max_tokens": 4096, "max_steps": 1,
                            "max_verifications": 1, "wall_seconds": 30})
    before = deepcopy(budget)
    policy = freeze_policy(budget)
    assert budget == before
    assert policy["reserved_calls"] == policy["reserved_steps"] == policy["reserved_verifications"] == 0
    assert 0 < policy["reserved_tokens"] < budget["max_tokens"]
    assert 0 < policy["reserved_seconds"] < budget["wall_seconds"]


def test_finalizing_host_state_blocks_writes_tools_and_stale_worker_request(tmp_path):
    async def scenario():
        engine, stale, requests, _ = ready_run(tmp_path)
        await engine.sandbox.prepare(stale["id"], stale["bundle"])
        engine.store.update_run(stale["id"], finishing_state={"phase": "finalizing"})
        for name in ("edit", "tool", "dispatch", "continue"):
            with pytest.raises(DecisionRejected, match="收尾阶段"):
                await engine._action(stale["id"], action(name))
        with pytest.raises(FinishingBudgetReached, match="development_closed"):
            await engine.model.work(stale, {"id": "late", "instructions": "UNIT", "write_paths": []}, {})
        assert requests == [] and engine.sandbox.apply_calls == []
    asyncio.run(scenario())


def test_candidate_changed_during_final_review_invalidates_success(tmp_path):
    engine, run, _, _ = ready_run(tmp_path)
    spend(engine, run)
    def mutate(payload):
        engine.sandbox.files[run["id"]]["tally.py"] += "# changed during review\n"
        return {"choices": [{"message": {"content": json.dumps(REVIEW_OK)}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 7}}
    engine.runtime._post = Mock(side_effect=mutate)
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "needs_human" and final["verified_hash"] is None


def test_parallel_requests_share_reserve_and_unknown_usage_is_charged(tmp_path):
    async def scenario():
        engine, run, _, _ = ready_run(tmp_path)
        spend(engine, run, calls=37)
        entered, release = threading.Event(), threading.Event()
        def post(payload):
            entered.set()
            release.wait(3)
            return {"choices": [{"message": {"content": '{"summary":"UNIT","files":{}}'}, "finish_reason": "stop"}]}
        engine.runtime._post = Mock(side_effect=post)
        worker = {"id": "first", "instructions": "UNIT", "write_paths": []}
        first = asyncio.create_task(engine.model.work(run, worker, {}))
        try:
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            with pytest.raises(FinishingBudgetReached, match="calls_reserve"):
                await engine.model.work(run, {**worker, "id": "second"}, {})
            active = engine.store.run(run["id"])["model_metrics"]
            assert active["model_calls"] == 38 and active["in_flight"] == 1
            reservation = active["reserved_tokens"]
        finally:
            release.set()
            await first
        final = engine.store.run(run["id"])["model_metrics"]
        assert engine.runtime._post.call_count == 1
        assert final["model_calls"] == 38 and final["in_flight"] == final["reserved_tokens"] == 0
        assert final["budget_tokens"] == 100 + reservation
        assert final["unknown_usage_calls"] == 1 and not final["metrics_complete"]
    asyncio.run(scenario())


def test_development_format_retry_cannot_spend_reserved_review_call(tmp_path):
    async def scenario():
        engine, run, requests, _ = ready_run(tmp_path, ["invalid JSON"])
        spend(engine, run, calls=37)
        with pytest.raises(FinishingBudgetReached, match="calls_reserve"):
            await engine.model.decide(run, {})
        metrics = engine.store.run(run["id"])["model_metrics"]
        assert len(requests) == 1 and metrics["model_calls"] == 38 and metrics["contract_retries"] == 0
        blocked = [event for event in engine.store.events(run["id"]) if event["type"] == "budget_reserve_blocked"]
        assert blocked[-1]["data"]["format_retry"] is True
    asyncio.run(scenario())


def test_parallel_token_admission_counts_inflight_reservations(tmp_path):
    async def scenario():
        engine, run, _, _ = ready_run(tmp_path, budget={"max_tokens": 4096, "max_output_tokens": 512})
        entered, release = threading.Event(), threading.Event()
        def post(payload):
            entered.set()
            release.wait(3)
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 20}}
        engine.runtime._post = Mock(side_effect=post)
        messages = [{"role": "user", "content": "x" * 1300}]
        first = asyncio.create_task(engine.model._request(run, "worker", "first", messages, 0))
        try:
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            with pytest.raises(FinishingBudgetReached, match="tokens_reserve"):
                await engine.model._request(run, "worker", "second", messages, 0)
            metrics = engine.store.run(run["id"])["model_metrics"]
            assert metrics["model_calls"] == 1 and metrics["in_flight"] == 1
            assert metrics["reserved_tokens"] < 4096 - run["finishing_budget"]["reserved_tokens"]
        finally:
            release.set()
            await first
        metrics = engine.store.run(run["id"])["model_metrics"]
        assert metrics["budget_tokens"] == 20 and metrics["reserved_tokens"] == 0
        assert engine.runtime._post.call_count == 1
    asyncio.run(scenario())


def test_finishing_uses_last_verification_slot_and_does_not_retest_pass(tmp_path):
    engine, run, requests, _ = ready_run(tmp_path, [REVIEW_OK], budget={"max_verifications": 2})
    engine.store.update_run(run["id"], verifications=[{"status": "failed", "candidate_hash": "old-source"}])
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "succeeded" and len(requests) == 1
    assert len(final["verifications"]) == 2 and len(engine.sandbox.test_results) == 1
    assert final["finishing_state"]["reason"] == "verifications_reserve"


def test_no_verification_slots_left_cannot_spend_review_or_claim_success(tmp_path):
    engine, run, requests, _ = ready_run(tmp_path, budget={"max_verifications": 2})
    engine.store.update_run(run["id"], verifications=[
        {"status": "failed", "candidate_hash": "old-a"}, {"status": "failed", "candidate_hash": "old-b"}])
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "needs_human" and requests == [] and not engine.sandbox.test_results
    assert len(final["verifications"]) == 2


def test_last_failed_same_candidate_is_not_retested_during_finishing(tmp_path):
    engine, run, requests, _ = ready_run(tmp_path, source=BAD)
    async def scenario():
        await engine.sandbox.prepare(run["id"], run["bundle"])
        fingerprint = await engine.sandbox.fingerprint(run["id"])
        engine.store.update_run(run["id"], verifications=[{"status": "failed", "candidate_hash": fingerprint}])
        spend(engine, run)
        await engine.execute(run["id"])
    asyncio.run(scenario())
    final = engine.store.run(run["id"])
    assert final["status"] == "needs_human" and requests == [] and not engine.sandbox.test_results
    assert len(final["verifications"]) == 1


def test_normal_development_can_use_reserve_for_last_semantic_review(tmp_path):
    _, engine, requests, queue = setup_app(tmp_path, [PREPARED, REVIEW_OK,
        dispatch("implement", "UNIT implement"), {"summary": "UNIT code", "files": {"tally.py": GOOD}},
        action("verify"), REVIEW_OK])
    run = engine.create_run_from_requirement(REQUEST, {"max_calls": 6, "max_tokens": 300000})
    asyncio.run(engine.execute(run["id"]))
    final = engine.store.run(run["id"])
    assert final["status"] == "succeeded" and len(requests) == 6 and not queue
    assert final["model_metrics"]["model_calls"] == final["budget"]["max_calls"]
    assert final["task_graph"][0]["status"] == "succeeded"
    assert len(engine.sandbox.test_results) == 2  # Input baseline + candidate, no duplicate final test.
    assert final["finishing_state"]["reason"] == "calls_reserve"


def test_hard_timeout_during_final_review_retains_unknown_remote_usage(tmp_path, monkeypatch):
    async def scenario():
        engine, run, _, _ = ready_run(tmp_path)
        spend(engine, run)
        entered, release = threading.Event(), threading.Event()
        def post(payload):
            entered.set()
            release.wait(3)
            return {"choices": [{"message": {"content": json.dumps(REVIEW_OK)}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 7}}
        engine.runtime._post = Mock(side_effect=post)
        original_timeout = asyncio.timeout
        monkeypatch.setattr("agentforge.autonomous.asyncio.timeout", lambda seconds: original_timeout(.1))
        try:
            await engine.execute(run["id"])
            assert entered.is_set()
            final = engine.store.run(run["id"])
            assert final["status"] == "needs_human" and not final["quality_reviews"]
            assert final["model_metrics"]["unknown_usage_calls"] == 1
            assert final["model_metrics"]["in_flight"] == final["model_metrics"]["reserved_tokens"] == 0
            assert final["model_metrics"]["budget_tokens"] > 100
            assert final["model_metrics"]["tokens"] == 100
            assert any(event["type"] == "budget_finalization_stopped"
                       and event["data"]["error_type"] == "TimeoutError"
                       for event in engine.store.events(run["id"]))
        finally:
            release.set()
    asyncio.run(scenario())
