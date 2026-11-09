"""Forward-compatible verification phase events with fake runtime results only."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from test_autonomous_engine import make_engine, BUNDLE
from agentforge.runtime import RuntimeErrorSafe


def test_verification_start_is_observable_before_result_and_does_not_claim_container(tmp_path):
    async def scenario():
        engine, run, _, sandbox = make_engine(tmp_path, [])
        await sandbox.prepare(run["id"], BUNDLE)
        invoked, release = asyncio.Event(), asyncio.Event()
        async def fake_test(_):
            invoked.set()
            await release.wait()
            return {"status": "passed", "isolated": True, "exit_code": 0, "test_count": 1,
                    "stdout": "FAKE UNIT RESULT", "stderr": ""}
        sandbox.test = fake_test
        execution = asyncio.create_task(engine._verify(run["id"]))
        await invoked.wait()
        during = engine.store.events(run["id"])
        start = next(event for event in during if event["type"] == "verification_started")
        assert start["data"]["stage"] == "preparing"
        assert start["data"]["container_confirmed"] is False
        assert start["data"]["index"] == 1
        assert start["data"]["classification"] == "input_baseline"
        assert start["data"]["acceptance_origin"] == "maintainer_provided"
        assert not any(event["type"] == "verification" for event in during)
        release.set()
        await execution
        completed = next(event for event in engine.store.events(run["id"]) if event["type"] == "verification")
        assert completed["id"] > start["id"]
        assert completed["data"]["index"] == start["data"]["index"]
        assert completed["data"]["candidate_hash"] == start["data"]["candidate_hash"]
        assert completed["data"]["isolated"] is True
    asyncio.run(scenario())


@pytest.mark.parametrize("failure,reason", [(asyncio.CancelledError(), "cancelled"),
                                           (RuntimeErrorSafe("unit failure"), "exception")])
def test_interruption_is_paired_with_started_without_fabricated_completion(tmp_path, failure, reason):
    async def scenario():
        engine, run, _, sandbox = make_engine(tmp_path, [])
        await sandbox.prepare(run["id"], BUNDLE)
        sandbox.test = AsyncMock(side_effect=failure)
        with pytest.raises(type(failure)):
            await engine._verify(run["id"])
        events = [event for event in engine.store.events(run["id"]) if event["type"].startswith("verification")]
        assert [event["type"] for event in events] == ["verification_started", "verification_interrupted"]
        assert events[1]["data"]["reason"] == reason
        assert events[1]["data"]["completion_unknown"] is True
        assert events[1]["data"]["candidate_hash"] == events[0]["data"]["candidate_hash"]
        assert engine.store.run(run["id"])["verifications"] == []
        assert engine.store.run(run["id"])["verified_hash"] is None
    asyncio.run(scenario())


def test_candidate_change_after_test_has_distinct_interruption_reason(tmp_path):
    async def scenario():
        engine, run, _, sandbox = make_engine(tmp_path, [])
        await sandbox.prepare(run["id"], BUNDLE)
        sandbox.fingerprint = AsyncMock(side_effect=["before", "after"])
        with pytest.raises(RuntimeErrorSafe, match="Candidate changed"):
            await engine._verify(run["id"])
        interrupted = next(event for event in engine.store.events(run["id"]) if event["type"] == "verification_interrupted")
        assert interrupted["data"]["reason"] == "candidate_changed"
        assert engine.store.run(run["id"])["verifications"] == []
    asyncio.run(scenario())


def test_normal_blocked_runtime_receipt_remains_a_completed_verification(tmp_path):
    async def scenario():
        engine, run, _, sandbox = make_engine(tmp_path, [])
        await sandbox.prepare(run["id"], BUNDLE)
        sandbox.test = AsyncMock(return_value={"status": "blocked", "isolated": False, "exit_code": None,
                                               "test_count": 0, "reason": "UNIT unavailable", "stdout": "", "stderr": ""})
        with pytest.raises(RuntimeErrorSafe, match="Docker verification blocked"):
            await engine._verify(run["id"])
        events = [event["type"] for event in engine.store.events(run["id"]) if event["type"].startswith("verification")]
        assert events == ["verification_started", "verification"]
        assert engine.store.run(run["id"])["verifications"][-1]["isolated"] is False
    asyncio.run(scenario())
