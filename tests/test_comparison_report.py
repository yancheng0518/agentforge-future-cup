"""Synthetic evidence fixtures only; no HTTP, Docker or candidate execution."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import asyncio
import shutil
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("comparison_report", ROOT / "scripts/comparison_report.py")
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)

TASK = {"id": "completion", "requirement": "Frozen fixture requirement", "expected_tests": 10,
        "snapshot_sha256": "a" * 64, "acceptance_sha256": "b" * 64}
START = datetime(2026, 11, 8, tzinfo=timezone.utc)


def fixture(arm="single_direct", repair=None, passed=True, tester_failure=False):
    events, steps, snapshots, requests = [], [], [], []
    context = {}

    def event(kind, node, data):
        value = {"id": len(events) + 1, "type": kind, "node_id": node, "data": deepcopy(data),
                 "timestamp": (START + timedelta(seconds=len(events) + 1)).isoformat()}
        events.append(value)
        return value

    def agent(node, output, status=None, error=None):
        count = len(requests) + 1
        invocation, request_id = "invoke-" + str(count), "request-" + str(count)
        aid = "single_direct" if arm == "single_direct" else "reviewer" if node.endswith("review") else "developer"
        requests.append({"sequence": count, "request_id": request_id, "invocation_id": invocation,
                         "sent": True, "status": "completed", "usage": {"total_tokens": 7}, "metrics_complete": True,
                         "model": "same-model", "max_tokens": 4096, "temperature": 0.1})
        data = {"agent_id": aid, "model_mode": "live", "model_calls": 1, "tokens": 7, "metrics_complete": True,
                "invocation_id": invocation, "request_ids": [request_id], "contract_retries": 0,
                "model_diagnostics": [], "output": output}
        if status:
            data.update(status=status, error=error)
        return event("agent", node, data)

    def candidate(number):
        source = "# INERT synthetic evidence source " + str(number)
        node = "develop" if number == 0 else f"round-{number}/fix"
        output = {"summary": "Fixture", "files": [{"path": "todo.py", "content": source}]}
        agent(node, output)
        steps.append({"id": node, "status": "succeeded", "output": deepcopy(output)})
        diff = {"files": [{"path": "todo.py", "after": source}]}
        event("tool", node, {"tool": "candidate_diff", "source": "runtime", "status": "completed", "output": diff})
        return diff

    def verify(number, diff, test_passed, gate_passed, fail_tester=False):
        scope = {"id": f"round-{number}/verify", "attempt": number, "candidate_hash": report.fingerprint(diff)}
        raw = {"status": "passed" if test_passed else "failed", "isolated": True, "exit_code": 0 if test_passed else 1,
               "acceptance_sha256": TASK["acceptance_sha256"], "task_id": TASK["id"], "expected_tests": 10,
               "stdout": "AGENTFORGE_ACCEPTANCE=" + json.dumps({"tests": 10, "passed": test_passed}),
               "stderr": "Ran 10 tests in 0.01s\n" + ("OK" if test_passed else "FAILED (failures=1)")}
        scoped = {**raw, "evidence_scope": scope}
        event("tool", scope["id"] + "/test", {"tool": "run_acceptance", "source": "runtime",
               "status": "completed" if test_passed else "failed", "output": raw})
        steps.append({"id": scope["id"] + "/test", "status": "failed" if fail_tester else "succeeded",
                      "output": None if fail_tester else {"test": deepcopy(scoped)}})
        context.update(test=deepcopy(scoped), validation_scope=scope)
        snapshots.append({"context": deepcopy(context)})
        if fail_tester:
            failed = agent(scope["id"] + "/test", None, "failed", "模型输出不符合 JSON 契约")
            failed["data"].update(contract_retries=1, model_diagnostics=[{"error": {"kind": "json_syntax"}}])
            return scoped
        review = None
        if arm != "single_direct":
            output = {"summary": "Fixture review", "passed": gate_passed, "issues": [] if gate_passed else ["Fixture issue"]}
            agent(scope["id"] + "/review", output)
            review = {**output, "evidence_scope": scope}
            steps.append({"id": scope["id"] + "/review", "status": "succeeded", "output": review})
        data = {"attempt": number, "test": scoped, "passed": gate_passed}
        if review is not None:
            data["review"] = review
        event("validation", f"round-{number}/gate", data)
        return scoped

    diff = candidate(0)
    if repair:
        scoped = verify(0, diff, repair == "review", False)
        event("feedback", "round-0/gate", {"next_attempt": 1, "test": scoped, "issues": ["Fixture failure"]})
        diff = candidate(1)
    verify(int(bool(repair)), diff, passed, passed, tester_failure)
    detail = {"id": "fixture-run", "comparison_arm": arm, "mode": "live", "workflow_id": "development",
              "task_id": TASK["id"], "task": deepcopy(TASK), "requirement": TASK["requirement"],
              "candidate_origin": "live_model_development", "fault_injection": False, "applied": False,
              "events": events, "steps": steps, "snapshots": snapshots, "context": context, "diff": diff,
              "status": "failed" if tester_failure else "succeeded" if passed else "needs_human",
              "summary": "Fixture status", "attempt": int(bool(repair)),
              "model_config": {"model": "same-model"}, "model_calls": len(requests), "model_tokens": len(requests) * 7,
              "model_metrics_complete": True, "created_at": START.isoformat(),
              "updated_at": (START + timedelta(seconds=len(events) + 1)).isoformat()}
    ledger = {"max_requests": 24, "sent_requests": len(requests), "denied_requests": 0,
              "requests": requests, "denials": [], "model_tokens_known": len(requests) * 7, "metrics_complete": True}
    return detail, ledger


@pytest.mark.parametrize("arm", report.ARMS)
def test_common_functional_endpoint_does_not_fake_single_review(arm):
    value, ledger = fixture(arm)
    result = report.classify(value, TASK, ledger)
    assert result["evidence_valid"]
    assert result["primary_success"]
    assert result["primary_result"] == "complete_verified"
    assert result["strategy_completed"]
    assert result["independent_review_observed"] is (arm != "single_direct")
    assert not result["natural_test_failure_observed"]
    assert not result["automatic_code_repair_observed"]


def test_test_passes_even_when_tester_protocol_fails_and_output_is_null():
    value, ledger = fixture("dynamic_parallel", tester_failure=True)
    result = report.classify(value, TASK, ledger)
    assert result["primary_success"]
    assert not result["strategy_completed"]
    assert result["native_status"] == "failed"
    assert result["contract_retries"] == 1
    assert result["unresolved_protocol_failure"]
    assert not result["automatic_code_repair_observed"]


def test_test_failure_is_observed_even_when_tester_output_is_null():
    value, ledger = fixture("dynamic_parallel", tester_failure=True, passed=False)
    result = report.classify(value, TASK, ledger)
    assert result["natural_test_failure_observed"]
    assert result["primary_result"] == "complete_verified"
    assert not result["primary_success"]


@pytest.mark.parametrize("arm", report.ARMS)
def test_natural_failure_feedback_and_changed_live_candidate_are_linked(arm):
    value, ledger = fixture(arm, repair="natural")
    result = report.classify(value, TASK, ledger)
    assert result["natural_test_failure_observed"]
    assert result["automatic_code_repair_observed"]
    assert result["natural_failure_repair_succeeded"]
    assert not result["review_triggered_repair_succeeded"]


def test_review_triggered_change_is_separate_from_natural_test_failure():
    value, ledger = fixture("fixed_serial", repair="review")
    result = report.classify(value, TASK, ledger)
    assert result["review_triggered_repair_succeeded"]
    assert result["review_only_failure_observed"]
    assert not result["natural_test_failure_observed"]
    assert not result["natural_failure_repair_succeeded"]


@pytest.mark.parametrize("change", ["feedback", "gate", "source", "feedback_test"])
def test_code_repair_requires_exact_failure_feedback_and_change(change):
    value, ledger = fixture(repair="natural")
    if change == "feedback":
        value["events"] = [e for e in value["events"] if e["type"] != "feedback"]
    elif change == "gate":
        value["events"] = [e for e in value["events"] if not (e["type"] == "validation" and e["node_id"] == "round-0/gate")]
    elif change == "source":
        event = next(e for e in value["events"] if e["node_id"] == "round-1/fix" and e["type"] == "agent")
        event["data"]["output"]["files"][0]["content"] = "# altered without matching write"
    else:
        next(e for e in value["events"] if e["type"] == "feedback")["data"]["test"]["exit_code"] = 125
    result = report.classify(value, TASK, ledger)
    assert not result["automatic_code_repair_observed"]
    assert not result["natural_failure_repair_succeeded"]


@pytest.mark.parametrize("change", ["seed", "origin", "mock", "task_sha", "requirement", "applied", "single_agent_id"])
def test_uncontrolled_or_seeded_candidates_cannot_claim_primary_or_natural(change):
    value, ledger = fixture(repair="natural")
    if change == "seed":
        value["fault_injection"] = True
    elif change == "origin":
        value["candidate_origin"] = "declared_synthetic_developer_fixture"
    elif change == "mock":
        value["mode"] = "mock"
    elif change == "task_sha":
        value["task"]["snapshot_sha256"] = "c" * 64
    elif change == "requirement":
        value["requirement"] = "different contract"
    elif change == "applied":
        value["applied"] = True
    else:
        next(e for e in value["events"] if e["type"] == "agent")["data"]["agent_id"] = "another_agent"
    result = report.classify(value, TASK, ledger)
    assert not result["primary_success"]
    assert not result["natural_test_failure_observed"]
    assert result["primary_result"] == "ineligible"


@pytest.mark.parametrize("change", ["scope", "acceptance_hash", "test_count", "runtime_source", "missing_write", "invalidated", "current_diff", "event_order"])
def test_missing_or_forged_test_chain_cannot_pass(change):
    value, ledger = fixture()
    test_event = next(e for e in value["events"] if e["type"] == "tool" and e["data"].get("tool") == "run_acceptance")
    if change == "scope":
        for step in value["steps"]:
            if step["id"].endswith("/test"):
                step["output"] = None
        value["context"] = {}
        value["snapshots"] = []
        value["events"] = [e for e in value["events"] if e["type"] != "validation"]
    elif change == "acceptance_hash":
        test_event["data"]["output"]["acceptance_sha256"] = "c" * 64
    elif change == "test_count":
        test_event["data"]["output"]["stderr"] = "Ran 9 tests in 0.01s"
    elif change == "runtime_source":
        test_event["data"]["source"] = "model"
    elif change == "missing_write":
        value["events"] = [e for e in value["events"] if e["data"].get("tool") != "candidate_diff"]
    elif change == "invalidated":
        value["events"].append({"id": 999, "type": "validation", "node_id": "round-0/verify", "data": {}})
    elif change == "current_diff":
        value["diff"]["files"][0]["after"] = "# externally changed"
    else:
        value["events"] = list(reversed(value["events"]))
    result = report.classify(value, TASK, ledger)
    assert not result["primary_success"]


@pytest.mark.parametrize("change", ["request_id", "double_request", "run_calls", "agent_tokens", "unknown_usage", "wrong_model", "unsent_record"])
def test_usage_requires_event_and_http_ledger_agreement(change):
    value, ledger = fixture()
    agent = next(e for e in value["events"] if e["type"] == "agent")["data"]
    if change == "request_id":
        agent["request_ids"] = ["not-sent"]
    elif change == "double_request":
        agent["request_ids"] *= 2
    elif change == "run_calls":
        value["model_calls"] += 1
    elif change == "agent_tokens":
        agent["tokens"] += 1
    elif change == "unknown_usage":
        ledger["requests"][0]["usage"] = None
    elif change == "wrong_model":
        ledger["requests"][0]["model"] = "another-model"
    else:
        ledger["requests"][0]["sent"] = False
    result = report.classify(value, TASK, ledger)
    assert not result["budget_audit"]["verified"]
    assert not result["primary_success"]


def test_budget_denial_does_not_add_an_http_request_or_natural_failure():
    value, ledger = fixture()
    ledger["denials"] = [{"invocation_id": "denied", "reason": "request_limit"}]
    ledger["denied_requests"] = 1
    result = report.classify(value, TASK, ledger)
    assert result["budget_audit"]["verified"]
    assert result["model_calls"] == 1
    assert result["budget_denials"] == 1
    assert not result["natural_test_failure_observed"]


def test_unknown_http_usage_stays_unknown_when_all_records_agree():
    value, ledger = fixture()
    agent = next(e for e in value["events"] if e["type"] == "agent")["data"]
    ledger["requests"][0].update(usage={}, metrics_complete=False)
    ledger.update(model_tokens_known=0, metrics_complete=False)
    agent.update(tokens=0, metrics_complete=False)
    value.update(model_tokens=0, model_metrics_complete=False)
    result = report.classify(value, TASK, ledger)
    assert result["budget_audit"]["verified"]
    assert not result["model_metrics_complete"]
    assert result["model_tokens_known"] == 0


def test_archive_audit_failure_retains_known_usage_but_not_verified_outcome():
    value, ledger = fixture()
    ledger["audit_complete"] = False
    result = report.classify(value, TASK, ledger)
    assert not result["evidence_valid"]
    assert not result["primary_success"]
    assert result["model_calls"] == 1
    assert result["model_tokens_known"] == 7
    assert result["model_metrics_complete"] is True
    assert "request_archive_audit_incomplete" in result["budget_audit"]["errors"]


@pytest.mark.parametrize("arm", report.ARMS)
def test_real_engine_shape_with_fake_http_and_fake_container_is_classifiable(tmp_path, arm):
    # This checks integration of the *real* experimental controller/ledger with
    # the report schema. The generated text is inert and never executed.
    from agentforge.comparison import BudgetRuntime, ComparisonEngine, SingleDirectEngine
    from agentforge.runtime import Runtime
    from agentforge.storage import Store

    for directory in ("examples", "knowledge", "acceptance"):
        shutil.copytree(ROOT / directory, tmp_path / directory)
    settings = {"api_key": "unit-not-valid-key", "key_source": "fixture", "base_url": "https://example.invalid/v1",
                "model": "same-model", "timeout": 5, "min_request_interval": 0,
                "docker_backend": "native", "image": "never-executed:fixture"}
    with patch("agentforge.runtime._settings", return_value=settings):
        runtime = BudgetRuntime(tmp_path, tmp_path / "data")
    task = runtime.snapshot_task("completion")
    store = Store(tmp_path / "fixture.sqlite3")
    engine = SingleDirectEngine(store, runtime) if arm == "single_direct" else ComparisonEngine(store, runtime, arm)
    run = engine.create_run(task["requirement"], task_id="completion")

    def model(payload):
        prompt = payload["messages"][0]["content"]
        if "developer Agent" in prompt or "fixer Agent" in prompt:
            output = {"summary": "inert fixture", "files": [{"path": "todo.py", "content": "# inert fixture source\n"}]}
        elif "planner Agent" in prompt:
            output = {"summary": "fixture plan", "tasks": []}
        elif "tester Agent" in prompt:
            output = {"summary": "fixture test commentary", "tests": []}
        else:
            output = {"summary": "fixture review", "passed": True, "issues": []}
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(output)}}],
                "usage": {"total_tokens": 7}}

    test = {"status": "passed", "isolated": True, "exit_code": 0, "task_id": "completion", "expected_tests": 10,
            "acceptance_sha256": task["acceptance_sha256"], "stdout": 'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": true}\n',
            "stderr": "Ran 10 tests in 0.01s\nOK\n"}

    async def scenario():
        with patch.object(Runtime, "_post", side_effect=model), patch.object(runtime, "test", AsyncMock(return_value=test)), \
                patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No real HTTP")), \
                patch("asyncio.create_subprocess_exec", side_effect=AssertionError("No subprocess/candidate execution")):
            await engine.execute(run["id"])
        detail = store.run(run["id"])
        detail.update(events=store.events(run["id"]), steps=store.steps(run["id"]),
                      snapshots=store.snapshots(run["id"], include_context=True), diff=await runtime.diff(run["id"]))
        result = report.classify(detail, task, runtime.budget_record(run["id"]))
        assert result["budget_audit"]["verified"], result
        assert result["primary_success"], result
        assert result["strategy_completed"], result
        assert result["independent_review_observed"] is (arm != "single_direct")

    asyncio.run(scenario())


def test_running_nulls_are_observable_without_terminal_success():
    value, ledger = fixture()
    value["status"] = "running"
    value["steps"].append({"id": "round-0/verify", "status": "running", "output": None})
    value["events"].append({"id": 999, "type": "status", "node_id": None, "data": None})
    result = report.classify(value, TASK, ledger)
    assert not result["primary_success"]
    assert result["functional_test_passed"]
    assert not result["terminal"]


def test_missing_pending_reports_and_all_failures_stay_in_denominator():
    plan = {"batch_id": "fixture", "trials": [
        {"trial": i + 1, "task_id": "completion", "arm": arm, "repetition": 1}
        for i, arm in enumerate(report.ARMS)]}
    value, ledger = fixture(passed=False)
    failed = report.classify(value, TASK, ledger)
    summary = report.summarize(plan, [{"trial": 1, "state": "complete", "run_id": "fixture", "report": failed}])
    assert summary["planned_trials"] == 3
    assert summary["finished_trials"] == 1
    assert len(summary["trials"]) == 3
    for arm in report.ARMS:
        assert summary["by_arm"][arm]["planned"] == 1
        assert summary["by_arm"][arm]["primary_successes"] == 0
        assert summary["by_arm"][arm]["primary_non_successes_including_unobserved"] == 1
