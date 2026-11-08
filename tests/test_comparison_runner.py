"""Comparison orchestration with inert engines and no network or Docker.

These fixtures intentionally produce no candidate or functional pass claim.
They verify registration/retention/stop behavior, not model quality.
"""
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_comparison as runner
from agentforge.runtime import RuntimeErrorSafe
from agentforge.storage import now


IMAGE = "sha256:" + "a" * 64
TASK_IDS = list(runner.TASKS)


def test_trial_order_pairs_all_arms_and_is_fixed_before_results():
    trials = runner.trial_order(TASK_IDS, 2)
    assert trials == runner.trial_order(TASK_IDS, 2)
    assert len(trials) == 24
    assert [t["trial"] for t in trials] == list(range(1, 25))
    for repetition in (1, 2):
        for task in TASK_IDS:
            group = [t for t in trials if t["repetition"] == repetition and t["task_id"] == task]
            assert {t["arm"] for t in group} == set(runner.ARMS)
            assert [t["arm_position"] for t in group] == [1, 2, 3]
            assert all(t["state"] == "pending" and t["run_id"] is None for t in group)
    for task in TASK_IDS:
        first = [t["arm"] for t in trials if t["task_id"] == task and t["repetition"] == 1]
        second = [t["arm"] for t in trials if t["task_id"] == task and t["repetition"] == 2]
        assert second == list(reversed(first))
    counts = Counter((t["arm"], t["arm_position"]) for t in trials)
    assert max(counts.values()) - min(counts.values()) <= 2


@pytest.fixture
def batch_environment(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    observed = SimpleNamespace(configured=[], executed=[], mode="normal", verify_count=0)

    class FakeRuntime:
        def __init__(self, root, data_dir, **kwargs):
            self.root, self.data_dir = root, data_dir
            self.settings = {"api_key": "not-a-real-unit-key", "model": "fake-model",
                             "base_url": "https://example.invalid/v1", "image": kwargs.get("image_id", "fake:tag"),
                             "min_request_interval": 4.2, "timeout": 60}

        def _docker_command(self, *args):
            return ["never-executed-docker", *args]

        async def _command(self, _args):
            return 0, IMAGE, ""

        async def health(self):
            return {"sandbox": {"available": True}, "model": {"configured": True}}

        def model_config(self):
            return {"model": "fake-model", "test_only": True}

        def snapshot_task(self, task):
            return {"id": task, "requirement": "inert requirement " + task,
                    "baseline": "# inert text", "acceptance": "# never executed",
                    "snapshot_sha256": "b" * 64, "acceptance_sha256": "c" * 64,
                    "baseline_sha256": "d" * 64, "requirement_sha256": "e" * 64,
                    "expected_tests": 10}

        async def diff(self, _run_id):
            return {"files": []}

        def budget_record(self, run_id):
            requests = []
            if observed.mode == "pending_transport":
                requests = [{"request_id": "inert-pending", "invocation_id": "inert-invocation",
                             "sequence": 1, "sent": True, "status": "pending", "usage": {},
                             "metrics_complete": False, "model": "fake-model", "max_tokens": 4096,
                             "temperature": 0.1}]
            return {"run_id": run_id, "requests": requests, "denials": [], "sent_requests": len(requests),
                    "denied_requests": 0, "max_requests": 24, "model_tokens_known": 0,
                    "metrics_complete": not requests}

    class FakeEngine:
        def __init__(self, store, runtime, arm="single_direct"):
            self.store, self.runtime, self.arm = store, runtime, arm

        def configure_run(self, requirement, task_id, task_snapshot):
            definition = runner.arm_definition(self.arm, self.store.agents())
            run = {"id": uuid4().hex, "comparison_arm": self.arm, "status": "queued",
                   "workflow_id": "development", "mode": "live", "task_id": task_id,
                   "task": runner.public_task(task_snapshot), "task_snapshot": task_snapshot,
                   "requirement": requirement, "context": {}, "created_at": now(), "updated_at": now(),
                   "candidate_origin": "live_model_development", "fault_injection": False,
                   "model_config": self.runtime.model_config(), "attempt": 0, "applied": False,
                   "model_calls": 0, "model_tokens": 0, "model_metrics_complete": True,
                   "workflow": definition["workflow"], "agent_versions": definition["agent_versions"]}
            self.store.create_run(run)
            observed.configured.append(run["id"])
            return run

        async def execute(self, run_id):
            observed.executed.append(run_id)
            if observed.mode == "timeout":
                try:
                    await asyncio.Future()
                finally:
                    self.store.update_run(run_id, status="blocked", summary="inert cancellation")
                return
            status = "failed" if len(observed.executed) % 2 else "succeeded"
            self.store.step(run_id, {"id": "develop", "status": status, "output": None})
            self.store.event(run_id, "status", "inert fixture", node_id=None)
            self.store.update_run(run_id, status=status, summary="inert fixture; no candidate executed")

    def verify(*_args, **_kwargs):
        observed.verify_count += 1
        if observed.mode == "source_changed" and observed.verify_count == 2:
            raise RuntimeErrorSafe("inert frozen source mismatch")

    monkeypatch.setattr(runner, "Runtime", FakeRuntime)
    monkeypatch.setattr(runner, "BudgetRuntime", FakeRuntime)
    monkeypatch.setattr(runner, "SingleDirectEngine", FakeEngine)
    monkeypatch.setattr(runner, "ComparisonEngine", FakeEngine)
    monkeypatch.setattr(runner, "validate_control_evidence", lambda *_: ({"docker_image_id": IMAGE}, {}))
    monkeypatch.setattr(runner, "freeze_sources", lambda *_: {})
    monkeypatch.setattr(runner, "verify_frozen", verify)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda *_a, **_k: pytest.fail("Network prohibited"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *_a, **_k: pytest.fail("Subprocess prohibited"))
    args = SimpleNamespace(output=root / ".agentforge/comparisons/inert-batch", controls=root / "inert-controls.json",
                           tasks=TASK_IDS, repetitions=2, max_requests=24, timeout=600, dry_run=False)
    return root, args, observed


def read_state(args):
    return json.loads((args.output / "state.json").read_text(encoding="utf-8"))


def test_dry_run_freezes_24_trials_without_creating_or_executing_runs(batch_environment):
    root, args, observed = batch_environment
    args.dry_run = True
    assert asyncio.run(runner.run_batch(args, root)) == 0
    assert observed.configured == observed.executed == []
    plan = json.loads((args.output / "preregistration.json").read_text(encoding="utf-8"))
    assert len(plan["trials"]) == 24
    assert plan["docker_image_id"] == IMAGE
    assert plan["limits"]["http_requests_per_trial"] == 24
    assert all(t["state"] == "pending" for t in read_state(args)["trials"])


def test_complete_batch_keeps_all_successes_failures_and_null_step_exports(batch_environment):
    root, args, observed = batch_environment
    assert asyncio.run(runner.run_batch(args, root)) == 0
    state = read_state(args)
    assert state["status"] == "completed"
    assert len(observed.executed) == len(set(observed.executed)) == 24
    assert len(state["trials"]) == 24
    assert Counter(t["native_status"] for t in state["trials"]) == {"failed": 12, "succeeded": 12}
    for trial in state["trials"]:
        evidence = json.loads((args.output / trial["evidence_path"]).read_text(encoding="utf-8"))
        assert evidence["run"]["steps"][0]["output"] is None
        assert evidence["run"]["events"][-1]["node_id"] is None
        assert evidence["candidate_artifacts"] == []
        assert evidence["report"]["primary_success"] is False


def test_changed_frozen_input_stops_new_submissions_without_dropping_denominator(batch_environment):
    root, args, observed = batch_environment
    observed.mode = "source_changed"
    assert asyncio.run(runner.run_batch(args, root)) == 2
    state = read_state(args)
    assert len(observed.executed) == 1
    assert state["status"] == "blocked"
    assert len(state["trials"]) == 24
    assert sum(t["state"] == "not_submitted" for t in state["trials"]) == 23


def test_timeout_stops_new_submissions_and_preserves_interrupted_trial(batch_environment, monkeypatch):
    root, args, observed = batch_environment
    observed.mode, args.timeout = "timeout", 0
    original_wait = asyncio.wait

    async def fast_wait(tasks, **_kwargs):
        return await original_wait(tasks, timeout=0.001)

    monkeypatch.setattr(asyncio, "wait", fast_wait)
    assert asyncio.run(runner.run_batch(args, root)) == 2
    state = read_state(args)
    assert len(observed.executed) == 1
    assert state["trials"][0]["state"] == "interrupted"
    assert sum(t["state"] == "not_submitted" for t in state["trials"]) == 23


def test_unknown_inflight_transport_stops_new_submissions(batch_environment):
    root, args, observed = batch_environment
    observed.mode = "pending_transport"
    assert asyncio.run(runner.run_batch(args, root)) == 2
    state = read_state(args)
    assert len(observed.executed) == 1
    assert len(state["trials"]) == 24
    assert sum(t["state"] == "not_submitted" for t in state["trials"]) == 23

