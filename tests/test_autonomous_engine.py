"""In-memory unit simulations only: these results are NOT model/Docker evidence."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from agentforge.autonomous import AutonomousEngine, DecisionRejected
from agentforge.autonomous_sandbox import SandboxIntegrityError
from agentforge.runtime import RuntimeErrorSafe
from agentforge.storage import Store


BUNDLE = {
    "requirement": "UNIT SIMULATION: implement arbitrary modules, no real model request",
    "files": {"alpha.py": "baseline", "beta.py": "baseline"},
    "acceptance": "UNIT SIMULATION: no Python execution",
    "provenance": {"kind": "natural", "description": "unit simulation only"},
}


def task(identity, paths=(), deps=()):
    return {"id": identity, "title": identity, "instructions": "unit simulated subtask " + identity,
            "write_paths": list(paths), "depends_on": list(deps)}


def action(name, **extra):
    return {"action": name, "summary": "unit simulated " + name, **extra}


class FakeRuntime:
    def model_config(self):
        return {"simulation": True}


class FakeSandbox:
    """Stores strings only; never imports a candidate or launches Docker."""
    def __init__(self, verification_statuses=None):
        self.files = {}
        self.verification_statuses = list(verification_statuses or ["passed"])
        self.test_calls = 0
        self.applied = []

    @staticmethod
    def validate_bundle(bundle):
        return deepcopy(bundle)

    async def prepare(self, run_id, bundle):
        self.files[run_id] = deepcopy(bundle["files"])

    async def read(self, run_id):
        return deepcopy(self.files[run_id])

    async def apply(self, run_id, files, allowed_paths):
        if not set(files) <= set(allowed_paths):
            raise ValueError("unit simulated unauthorized write")
        self.files[run_id].update(files)
        self.applied.append(deepcopy(files))
        return {"simulation": True, "candidate_sha256": await self.fingerprint(run_id)}

    async def fingerprint(self, run_id):
        return hashlib.sha256(json.dumps(self.files[run_id], sort_keys=True).encode()).hexdigest()

    async def test(self, run_id):
        index = min(self.test_calls, len(self.verification_statuses) - 1)
        status = self.verification_statuses[index]
        self.test_calls += 1
        # isolated=True here is a fake value to exercise the gate, NOT evidence.
        return {"status": status, "exit_code": 0 if status == "passed" else 1,
                "isolated": True, "test_count": 1, "stdout": "UNIT SIMULATION ONLY", "stderr": ""}


class FakeModel:
    def __init__(self, decisions, outputs=None):
        self.decisions = list(decisions)
        self.outputs = outputs or {}
        self.contexts = []
        self.work_contexts = {}
        self.work_ids = []
        self.active = self.max_active = 0
        self.work_started = asyncio.Event()
        self.hold_work = False
        self.config_version = 1

    def config(self, budget):
        return {"simulation": True, "revision": self.config_version}

    async def decide(self, run, context):
        self.contexts.append(deepcopy(context))
        decision = self.decisions.pop(0)
        return decision(run, context) if callable(decision) else deepcopy(decision)

    async def work(self, run, assigned, context):
        self.work_ids.append(assigned["id"])
        self.work_contexts[assigned["id"]] = deepcopy(context)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.work_started.set()
        try:
            if self.hold_work:
                await asyncio.Event().wait()
            await asyncio.sleep(0.002)
            output = self.outputs.get(assigned["id"], {"summary": "simulated output", "files": {}})
            if isinstance(output, BaseException):
                raise output
            return deepcopy(output)
        finally:
            self.active -= 1


def make_engine(tmp_path, decisions, outputs=None, statuses=None, budget=None):
    model, sandbox = FakeModel(decisions, outputs), FakeSandbox(statuses)
    store = Store(tmp_path / "simulation.sqlite3")
    engine = AutonomousEngine(store, FakeRuntime(), model=model, sandbox=sandbox)
    run = engine.create_run(BUNDLE, budget)
    return engine, run, model, sandbox


def finish_run(engine, run):
    asyncio.run(engine.execute(run["id"]))
    return engine.store.run(run["id"])


@pytest.mark.parametrize("assigned", [
    [task("domain_parser", ["alpha.py"])],
    [task("quantity_analysis"), task("unicode_layout", ["beta.py"])],
])
def test_arbitrary_decompositions_not_fixed_roles_or_catalog(tmp_path, assigned):
    engine, run, model, sandbox = make_engine(tmp_path, [
        action("dispatch", tasks=assigned), action("verify"), action("finish")])
    final = finish_run(engine, run)
    assert final["status"] == "succeeded"
    assert [t["id"] for t in final["task_graph"]] == [t["id"] for t in assigned]
    assert len({t["agent_id"] for t in final["task_graph"]}) == len(assigned)
    assert all("role" not in t for t in final["task_graph"])
    assert sandbox.test_calls == 1


def test_dependencies_and_disjoint_parallel_wave(tmp_path):
    assigned = [task("parse", ["alpha.py"]), task("integrate", ["alpha.py"], ["parse"]), task("lookup", ["beta.py"])]
    outputs = {"parse": {"summary": "parser", "files": {"alpha.py": "parsed"}},
               "lookup": {"summary": "lookup", "files": {"beta.py": "lookup"}},
               "integrate": {"summary": "integration", "files": {"alpha.py": "integrated"}}}
    engine, run, model, _ = make_engine(tmp_path, [action("dispatch", tasks=assigned), action("continue"), action("verify"), action("finish")], outputs)
    final = finish_run(engine, run)
    assert final["status"] == "succeeded"
    assert model.work_ids == ["parse", "lookup", "integrate"]
    assert model.max_active == 2
    assert model.work_contexts["integrate"]["project"] == {"alpha.py": "parsed", "beta.py": "lookup"}


def test_overlapping_writes_are_serialized(tmp_path):
    engine, run, model, _ = make_engine(tmp_path, [
        action("dispatch", tasks=[task("one", ["alpha.py"]), task("two", ["alpha.py"])]),
        action("continue"), action("verify"), action("finish")])
    assert finish_run(engine, run)["status"] == "succeeded"
    assert model.work_ids == ["one", "two"]
    assert model.max_active == 1


def test_verification_feedback_changes_real_task_graph_and_preserves_failure(tmp_path):
    outputs = {"initial": {"summary": "first simulated candidate", "files": {"alpha.py": "bad"}},
               "boundary_repair": {"summary": "follow feedback", "files": {"alpha.py": "fixed"}}}
    engine, run, model, _ = make_engine(tmp_path, [
        action("dispatch", tasks=[task("initial", ["alpha.py"])]), action("verify"),
        action("dispatch", tasks=[task("boundary_repair", ["alpha.py"], ["initial"])]),
        action("verify"), action("finish")], outputs, statuses=["failed", "passed"])
    final = finish_run(engine, run)
    assert final["status"] == "succeeded"
    assert model.contexts[2]["last_verification"]["status"] == "failed"
    assert [v["status"] for v in final["verifications"]] == ["failed", "passed"]
    assert len({v["candidate_hash"] for v in final["verifications"]}) == 2
    assert [t["id"] for t in final["task_graph"]] == ["initial", "boundary_repair"]
    assert len([e for e in engine.store.events(run["id"]) if e["type"] == "verification"]) == 2


def test_cancel_failed_dependency_subtree_then_replace_keeps_history(tmp_path):
    engine, run, model, _ = make_engine(tmp_path, [
        action("dispatch", tasks=[task("failed_parent"), task("old_child", deps=["failed_parent"])]),
        action("cancel", task_ids=["old_child"]),
        action("dispatch", tasks=[task("replacement", ["beta.py"])]), action("verify"), action("finish")],
        {"failed_parent": RuntimeErrorSafe("simulated HTTP failure")})
    final = finish_run(engine, run)
    assert final["status"] == "succeeded"
    assert {t["id"]: t["status"] for t in final["task_graph"]} == {
        "failed_parent": "failed", "old_child": "cancelled", "replacement": "succeeded"}
    assert model.work_ids == ["failed_parent", "replacement"]
    assert "simulated HTTP failure" in final["task_graph"][0]["error"]


@pytest.mark.parametrize("assigned", [
    [task("one", deps=["missing"])],
    [task("one", deps=["two"]), task("two", deps=["one"])],
    [task("one"), task("one")],
    [task("one", ["../escape.py"])],
])
def test_invalid_dispatch_is_atomic_and_rejected(tmp_path, assigned):
    engine, run, _, _ = make_engine(tmp_path, [])
    with pytest.raises(DecisionRejected):
        engine._dispatch(run, action("dispatch", tasks=assigned))
    assert engine.store.run(run["id"])["task_graph"] == []


def test_ids_cannot_overwrite_completed_history(tmp_path):
    engine, run, _, _ = make_engine(tmp_path, [action("dispatch", tasks=[task("historic")]), action("stop")])
    final = finish_run(engine, run)
    with pytest.raises(DecisionRejected):
        engine._dispatch(final, action("dispatch", tasks=[task("historic")]))
    assert engine.store.run(run["id"])["task_graph"][0]["status"] == "succeeded"


def test_unauthorized_worker_write_is_failed_task_and_preserved(tmp_path):
    engine, run, _, sandbox = make_engine(tmp_path, [action("dispatch", tasks=[task("limited", ["alpha.py"])]), action("stop")],
        {"limited": {"summary": "bad write proposal", "files": {"beta.py": "unauthorized"}}})
    final = finish_run(engine, run)
    assert final["status"] == "needs_human"
    assert final["task_graph"][0]["status"] == "failed"
    assert sandbox.files[run["id"]]["beta.py"] == "baseline"
    result_events = [e for e in engine.store.events(run["id"]) if e["type"] == "task_result"]
    assert result_events[0]["data"]["status"] == "failed"


def test_finish_refuses_stale_verification(tmp_path):
    engine, run, model, sandbox = make_engine(tmp_path, [action("verify")])
    def mutate_before_finish(current, context):
        sandbox.files[current["id"]]["alpha.py"] = "external edit after verification"
        return action("finish")
    model.decisions.extend([mutate_before_finish, action("stop")])
    final = finish_run(engine, run)
    assert final["status"] == "needs_human"
    assert any(e["type"] == "decision_rejected" and "current candidate" in e["message"] for e in engine.store.events(run["id"]))


def test_finish_requires_verification_and_completed_pending_tasks(tmp_path):
    engine, run, _, _ = make_engine(tmp_path, [action("finish"), action("dispatch", tasks=[task("first"), task("later", deps=["first"])]),
        action("verify"), action("finish"), action("stop")])
    final = finish_run(engine, run)
    assert final["status"] == "needs_human"
    rejections = [e["message"] for e in engine.store.events(run["id"]) if e["type"] == "decision_rejected"]
    assert len(rejections) == 2
    assert "current candidate" in rejections[0]
    assert "Pending tasks" in rejections[1]


def test_step_budget_ends_run_and_terminal_execution_does_not_replay(tmp_path):
    engine, run, model, sandbox = make_engine(tmp_path, [action("verify")], budget={"max_steps": 1})
    final = finish_run(engine, run)
    assert final["status"] == "needs_human"
    assert "budget" in final["summary"]
    assert len(final["controller_decisions"]) == 1
    finish_run(engine, run)
    assert sandbox.test_calls == 1
    assert len(model.contexts) == 1


def test_verification_budget_failure_retains_first_result(tmp_path):
    engine, run, _, sandbox = make_engine(tmp_path, [action("verify"), action("verify")], budget={"max_verifications": 1})
    final = finish_run(engine, run)
    assert final["status"] == "needs_human"
    assert len(final["verifications"]) == sandbox.test_calls == 1
    assert len(final["controller_decisions"]) == 2


def test_frozen_model_configuration_prevents_reconfigured_execution(tmp_path):
    engine, run, model, _ = make_engine(tmp_path, [action("verify")])
    model.config_version = 2
    final = finish_run(engine, run)
    assert final["status"] == "failed"
    assert final["summary"] == "Frozen model configuration changed"
    assert model.contexts == []


def test_cancelled_worker_is_terminal_in_persisted_graph(tmp_path):
    engine, run, model, _ = make_engine(tmp_path, [action("dispatch", tasks=[task("slow", ["alpha.py"])])])
    model.hold_work = True
    async def interrupt():
        execution = asyncio.create_task(engine.execute(run["id"]))
        await model.work_started.wait()
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    asyncio.run(interrupt())
    final = engine.store.run(run["id"])
    assert final["status"] == "blocked"
    assert all(t["status"] != "running" for t in final["task_graph"])
    assert all(t["status"] != "running" for t in engine.store.steps(run["id"]))
    assert model.active == 0


def test_process_recovery_charges_unknown_inflight_without_replaying(tmp_path):
    engine, run, model, sandbox = make_engine(tmp_path, [action("verify")])
    interrupted_task = {**task("unconfirmed", ["alpha.py"]), "agent_id": "worker-unit", "status": "running"}
    metrics = {"model_calls": 3, "tokens": 100, "budget_tokens": 100,
               "in_flight": 2, "reserved_tokens": 900, "unknown_usage_calls": 0,
               "metrics_complete": False, "contract_retries": 1}
    engine.store.update_run(run["id"], status="running", task_graph=[interrupted_task], model_metrics=metrics)
    engine.store.step(run["id"], interrupted_task)
    engine.recover_interrupted()
    recovered = engine.store.run(run["id"])
    assert recovered["status"] == "blocked"
    assert recovered["task_graph"][0]["status"] != "running"
    assert engine.store.steps(run["id"])[0]["status"] != "running"
    assert recovered["model_metrics"] == {
        **metrics, "budget_tokens": 1000, "in_flight": 0, "reserved_tokens": 0,
        "unknown_usage_calls": 2, "metrics_complete": False}
    events = engine.store.events(run["id"])
    assert events[-1]["data"]["metrics_before"] == metrics
    engine.recover_interrupted()
    assert engine.store.run(run["id"]) == recovered
    assert engine.store.events(run["id"]) == events
    finish_run(engine, run)
    assert model.contexts == [] and model.work_ids == [] and sandbox.test_calls == 0


def test_recovery_leaves_completed_run_unchanged(tmp_path):
    engine, run, _, _ = make_engine(tmp_path, [action("verify"), action("finish")])
    finish_run(engine, run)
    previous = engine.export(run["id"])
    engine.recover_interrupted()
    assert engine.export(run["id"]) == previous


def test_casefolded_write_scopes_are_serialized(tmp_path):
    engine, run, model, _ = make_engine(tmp_path, [
        action("dispatch", tasks=[task("upper", ["Library/Alpha.py"]), task("lower", ["library/alpha.py"])]),
        action("continue"), action("verify"), action("finish")])
    assert finish_run(engine, run)["status"] == "succeeded"
    assert model.work_ids == ["upper", "lower"]
    assert model.max_active == 1


def test_fatal_candidate_rollback_stops_before_next_controller(tmp_path):
    engine, run, model, sandbox = make_engine(tmp_path, [
        action("dispatch", tasks=[task("unsafe_commit", ["alpha.py"])]), action("verify"), action("finish")],
        {"unsafe_commit": {"summary": "simulated proposal", "files": {"alpha.py": "candidate"}}})
    async def failed_rollback(*args):
        raise SandboxIntegrityError("unit simulated write failed and rollback failed")
    sandbox.apply = failed_rollback
    final = finish_run(engine, run)
    assert final["status"] == "failed"
    assert "rollback failed" in final["summary"]
    assert len(model.contexts) == len(final["controller_decisions"]) == 1
    assert sandbox.test_calls == 0
    assert final["task_graph"][0]["status"] != "running"
    assert engine.store.events(run["id"])[-1]["type"] == "terminal"


def test_fatal_second_commit_retains_already_completed_task(tmp_path):
    outputs = {"completed": {"summary": "successful first result", "files": {"alpha.py": "first"}},
               "broken_commit": {"summary": "failed second commit", "files": {"beta.py": "second"}}}
    engine, run, _, sandbox = make_engine(tmp_path, [
        action("dispatch", tasks=[task("completed", ["alpha.py"]), task("broken_commit", ["beta.py"])]), action("stop")], outputs)
    original_apply = sandbox.apply
    async def second_rollback_failed(run_id, files, allowed_paths):
        if "beta.py" in files:
            raise SandboxIntegrityError("unit simulated second rollback failed")
        return await original_apply(run_id, files, allowed_paths)
    sandbox.apply = second_rollback_failed
    final = finish_run(engine, run)
    assert final["status"] == "failed"
    assert final["task_graph"][0]["status"] == "succeeded"
    assert final["task_graph"][0]["result"]["summary"] == "successful first result"
    assert final["task_graph"][1]["status"] == "failed"
    assert "rollback failed" in final["task_graph"][1]["error"]
