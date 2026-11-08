"""Interrupted evidence and unverified Docker launches; no network or candidates.

Model responses and Docker commands are stubbed. Cancellation uses a real
asyncio task while its fake second HTTP request is pending in a worker thread.
"""
import asyncio
import json
from pathlib import Path
import shutil
import threading
from unittest.mock import AsyncMock, patch

import pytest

from agentforge.engine import Engine
from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.storage import Store


ROOT = Path(__file__).resolve().parents[1]
FAKE_SECRET = "interruption-unit-test-not-a-real-key"


@pytest.fixture
def runtime(tmp_path):
    for name in ("examples", "knowledge", "acceptance"):
        shutil.copytree(ROOT / name, tmp_path / name)
    settings = {
        "api_key": FAKE_SECRET, "key_source": "unit_test",
        "base_url": "https://example.invalid/v1", "model": "fake-model",
        "timeout": 5, "min_request_interval": 0,
        "docker_backend": "native", "image": "unit-test:never-executed",
    }
    with patch("agentforge.runtime._settings", return_value=settings):
        value = Runtime(tmp_path, tmp_path / "data")
    with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network in unit tests")):
        yield value


async def cancelled_after_contract_error(runtime, invoke):
    second_request_started = threading.Event()
    release_second_request = threading.Event()
    count = 0

    def fake_post(_payload):
        nonlocal count
        count += 1
        if count == 1:
            return {"choices": [{"finish_reason": "stop", "message": {
                "content": "prose before JSON; fake credential " + FAKE_SECRET,
            }}], "usage": {"total_tokens": 7}}
        second_request_started.set()
        if not release_second_request.wait(timeout=5):
            raise AssertionError("Fake request was not released")
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": '{"summary":"late result","passed":true,"issues":[]}',
        }}], "usage": {"total_tokens": 11}}

    with patch.object(runtime, "_post", side_effect=fake_post):
        pending = asyncio.create_task(invoke())
        try:
            assert await asyncio.to_thread(second_request_started.wait, 2), "Retry request was not reached"
            pending.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await pending
        finally:
            release_second_request.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert count == 2
    return caught.value


def assert_partial_evidence(metrics):
    assert metrics["model_calls"] == 2
    assert metrics["tokens"] == 7
    assert metrics["metrics_complete"] is False
    assert metrics["contract_retries"] == 1
    diagnostics = metrics["model_diagnostics"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["error"]["kind"] == "json_syntax"
    assert diagnostics[0]["retry_scheduled"] is True
    assert diagnostics[0]["redacted"] is True
    assert FAKE_SECRET not in json.dumps(diagnostics)


def test_cancellation_preserves_known_usage_and_redacted_contract_diagnostic(runtime):
    async def scenario():
        error = await cancelled_after_contract_error(runtime, lambda: runtime.invoke(
            {"id": "reviewer", "role": "reviewer", "tools": []},
            {"mode": "live", "kind": "inspect"}, {}))
        assert_partial_evidence({name: getattr(error, name, None) for name in (
            "model_calls", "tokens", "metrics_complete", "contract_retries", "model_diagnostics")})

    asyncio.run(scenario())


def test_engine_persists_partial_model_evidence_when_invocation_is_cancelled(runtime):
    async def scenario():
        store = Store(runtime.data_dir / "interruptions.sqlite3")
        engine = Engine(store, runtime)
        run = engine.create_run("unit test only", mode="live")
        agent = next(a for a in run["agent_versions"] if a["id"] == "reviewer")
        await cancelled_after_contract_error(runtime, lambda: engine._invoke(
            run, {"id": "inspect"}, "round-0/verify/inspect", agent, {}))
        events = [e for e in store.events(run["id"]) if e["type"] == "agent"]
        assert len(events) == 1
        assert events[0]["data"]["status"] == "failed"
        assert_partial_evidence(events[0]["data"])
        persisted = store.run(run["id"])
        assert persisted["model_calls"] == 2
        assert persisted["model_tokens"] == 7
        assert persisted["model_metrics_complete"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [OSError("simulated Docker launch failure"),
                                       RuntimeErrorSafe("simulated command failure before launch")],
                         ids=["os_error", "safe_runtime_error"])
def test_docker_launch_error_does_not_claim_isolated_execution(runtime, error):
    async def scenario():
        await runtime.prepare("unverified-launch")
        command = AsyncMock(side_effect=[error, (0, "", "")])
        with patch.object(runtime, "health", AsyncMock(return_value={"sandbox": {"available": True}})), \
                patch.object(runtime, "_command", command):
            result = await runtime.test("unverified-launch")
        assert result["status"] != "passed"
        assert result["exit_code"] is None
        assert result["isolated"] is False
        assert result["stdout"] == ""
        assert result["reason"]
        assert command.await_args_list[0].args[0][:2] == ["docker", "run"]
        assert command.await_args_list[1].args[0][:3] == ["docker", "rm", "-f"]

    asyncio.run(scenario())
