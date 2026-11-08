"""Batch/provenance tests with fake API data; no model or candidate execution."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("evaluate_tasks", ROOT / "scripts/evaluate_tasks.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)

TASK = {"id": "completion", "requirement": "Explicit fixture contract", "expected_tests": 10,
        "snapshot_sha256": "a" * 64, "acceptance_sha256": "c" * 64}


def detail(repair=False, review_failure=False):
    events, steps = [], []

    def candidate(node, source):
        output = {"files": [{"path": "todo.py", "content": source}], "summary": "SIMULATED API DATA"}
        steps.append({"id": node, "status": "succeeded", "output": deepcopy(output)})
        events.extend([
            {"type": "agent", "node_id": node, "data": {"model_mode": "live", "model_calls": 1, "output": output}},
            {"type": "tool", "node_id": node, "data": {"source": "runtime", "tool": "candidate_diff", "status": "completed",
                                                          "output": {"files": [{"path": "todo.py", "after": source}]}}},
        ])

    def gate(number, passed, source, test_passed):
        scope = {"id": f"round-{number}/verify", "attempt": number,
                 "candidate_hash": evaluation.candidate_fingerprint({"files": [{"path": "todo.py", "after": source}]})}
        raw = {"isolated": True, "status": "passed" if test_passed else "failed", "exit_code": 0 if test_passed else 1,
               "acceptance_sha256": TASK["acceptance_sha256"],
               "stdout": 'AGENTFORGE_ACCEPTANCE=' + json.dumps({"tests": 10, "passed": test_passed}),
               "stderr": "Ran 10 tests in 0.01s\n" + ("OK" if test_passed else "FAILED (failures=1)")}
        test = {**raw, "evidence_scope": scope}
        review = {"passed": passed, "issues": [] if passed else ["SIMULATED ISSUE"], "evidence_scope": scope}
        steps.extend([{"id": scope["id"], "status": "succeeded", "output": {"validation_scope": scope, "test": test, "review": review}},
                      {"id": scope["id"] + "/test", "status": "succeeded", "output": {"test": test}}])
        events.extend([{"type": "tool", "node_id": scope["id"] + "/test", "data": {"source": "runtime", "tool": "run_acceptance", "output": raw}},
                       {"type": "validation", "node_id": f"round-{number}/gate", "data": {"attempt": number, "passed": passed, "test": test, "review": review}}])
        return test

    source = "# SIMULATED initial candidate" if repair else "# SIMULATED successful candidate"
    candidate("develop", source)
    if repair:
        test = gate(0, False, source, review_failure)
        events.append({"type": "feedback", "node_id": "round-0/gate", "data": {"next_attempt": 1, "test": test}})
        source = "# SIMULATED repaired candidate"
        candidate("round-1/fix", source)
    gate(int(repair), True, source, True)
    for number, event in enumerate(events, 1):
        event["id"] = number
    diff = {"files": [{"path": "todo.py", "after": source}]}
    return {"id": "b" * 32, "task_id": "completion", "task": deepcopy(TASK), "requirement": TASK["requirement"],
            "mode": "live", "workflow_id": "development", "candidate_origin": "live_model_development", "fault_injection": False,
            "status": "succeeded", "attempt": int(repair), "events": events, "steps": steps, "diff": diff,
            "verified_hash": evaluation.candidate_fingerprint(diff), "model_calls": 2 + int(repair), "model_tokens": 123,
            "model_metrics_complete": True, "applied": False,
            "created_at": "2026-11-08T00:00:00+00:00", "updated_at": "2026-11-08T00:00:03+00:00"}


def test_first_pass_is_not_natural_failure_or_repair():
    result = evaluation.classify_run(detail(), TASK)
    assert result["workflow_succeeded"]
    assert result["first_round_result"] == "passed"
    assert not result["natural_test_failure_observed"]
    assert not result["automatic_repair_observed"]


def test_non_node_status_events_do_not_break_candidate_classification():
    # Real Store events use node_id=None for run-level status/errors. These are
    # normal event records, not malformed candidates or grounds to lose a run.
    assert evaluation.valid_live_candidate({"type": "status", "node_id": None, "data": {}}, {}) is False
    value = detail()
    value["events"].insert(0, {"type": "status", "node_id": None, "data": {}, "message": "Run created"})
    for number, event in enumerate(value["events"], 1):
        event["id"] = number
    result = evaluation.classify_run(value, TASK)
    assert result["workflow_succeeded"]
    assert result["first_round_result"] == "passed"


@pytest.mark.parametrize("node", ["round-0/verify/test", "round-0/verify", "round-0/verify/review", "round-1/fix"])
def test_partial_running_steps_with_null_outputs_remain_observable(node):
    # Polling observes unfinished nodes with output=None. The observer must
    # export that state without raising or upgrading it into successful work.
    value = detail(repair=True)
    value["status"] = "running"
    step = next((s for s in value["steps"] if s["id"] == node), None)
    if step is None:
        step = {"id": node}
        value["steps"].append(step)
    step.update(status="running", output=None)
    result = evaluation.classify_run(value, TASK)
    assert result["status"] == "running"
    assert not result["workflow_succeeded"]
    if node == "round-1/fix":
        assert not result["automatic_repair_observed"]


def test_api_requirement_trimming_preserves_the_same_contract():
    declared = {**TASK, "requirement": TASK["requirement"] + "\n"}
    assert evaluation.classify_run(detail(), declared)["workflow_succeeded"]


def test_ordered_real_candidate_failure_and_fix_are_distinct_evidence():
    result = evaluation.classify_run(detail(repair=True), TASK)
    assert result["first_round_result"] == "test_failed"
    assert result["natural_test_failure_observed"]
    assert result["automatic_repair_after_natural_failure_succeeded"]
    assert [r["candidate_origin"] for r in result["rounds"]] == ["live_developer", "live_fixer"]


@pytest.mark.parametrize("test_passed", [False, True])
def test_completed_candidate_tests_are_observed_without_review_or_gate(test_passed):
    # Inspect can fail its model contract after the independent Docker test
    # completes. The test fact does not require a successful review/gate.
    value = detail(repair=True, review_failure=test_passed)
    value["status"] = "failed"
    value["attempt"] = 0
    value["events"] = [e for e in value["events"]
                       if e.get("node_id") in {"develop", "round-0/verify/test"}]
    value["steps"] = [s for s in value["steps"] if s["id"] in {"develop", "round-0/verify/test"}]
    value["steps"].append({"id": "round-0/verify", "status": "failed", "output": None})
    result = evaluation.classify_run(value, TASK)
    assert result["rounds"][0]["test_suite_completed"]
    assert result["rounds"][0]["test_passed"] is test_passed
    assert result["natural_test_failure_observed"] is (not test_passed)
    assert result["first_round_result"] == ("unverified" if test_passed else "test_failed")
    assert not result["strict_outcome"]["real_isolated_test_passed"]
    assert not result["workflow_succeeded"]
    assert not result["automatic_repair_observed"]


@pytest.mark.parametrize("change", ["origin", "injected", "task", "requirement", "mock", "baseline"])
def test_synthetic_or_unmatched_provenance_cannot_claim_natural_failure(change):
    value = detail(repair=True)
    if change == "origin":
        value["candidate_origin"] = "preset_fixture"
    elif change == "injected":
        value["fault_injection"] = True
    elif change == "task":
        value["task_id"] = "bulk_edit"
    elif change == "requirement":
        value["requirement"] = "different task"
    elif change == "mock":
        value["mode"] = "mock"
    elif change == "baseline":
        value["events"] = [e for e in value["events"] if e.get("node_id") != "develop"]
        # A later fix can be a live candidate but the initial failed baseline is
        # no longer a live-developer-produced failure.
    result = evaluation.classify_run(value, TASK)
    assert not result["natural_test_failure_observed"]
    assert not result["automatic_repair_after_natural_failure_succeeded"]


@pytest.mark.parametrize("change", ["incomplete", "startup", "unisolated", "wrong_hash", "missing_write", "out_of_order"])
def test_untrusted_or_incomplete_test_evidence_is_not_natural_failure(change):
    value = detail(repair=True)
    event = next(e for e in value["events"] if e.get("type") == "validation")
    test = event["data"]["test"]
    if change == "incomplete":
        test["stderr"] = "Candidate RPC timed out"
    elif change == "startup":
        test.update(exit_code=125, stdout="", stderr="container launch failed")
    elif change == "unisolated":
        test["isolated"] = False
    elif change == "wrong_hash":
        test["evidence_scope"]["candidate_hash"] = "wrong"
    elif change == "missing_write":
        value["events"] = [e for e in value["events"] if not (e.get("node_id") == "develop" and e.get("type") == "tool")]
    else:
        value["events"] = list(reversed(value["events"]))
    result = evaluation.classify_run(value, TASK)
    assert not result["natural_test_failure_observed"]


def test_review_only_rejection_does_not_count_as_test_failure():
    result = evaluation.classify_run(detail(repair=True, review_failure=True), TASK)
    assert result["review_only_failure_observed"]
    assert result["first_round_result"] == "review_rejected"
    assert result["automatic_repair_observed"]
    assert not result["natural_test_failure_observed"]


@pytest.mark.parametrize("change", ["test_hash", "snapshot_hash", "missing_test_hash", "missing_snapshot_hash"])
def test_offline_classification_rejects_another_tasks_ten_test_suite(change):
    value = detail(repair=True)
    declared = deepcopy(TASK)
    if change == "test_hash":
        declared["acceptance_sha256"] = "d" * 64
    elif change == "snapshot_hash":
        value["task"]["snapshot_sha256"] = "e" * 64
    elif change == "missing_test_hash":
        declared.pop("acceptance_sha256")
    else:
        declared.pop("snapshot_sha256")
    result = evaluation.classify_run(value, declared)
    assert not result["natural_test_failure_observed"]
    assert not result["automatic_repair_after_natural_failure_succeeded"]
    assert not result["workflow_succeeded"]


def fake_project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    paths = {"baseline": "examples/todo/todo.py", "requirement": "knowledge/requirements.md", "acceptance": "acceptance/test_todo.py"}
    for kind, relative in paths.items():
        p = root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TASK["requirement"] if kind == "requirement" else "# FIXTURE TEXT ONLY\n", encoding="utf-8")
    (root / ".env").write_text("unit-secret", encoding="utf-8")
    reference = root / "benchmarks/reference/completion.py.txt"
    reference.parent.mkdir(parents=True)
    reference.write_text("REFERENCE ANSWER MUST NOT BE FROZEN", encoding="utf-8")
    task = {**TASK, "name": "fixture", "paths": paths}
    for kind, relative in paths.items():
        task[kind + "_sha256"] = evaluation.digest((root / relative).read_text(encoding="utf-8").encode())
    settings = {"api_key": "unit-secret", "model": "unit-model", "base_url": "https://example.invalid/v1", "min_request_interval": 4.2}
    agents = [{"id": "fake", "version": 1}]
    workflows = [{"id": "development", "max_repairs": 2}]
    request = Mock(side_effect=lambda base, path, body=None: {"/tasks": [task], "/agents": agents, "/workflows": workflows}[path])
    directory = tmp_path / "evaluation"
    plan, state = evaluation.preregister(root, directory, "http://127.0.0.1:8000/api", ["completion"], 1, 0, 120, 900, settings, request)
    return root, directory, plan, state, settings, task, agents, workflows


def test_preregister_is_read_only_freezes_inputs_and_excludes_credentials_answers(tmp_path):
    root, directory, plan, state, settings, *_ = fake_project(tmp_path)
    assert len(plan["trials"]) == 1 and plan["trials"][0]["state"] == "pending"
    assert not (directory / "frozen/.env").exists()
    assert not (directory / "frozen/benchmarks/reference").exists()
    assert "unit-secret" not in (directory / "preregistration.json").read_text(encoding="utf-8")
    state["trials"][0]["state"] = "observing"
    assert plan["trials"][0]["state"] == "pending"
    evaluation.verify_frozen(root, directory, plan["source_manifest"])
    (root / "knowledge/requirements.md").write_text("changed task", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed after preregistration"):
        evaluation.verify_frozen(root, directory, plan["source_manifest"])


def test_ambiguous_submission_is_never_automatically_repeated(tmp_path):
    root, directory, plan, state, settings, *_ = fake_project(tmp_path)
    state["trials"][0]["state"] = "submitting"
    request = Mock(side_effect=AssertionError("No API calls for an uncertain submission"))
    assert evaluation.run_batch(root, directory, plan, state, settings, request) == 6
    request.assert_not_called()
    assert state["status"] == "submission_uncertain"


def test_resume_existing_run_only_observes_without_new_post(tmp_path):
    root, directory, plan, state, settings, task, *_ = fake_project(tmp_path)
    state["trials"][0].update(state="observing", run_id="b" * 32)
    value = detail()
    value["task"] = task
    value["model_config"] = plan["model_config"]
    # Use the frozen project's acceptance hash consistently in all nested
    # copies, exactly as Runtime does for the actual mounted test suite.
    for event in value["events"]:
        if event.get("type") == "tool" and event.get("data", {}).get("tool") == "run_acceptance":
            event["data"]["output"]["acceptance_sha256"] = task["acceptance_sha256"]
        if event.get("type") == "validation":
            event["data"]["test"]["acceptance_sha256"] = task["acceptance_sha256"]
    request = Mock(return_value=value)
    assert evaluation.run_batch(root, directory, plan, state, settings, request) == 0
    request.assert_called_once_with(plan["api_base"], "/runs/" + "b" * 32)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["completed_trials"] == 1 and summary["successes"] == 1
    assert summary["natural_failure_trials"] == 0
    assert summary["trials"][0]["report"]["run_duration_seconds"] == 3


def test_budget_guard_stops_before_next_billable_submission(tmp_path):
    root, directory, plan, state, settings, task, agents, workflows = fake_project(tmp_path)
    prior = {"trial": 1, "state": "complete", "report": {"model_calls": 120, "model_tokens": 200, "model_metrics_complete": True,
             "workflow_succeeded": False, "first_round_result": "test_failed", "natural_test_failure_observed": True,
             "automatic_repair_after_natural_failure_succeeded": False, "review_only_failure_observed": False}}
    state["trials"].insert(0, prior)
    plan["trials"].append({"trial": 2})
    request = Mock(side_effect=lambda base, path, body=None: {"/agents": agents, "/workflows": workflows}[path])
    assert evaluation.run_batch(root, directory, plan, state, settings, request) == 5
    assert all(call.args[1] != "/runs" for call in request.call_args_list)


def test_snapshot_mismatch_is_preserved_not_retried(tmp_path):
    root, directory, plan, state, settings, *_ = fake_project(tmp_path)
    state["trials"][0].update(state="observing", run_id="b" * 32)
    value = detail()
    value["task"]["snapshot_sha256"] = "different"
    request = Mock(return_value=value)
    with pytest.raises(RuntimeError, match="snapshot differs"):
        evaluation.run_batch(root, directory, plan, state, settings, request)
    assert request.call_count == 1


def test_trial_export_redacts_secret_and_records_candidate_artifacts(tmp_path):
    trial = {"trial": 1, "task_id": "completion", "repetition": 1}
    value = detail(repair=True)
    value["summary"] = "provider echoed unit-secret"
    evaluation.export_trial(tmp_path, trial, value, TASK, "unit-secret", 1.0)
    folder = tmp_path / "trials/01-completion-r1"
    evidence = (folder / "evidence.json").read_text(encoding="utf-8")
    assert "unit-secret" not in evidence
    assert len(list(folder.glob("*.py.txt"))) == 2
    assert json.loads((folder / "rounds.json").read_text(encoding="utf-8"))["validation_and_feedback"]


def test_post_response_loss_remains_uncertain_and_durable(tmp_path):
    root, directory, plan, state, settings, task, agents, workflows = fake_project(tmp_path)
    def request(base, path, body=None):
        if path == "/runs":
            raise RuntimeError("Local API unavailable")
        return {"/agents": agents, "/workflows": workflows,
                "/health": {"model": {"configured": True, "model": settings["model"], "base_url": settings["base_url"]},
                            "sandbox": {"available": True}}}[path]
    with pytest.raises(RuntimeError, match="unavailable"):
        evaluation.run_batch(root, directory, plan, state, settings, request)
    persisted = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "submission_uncertain"
    assert persisted["trials"][0]["state"] == "submitting"
    assert persisted["trials"][0]["run_id"] is None


def test_saved_batch_rejects_nonlocal_api_destinations():
    assert evaluation.local_api_url("http://127.0.0.1:8000/api")
    assert not evaluation.local_api_url("https://remote.example/api")
    assert not evaluation.local_api_url("http://user:secret@127.0.0.1:8000/api")
    assert not evaluation.local_api_url("http://127.0.0.1:8000/api?forward=secret")


def test_preregistration_declares_bounded_contract_retry_adapter(tmp_path):
    _, _, plan, _, settings, *_ = fake_project(tmp_path)
    config = evaluation.expected_model_config(settings)
    assert config["adapter_revision"] == plan["model_config"]["adapter_revision"] == 2
    assert config["contract_retries"] == plan["model_config"]["contract_retries"] == 1
    parameters = plan["model_request_parameters"]
    assert parameters["adapter_revision"] == 2
    assert parameters["max_contract_retries"] == 1
    assert parameters["max_tool_rounds"] == config["tool_rounds"] == 4
    assert parameters["automatic_tool_rounds"] == config["automatic_tool_rounds"] == 2
