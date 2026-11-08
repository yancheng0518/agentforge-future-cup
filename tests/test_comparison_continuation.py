"""Continuation claims and condition invariants; no model or Docker traffic."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import comparison_continuation as continuation
import evaluate_comparison as runner
from agentforge.runtime import RuntimeErrorSafe
from evaluate_tasks import canonical, digest
from test_comparison_runner import batch_environment, read_state  # noqa: F401


def write_parent(directory, plan, state):
    raw = json.dumps(plan, ensure_ascii=False, indent=2).encode()
    state["preregistration_sha256"] = digest(raw)
    (directory / "preregistration.json").write_bytes(raw)
    (directory / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def parent_fixture(tmp_path):
    root = tmp_path / "project"
    directory = root / ".agentforge/comparisons/parent"
    frozen = directory / "frozen/backend/agentforge/runtime.py"
    frozen.parent.mkdir(parents=True)
    content = b"# inert frozen runtime text, never executed\n"
    frozen.write_bytes(content)
    manifest = {"backend/agentforge/runtime.py": {"sha256": digest(content), "bytes": len(content)}}
    intended = runner.trial_order(["completion"], 1)
    plan = {key: {"inert": key} for key in continuation.SAME_CONDITIONS}
    plan.update(schema=runner.SCHEMA, trials=intended, source_manifest=manifest,
                source_manifest_sha256=digest(canonical(manifest)))
    observed = deepcopy(intended)
    observed[0].update(state="complete", run_id="retained-first-run")
    observed[1].update(state="interrupted", run_id="retained-interrupted-run")
    observed[2].update(state="not_submitted", run_id=None)
    state = {"status": "blocked", "trials": observed}
    write_parent(directory, plan, state)
    return root, directory, plan, state


def child_plan(parent):
    plan = deepcopy(parent["plan"])
    plan["trials"] = deepcopy(parent["trials"])
    return plan


def test_load_parent_selects_only_never_submitted_slots_and_changes_no_bytes(parent_fixture):
    root, directory, _plan, _state = parent_fixture
    before = {name: (directory / name).read_bytes() for name in ("state.json", "preregistration.json")}
    parent = continuation.load_parent(root, directory)
    assert [t["trial"] for t in parent["trials"]] == [3]
    assert parent["trials"][0]["state"] == "pending"
    assert parent["trials"][0]["run_id"] is None
    assert parent["reference"]["retained_parent_trials"] == [1, 2]
    assert before == {name: (directory / name).read_bytes() for name in before}


@pytest.mark.parametrize("mutation", ["running_parent", "ambiguous_slot", "duplicate_trial", "identity_changed",
                                    "duplicate_run_id", "preregistration_hash", "manifest_hash"])
def test_parent_rejects_ambiguous_or_changed_inventory(parent_fixture, mutation):
    root, directory, plan, state = parent_fixture
    if mutation == "running_parent":
        state["status"] = "running"
    elif mutation == "ambiguous_slot":
        state["trials"][2]["run_id"] = "uncertain-request"
    elif mutation == "duplicate_trial":
        state["trials"][1]["trial"] = 1
    elif mutation == "identity_changed":
        state["trials"][2]["arm"] = "single_direct"
    elif mutation == "duplicate_run_id":
        state["trials"][1]["run_id"] = state["trials"][0]["run_id"]
    elif mutation == "manifest_hash":
        plan["source_manifest_sha256"] = "0" * 64
    write_parent(directory, plan, state)
    if mutation == "preregistration_hash":
        state["preregistration_sha256"] = "0" * 64
        (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(RuntimeErrorSafe):
        continuation.load_parent(root, directory)


def test_parent_rejects_changed_frozen_source_and_external_directory(parent_fixture):
    root, directory, _plan, _state = parent_fixture
    (directory / "frozen/backend/agentforge/runtime.py").write_text("# changed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Frozen archive changed"):
        continuation.load_parent(root, directory)
    with pytest.raises(RuntimeErrorSafe, match="inside comparisons"):
        continuation.load_parent(root, root.parent)


@pytest.mark.parametrize("condition", ["model_config", "model_settings", "task_catalog", "task_order",
                                     "arms", "docker_image_id", "limits", "fault_injection"])
def test_continuation_rejects_model_task_budget_or_other_condition_drift(parent_fixture, condition):
    root, directory, _plan, _state = parent_fixture
    parent = continuation.load_parent(root, directory)
    plan = child_plan(parent)
    plan[condition] = {"changed": condition}
    with pytest.raises(RuntimeErrorSafe, match=condition):
        continuation.validate_conditions(parent, plan)


def test_continuation_allows_only_disclosed_observer_source_changes(parent_fixture):
    root, directory, _plan, _state = parent_fixture
    parent = continuation.load_parent(root, directory)
    plan = child_plan(parent)
    plan["source_manifest"]["scripts/evaluate_tasks.py"] = {"sha256": "c" * 64, "bytes": 10}
    amendment = continuation.validate_conditions(parent, plan)
    assert set(amendment["source_changes"]) == {"scripts/evaluate_tasks.py"}
    assert amendment["selected_trial_ids"] == [3]
    changed_runtime = deepcopy(plan)
    changed_runtime["source_manifest"]["backend/agentforge/runtime.py"]["sha256"] = "d" * 64
    with pytest.raises(RuntimeErrorSafe, match="frozen source"):
        continuation.validate_conditions(parent, changed_runtime)
    extra_slot = deepcopy(plan)
    extra_slot["trials"].append(deepcopy(parent["plan"]["trials"][0]))
    with pytest.raises(RuntimeErrorSafe, match="exactly"):
        continuation.validate_conditions(parent, extra_slot)


def test_claim_is_exclusive_and_never_overwritten_even_under_race(parent_fixture):
    root, directory, _plan, _state = parent_fixture
    parent = continuation.load_parent(root, directory)

    def attempt(index):
        child = directory.parent / f"child-{index}"
        child.mkdir()
        try:
            return continuation.claim_continuation(root, parent, child, "f" * 64)
        except RuntimeErrorSafe:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(attempt, range(4)))
    winner = [claim for claim in claims if claim is not None]
    assert len(winner) == 1
    path = root / winner[0]["path"]
    original = path.read_bytes()
    assert digest(original) == winner[0]["sha256"]
    with pytest.raises(RuntimeErrorSafe, match="already has"):
        continuation.claim_continuation(root, parent, directory.parent / "another-child", "e" * 64)
    assert path.read_bytes() == original


def test_parent_mutation_after_registration_blocks_claim(parent_fixture):
    root, directory, _plan, _state = parent_fixture
    parent = continuation.load_parent(root, directory)
    (directory / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeErrorSafe, match="Parent evidence changed"):
        continuation.claim_continuation(root, parent, directory.parent / "child", "e" * 64)
    assert not (root / ".agentforge/comparison-continuation-claims").exists()


def test_new_phase_submits_only_original_unsubmitted_slots_and_claim_prevents_second_phase(batch_environment):
    root, args, observed = batch_environment
    observed.mode = "source_changed"
    assert asyncio.run(runner.run_batch(args, root)) == 2
    parent_directory = args.output
    parent_state = read_state(args)
    original_run = parent_state["trials"][0]["run_id"]
    original = {name: (parent_directory / name).read_bytes() for name in ("state.json", "preregistration.json")}
    observed.mode, observed.verify_count = "normal", 0
    args.output = parent_directory.parent / "phase-two"
    args.continue_unsubmitted = parent_directory
    assert asyncio.run(runner.run_batch(args, root)) == 0
    child = read_state(args)
    assert [t["trial"] for t in child["trials"]] == list(range(2, 25))
    assert len(observed.executed) == 24
    assert observed.executed.count(original_run) == 1
    assert original_run not in {t["run_id"] for t in child["trials"]}
    assert original == {name: (parent_directory / name).read_bytes() for name in original}
    args.output = parent_directory.parent / "duplicate-phase"
    assert asyncio.run(runner.run_batch(args, root)) == 2
    assert len(observed.executed) == 24
    assert all(t["state"] == "not_submitted" for t in read_state(args)["trials"])


def test_observer_exception_marks_submitted_trial_interrupted_and_does_not_resubmit(batch_environment, monkeypatch):
    root, args, observed = batch_environment
    original_export = runner.export_trial
    attempts = []

    def fail_first_export(*positional, **keywords):
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError("inert observer write failure")
        return original_export(*positional, **keywords)

    monkeypatch.setattr(runner, "export_trial", fail_first_export)
    assert asyncio.run(runner.run_batch(args, root)) == 2
    state = read_state(args)
    assert len(observed.executed) == 1
    assert state["trials"][0]["state"] == "interrupted"
    assert state["trials"][0]["run_id"]
    assert sum(t["state"] == "not_submitted" for t in state["trials"]) == 23
    parent = continuation.load_parent(root, args.output)
    assert parent["reference"]["retained_parent_trials"] == [1]
    assert [t["trial"] for t in parent["trials"]] == list(range(2, 25))


def test_initial_running_state_write_failure_keeps_run_but_sends_no_model_work(batch_environment, monkeypatch):
    root, args, observed = batch_environment
    original_write = runner.write_json
    failures = []

    def fail_first_running_state(path, value, *extra, **kwargs):
        if (Path(path).name == "state.json" and value.get("trials")
                and value["trials"][0]["state"] == "running" and not failures):
            failures.append(1)
            raise PermissionError("inert initial running-state save failure")
        return original_write(path, value, *extra, **kwargs)

    monkeypatch.setattr(runner, "write_json", fail_first_running_state)
    assert asyncio.run(runner.run_batch(args, root)) == 2
    state = read_state(args)
    assert failures == [1]
    assert len(observed.configured) == 1 and observed.executed == []
    assert state["trials"][0]["state"] == "interrupted"
    assert state["trials"][0]["native_status"] == "blocked"
    assert state["trials"][0]["run_id"] == observed.configured[0]
    assert sum(t["state"] == "not_submitted" for t in state["trials"]) == 23
    parent = continuation.load_parent(root, args.output)
    assert parent["reference"]["retained_parent_trials"] == [1]
    assert [t["trial"] for t in parent["trials"]] == list(range(2, 25))

