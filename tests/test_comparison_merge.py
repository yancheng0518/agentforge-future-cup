"""Offline merge tests. Every model/container observation below is synthetic."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import merge_comparison as merger

SPEC = importlib.util.spec_from_file_location("merge_evidence_fixtures", ROOT / "tests/test_comparison_report.py")
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def reseal(directory, plan=None, state=None):
    plan = plan or read(directory / "preregistration.json")
    state = state or read(directory / "state.json")
    write(directory / "preregistration.json", plan)
    state["preregistration_sha256"] = merger.digest((directory / "preregistration.json").read_bytes())
    write(directory / "state.json", state)


def export(directory, row, detail, ledger, config):
    detail = deepcopy(detail)
    ledger = deepcopy(ledger)
    detail.update(id="synthetic-run-" + str(row["trial"]), model_config=deepcopy(config))
    row.update(run_id=detail["id"], state="complete", native_status=detail["status"])
    ledger["run_id"] = detail["id"]
    for request in ledger["requests"]:
        request.update(run_id=detail["id"])
        payload = {"model": config["model"], "temperature": 0.1, "max_tokens": 4096, "messages": []}
        body = json.dumps(payload).encode()
        request.update(payload_sha256=merger.digest(body), payload_bytes=len(body))
        archive = directory / "requests" / detail["id"] / (request["request_id"] + ".json")
        write(archive, {"request": deepcopy(request), "payload": payload, "response": None})
        request.update(artifact_path=archive.relative_to(directory).as_posix(), artifact_sha256=merger.digest(archive.read_bytes()))
    folder = directory / "trials" / str(row["trial"])
    folder.mkdir(parents=True)
    candidates = []
    for event in detail["events"]:
        output = event.get("data", {}).get("output")
        if event["type"] == "agent" and isinstance(output, dict) and output.get("files"):
            source = output["files"][0]["content"]
            path = folder / ("candidate-" + str(event["id"]) + ".txt")
            path.write_bytes(source.encode("utf-8"))
            candidates.append({"event_id": event["id"], "node_id": event["node_id"], "path": path.name,
                               "source_sha256": merger.digest(path.read_bytes()), "redacted": False})
    report = merger.classify(detail, fixtures.TASK, ledger)
    evidence = {"trial": deepcopy(row), "run": detail, "budget_ledger": ledger,
                "candidate_artifacts": candidates, "report": report}
    write(folder / "evidence.json", evidence)
    write(folder / "classification.json", report)
    row.update(evidence_path=(folder / "evidence.json").relative_to(directory).as_posix(), report=report)


def make_phases(tmp_path):
    parent, child = tmp_path / "parent", tmp_path / "child"
    model = {"model": "same-model", "temperature": 0.1, "sandbox_image": "sha256:" + "d" * 64}
    rows = [{"trial": i, "task_id": "completion", "arm": arm, "repetition": 1,
             "block": i, "arm_position": 1, "state": "pending", "run_id": None}
            for i, arm in enumerate(("fixed_serial", "single_direct", "single_direct", "fixed_serial"), 1)]
    common = {"schema": "agentforge-paired-three-arm-v1", "task_order": ["completion"],
              "task_catalog": {"completion": fixtures.TASK}, "arms": {"single_direct": {}, "fixed_serial": {}},
              "repetitions": 1, "model_config": model, "model_settings": {"model": "same-model"},
              "docker_image_id": model["sandbox_image"], "candidate_origin": "live_model_development", "fault_injection": False,
              "limits": {"http_requests_per_trial": 24, "output_tokens_per_request": 4096, "utf8_payload_bytes_per_request": 180000},
              "controls": {"controlled_seed_failures": 4, "controlled_seed_successes": 4}}
    manifests = []
    for directory in (parent, child):
        manifest = {}
        source = {p: (ROOT / p).read_bytes() for p in ("scripts/comparison_report.py", "backend/agentforge/engine.py")}
        source["scripts/evaluate_tasks.py"] = b"old observer" if directory == parent else b"new observer"
        if directory == child:
            source["scripts/merge_comparison.py"] = (ROOT / "scripts/merge_comparison.py").read_bytes()
        for relative, raw in source.items():
            path = directory / "frozen" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            manifest[relative] = {"sha256": merger.digest(raw), "bytes": len(raw)}
        manifests.append(manifest)
    parent_plan = {**deepcopy(common), "batch_id": "parent", "trials": deepcopy(rows), "source_manifest": manifests[0],
                   "source_manifest_sha256": merger.digest(merger.canonical(manifests[0]))}
    original = deepcopy(rows)
    # The complete Docker result survives a later HTTP failure in the tester.
    detail, ledger = fixtures.fixture("fixed_serial", tester_failure=True)
    last = ledger["requests"][-1]
    last.update(status="failed", usage={}, metrics_complete=False, error_type="RuntimeErrorSafe")
    ledger.update(metrics_complete=False, model_tokens_known=7)
    agent = [e["data"] for e in detail["events"] if e["type"] == "agent"][-1]
    agent.update(error="Model HTTP 429", tokens=0, metrics_complete=False, contract_retries=0, model_diagnostics=[])
    detail.update(model_tokens=7, model_metrics_complete=False)
    export(parent, original[0], detail, ledger, model)
    # The local observer stops after a real candidate write and before a test result.
    detail, ledger = fixtures.fixture()
    detail.update(events=detail["events"][:2], steps=detail["steps"][:1], snapshots=[], context={}, status="blocked")
    detail["events"].append({"id": 3, "type": "tool", "node_id": "round-0/verify/test", "data":
                             {"tool": "run_acceptance", "status": "blocked", "source": "runtime"}})
    export(parent, original[1], detail, ledger, model)
    for row in original[2:]:
        row["state"] = "not_submitted"
    parent_state = {"batch_id": "parent", "status": "blocked", "trials": original, "error": "[WinError 5] state.json.pending -> state.json"}
    reseal(parent, parent_plan, parent_state)
    changes = {key: {"before": manifests[0].get(key), "after": manifests[1].get(key)}
               for key in sorted(manifests[0].keys() | manifests[1].keys()) if manifests[0].get(key) != manifests[1].get(key)}
    child_plan = {**deepcopy(common), "batch_id": "child", "trials": deepcopy(rows[2:]), "source_manifest": manifests[1],
                  "source_manifest_sha256": merger.digest(merger.canonical(manifests[1])), "continuation":
                  {"parent_batch": "parent", "parent_preregistration_sha256": merger.digest((parent / "preregistration.json").read_bytes()),
                   "parent_state_sha256": merger.digest((parent / "state.json").read_bytes()), "retained_parent_trials": [1, 2],
                   "selected_trial_ids": [3, 4], "source_changes": changes}}
    continued = deepcopy(rows[2:])
    export(child, continued[0], *fixtures.fixture(repair="natural"), model)
    export(child, continued[1], *fixtures.fixture("fixed_serial", passed=False), model)
    reseal(child, child_plan, {"batch_id": "child", "status": "completed", "trials": continued})
    return parent, child


def hashes(directory):
    return {p.relative_to(directory).as_posix(): merger.digest(p.read_bytes()) for p in directory.rglob("*") if p.is_file()}


def test_merges_all_slots_reclassifies_and_preserves_parent_state(tmp_path):
    parent, child = make_phases(tmp_path)
    before = hashes(parent)
    result = merger.merge(parent, child)
    assert result == read(child / "combined-summary.json")
    assert hashes(parent) == before
    assert result["planned_trials"] == result["finished_trials"] == 4
    assert result["all_original_planned_slots_retained"]
    assert [r["trial"] for r in result["trials"]] == [1, 2, 3, 4]
    assert result["http_status_failures"] == {"429": 1}
    first, second, third, fourth = result["trials"]
    assert first["report"]["primary_success"] and not first["report"]["strategy_completed"]
    assert first["original_native_status"] == "failed"
    assert second["state"] == second["original_state"] == "complete"
    assert second["original_native_status"] == "blocked"
    assert second["failure_attribution"]["local_observer_interruption_inferred"]
    assert not second["failure_attribution"]["natural_failure"]
    assert result["stage_break"]["affected_trial_ids"] == [2]
    assert third["report"]["natural_failure_repair_succeeded"]
    assert fourth["failure_attribution"]["natural_failure"]
    assert not fourth["failure_attribution"]["infrastructure_items"]
    assert all(p["preflight_controls_in_live_denominator"] is False for p in result["phases"])
    assert sum(p["included_trials"] for p in result["phases"]) == 4
    assert all(a["reclassification_equal"] for a in result["evidence_audits"])
    with pytest.raises(FileExistsError):
        merger.merge(parent, child)


@pytest.mark.parametrize("condition", ["model_config", "model_settings", "task_catalog", "limits", "docker_image_id", "arms", "fault_injection"])
def test_rejects_changed_experimental_conditions(tmp_path, condition):
    parent, child = make_phases(tmp_path)
    plan = read(child / "preregistration.json")
    plan[condition] = "tampered"
    reseal(child, plan)
    with pytest.raises(ValueError, match="Frozen condition changed"):
        merger.merge(parent, child)
    assert not (child / "combined-summary.json").exists()


@pytest.mark.parametrize("artifact", ["parent_state", "parent_plan", "frozen", "classifier", "candidate", "request", "classification", "state_report", "evidence_report"])
def test_rejects_tampered_hashes_artifacts_or_reports(tmp_path, artifact):
    parent, child = make_phases(tmp_path)
    paths = {"parent_state": parent / "state.json", "parent_plan": parent / "preregistration.json",
             "frozen": parent / "frozen/scripts/evaluate_tasks.py", "classifier": child / "frozen/scripts/comparison_report.py",
             "candidate": parent / "trials/1/candidate-1.txt", "request": next((parent / "requests").rglob("*.json"))}
    if artifact in paths:
        path = paths[artifact]
        path.write_bytes(path.read_bytes() + b" ")
    elif artifact == "classification":
        path = parent / "trials/1/classification.json"
        value = read(path)
        value["primary_success"] = False
        write(path, value)
    elif artifact == "state_report":
        value = read(child / "state.json")
        value["trials"][0]["report"]["model_calls"] = 999
        write(child / "state.json", value)
    else:
        path = child / "trials/3/evidence.json"
        value = read(path)
        value["report"]["primary_success"] = False
        write(path, value)
    with pytest.raises(ValueError):
        merger.merge(parent, child)
    assert not (child / "combined-summary.json").exists()


@pytest.mark.parametrize("change", ["undeclared_source", "forbidden_source", "repeat_parent", "duplicate_run", "missing_slot", "wrong_parent", "running_child"])
def test_rejects_invalid_continuation_selection_and_amendments(tmp_path, change):
    parent, child = make_phases(tmp_path)
    plan, state = read(child / "preregistration.json"), read(child / "state.json")
    if change == "undeclared_source":
        plan["continuation"]["source_changes"] = {}
    elif change == "forbidden_source":
        relative = "backend/agentforge/runtime.py"
        raw = b"changed transport"
        path = child / "frozen" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        item = {"sha256": merger.digest(raw), "bytes": len(raw)}
        plan["source_manifest"][relative] = item
        plan["source_manifest_sha256"] = merger.digest(merger.canonical(plan["source_manifest"]))
        plan["continuation"]["source_changes"][relative] = {"before": None, "after": item}
    elif change == "repeat_parent":
        plan["trials"][0]["trial"] = state["trials"][0]["trial"] = 1
    elif change == "duplicate_run":
        state["trials"][0]["run_id"] = "synthetic-run-1"
    elif change == "missing_slot":
        plan["trials"].pop()
        state["trials"].pop()
    elif change == "wrong_parent":
        plan["continuation"]["parent_batch"] = "unrelated"
    else:
        state["status"] = "running"
    reseal(child, plan, state)
    with pytest.raises(ValueError):
        merger.merge(parent, child)


def test_stopped_child_keeps_unsubmitted_rows_in_denominator(tmp_path):
    parent, child = make_phases(tmp_path)
    state = read(child / "state.json")
    row = state["trials"][1]
    state["trials"][1] = {**merger.identity(row), "state": "not_submitted", "run_id": None}
    state["status"] = "blocked"
    reseal(child, state=state)
    result = merger.merge(parent, child)
    assert result["planned_trials"] == 4 and result["finished_trials"] == 3
    assert result["by_arm"]["fixed_serial"]["planned"] == 2
    assert result["by_arm"]["fixed_serial"]["not_observed"] == 1
    assert result["trials"][-1]["report"] is None


def test_output_must_stay_in_child_and_never_overwrite_parent(tmp_path):
    parent, child = make_phases(tmp_path)
    before = hashes(parent)
    with pytest.raises(ValueError, match="Output must be child"):
        merger.merge(parent, child, parent / "combined-summary.json")
    assert hashes(parent) == before


def test_attribution_separates_budget_format_http_and_missing_usage():
    run, ledger = fixtures.fixture(tester_failure=True)
    ledger["denials"] = [{"reason": "request_limit", "sent": False}]
    ledger["requests"][-1].update(status="failed", usage={}, metrics_complete=False, error_type="TimeoutError")
    report = {"natural_test_failure_observed": False, "unresolved_protocol_failure": True,
              "contract_retries": 1, "rounds": []}
    result = merger.failure_items(run, ledger, report)
    assert result["http_status_failures"] == {}
    assert len(result["transport_failures"]) == 1
    assert result["transport_failures"][0]["http_status"] is None
    assert result["budget_denials_before_http"] == ledger["denials"]
    assert result["format_retries"] == 1 and result["unresolved_protocol_failure"]
    assert not result["natural_failure"]


@pytest.mark.parametrize("path", ["../outside.json", "D:/outside.json", "/outside.json", "a/../../outside.json"])
def test_artifact_paths_cannot_escape_phase(tmp_path, path):
    with pytest.raises(ValueError):
        merger.inside(tmp_path, path)
