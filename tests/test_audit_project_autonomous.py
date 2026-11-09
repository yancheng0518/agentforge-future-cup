"""Synthetic project audit evidence. No models, candidates, or containers run."""
from copy import deepcopy
import json
import sqlite3

import pytest

from test_audit_autonomous import AUDIT, base_run, write_db, write_json


def suite(label):
    # Deliberately reuse the class and method names across revision namespaces.
    return ("import unittest\nfrom bridge import call\nclass Acceptance(unittest.TestCase):\n"
            + "".join(f"    def test_{i}(self):\n        self.assertEqual(call('x', 'value', {i}), {i})  # {label}\n"
                      for i in range(3)))


def merged(parts):
    return ("import unittest\nimport types\n_AGENTFORGE_SUITE_PARTS = "
            + repr([{"source": p, "count": 3} for p in parts])
            + "\ndef load_tests(loader, tests, pattern):\n    combined = unittest.TestSuite()\n"
            + "    for i, part in enumerate(_AGENTFORGE_SUITE_PARTS):\n"
            + "        module = types.ModuleType('part_' + str(i))\n"
            + "        exec(part['source'], module.__dict__)\n"
            + "        combined.addTests(loader.loadTestsFromModule(module))\n"
            + "    return combined\n")


def save_fixture(data, run, events):
    """Persist both evidence forms together so each tamper tests its own binding."""
    with sqlite3.connect(data / "agentforge.sqlite3") as db:
        db.execute("UPDATE runs SET body=? WHERE id=?", (json.dumps(run), run["id"]))
        db.execute("DELETE FROM events WHERE run_id=?", (run["id"],))
        for event in events:
            db.execute("INSERT INTO events VALUES(?,?)", (run["id"], json.dumps(event)))
    write_json(data / "runs" / run["id"] / "evidence.json",
               {"run": run, "events": events, "steps": [], "snapshots": []})


def project_fixture(data, *, tool=None, baseline_passed=False, prior_rounds=1):
    """Produce internal evidence, expressly not a claim of real execution."""
    run = base_run("project-evidence-fixture")
    run.update(requirement="Synthetic requirement for project revision", status="succeeded",
               summary="Synthetic successful delivery", candidate_origin="natural_candidate")
    base = data / "runs" / run["id"]
    before = {"x.py": "def value(x):\n    return x\n"}
    after = {"x.py": "def value(x):\n    return x  # synthetic edit\n"}
    source_hash = lambda files: AUDIT.digest(AUDIT.canonical(files))
    old_parts = [suite(f"round-{i}") for i in range(prior_rounds)]
    new_suite = suite("new-requirement")
    previous_suite = old_parts[0] if len(old_parts) == 1 else merged(old_parts)
    combined = merged([*old_parts, new_suite])
    test_count = 3 * (prior_rounds + 1)
    revision = {"id": "revision-1", "project_id": "project-1", "number": prior_rounds,
                "files": before, "source_sha256": source_hash(before), "acceptance": previous_suite}
    profile = {"id": "profile-1", "version": 2, "name": "Synthetic coder", "description": "Frozen guidance",
               "prompt": "Preserve public behavior", "capabilities": ["edit"], "enabled": True}
    profile["sha256"] = source_hash({k: profile[k] for k in ("name", "description", "prompt", "capabilities", "enabled")})
    document = {"id": "doc-1", "version": 3, "title": "Synthetic project contract", "content": "Keep function value(x) compatible."}
    document["sha256"] = source_hash({k: document[k] for k in ("title", "content")})
    profiles, documents = [profile], [document]
    configuration_hash = source_hash({"profiles": profiles, "knowledge": documents})
    match = {**{k: document[k] for k in ("id", "version", "title", "sha256")},
             "citation": "knowledge:doc-1@3", "excerpt_start": 0, "excerpt": document["content"], "score": 1}
    configuration = {"profiles": [{k: profile[k] for k in ("id", "version", "name", "description", "capabilities", "sha256", "enabled")}],
                     "selected_profile": None, "knowledge": [match]}
    manifest = {"regression_test_count": 3 * prior_rounds, "new_test_count": 3, "total_test_count": test_count,
                "parts": [{"sha256": AUDIT.digest(p), "count": 3,
                           "origin": "regression" if i < prior_rounds else "new_requirement"}
                          for i, p in enumerate([*old_parts, new_suite])]}
    proposed = {"status": "ready", "summary": "Synthetic intake", "specification": "Keep value(x) compatible",
                "assumptions": [], "files": {}, "acceptance": new_suite}
    bundle = {"requirement": run["requirement"], "files": before, "acceptance": combined,
              "provenance": {"kind": "project_revision", "acceptance_origin": "model_generated"}}
    run.update(project_context=revision, project_id=revision["project_id"], base_revision_id=revision["id"],
               profile_snapshot=profiles, knowledge_snapshot=documents, configuration_sha256=configuration_hash,
               bundle=bundle, bundle_sha256=AUDIT.digest(json.dumps(bundle, ensure_ascii=False, sort_keys=True)),
               intake={"status": "completed", "specification": proposed["specification"], "acceptance_origin": "model_generated",
                       "regression_manifest": manifest}, acceptance_manifest=manifest,
               intake_proposals=[{"index": 1, "timestamp": "synthetic", **deepcopy(proposed)}])
    write_json(base / "bundle.json", bundle)
    for relative, content in {"candidate/x.py": after["x.py"], "trusted/test_acceptance.py": combined,
                              "runner/runner.py": "# synthetic runner: never executed\n"}.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    sandbox_manifest = {"bundle_sha256": source_hash(bundle), "acceptance_sha256": AUDIT.digest(combined),
                        "baseline_sha256": source_hash(before),
                        "runner_sha256": {"runner.py": AUDIT.digest("# synthetic runner: never executed\n")}}
    write_json(base / "autonomous_manifest.json", sandbox_manifest)
    events = [{"type": "project_revision_frozen", "data": {
        "project_id": revision["project_id"], "revision_id": revision["id"], "number": revision["number"],
        "source_sha256": revision["source_sha256"], "configuration_sha256": configuration_hash,
        "profiles": [{k: profile[k] for k in ("id", "version", "sha256", "enabled")}],
        "knowledge": [{k: document[k] for k in ("id", "version", "sha256")}]}}]
    records = []

    def request(role, output, **context):
        record = {"run_id": run["id"], "sequence": len(records) + 1, "role": role, "task_id": None,
                  "status": "accepted", "format_retry": False, "usage_known": True,
                  "usage": {"total_tokens": 20}, "budget_charge_tokens": 20, "reserved_budget_tokens": 100,
                  "content": json.dumps(output), "output": deepcopy(output),
                  "configuration_sha256": configuration_hash, "profile": None, "profile_model_policy": "inherits_run_model",
                  "knowledge_references": [{k: match[k] for k in ("id", "version", "sha256", "citation")}],
                  "request": {"model": run["model_config"]["model"], "messages": [
                      {"role": "system", "content": "Synthetic evidence, never sent"},
                      {"role": "user", "content": json.dumps({"context": {
                          "configuration": deepcopy(configuration), "original_requirement": run["requirement"], **context}})}]}}
        records.append(record)
        events.extend([{"type": "knowledge_retrieved", "node_id": None, "data": {"role": role, "matches": deepcopy([match])}},
                       {"type": "model_request", "data": {"sequence": record["sequence"]}}])
        return record

    def decision(output):
        request("main", output)
        saved = {"index": len(run["controller_decisions"]) + 1, "timestamp": "synthetic", **deepcopy(output)}
        run["controller_decisions"].append(saved)
        events.append({"type": "controller_decision", "data": saved})

    def verification(files, status, origin):
        number = len(run["verifications"]) + 1
        fingerprint = source_hash(files)
        snapshot = f"evidence/{number}-validation-start.json"
        write_json(base / snapshot, {"files": files, "candidate_sha256": fingerprint})
        result = {"status": status, "isolated": True, "exit_code": 0 if status == "passed" else 1,
                  "test_count": test_count, "candidate_sha256": fingerprint,
                  "acceptance_sha256": sandbox_manifest["acceptance_sha256"], "runner_sha256": sandbox_manifest["runner_sha256"],
                  "snapshot": snapshot, "result_record": f"evidence/{number}-validation-result-synthetic.json",
                  "container_invoked": True, "image": "sha256:" + "1" * 64,
                  "attestation": {"tests": test_count, "discovered": test_count, "passed": status == "passed", "nonce": "synthetic"}}
        write_json(base / result["result_record"], result)
        result.update(candidate_hash=fingerprint, expected_test_count=test_count, candidate_origin=origin,
                      classification=origin, index=number)
        run["verifications"].append(result)
        events.append({"type": "verification", "data": result})
        run["verified_hash"] = fingerprint if status == "passed" else None

    request("intake", proposed, project=before)
    verdict = {"passed": True, "issues": [], "summary": "Synthetic consistent contract"}
    request("contract_review", verdict, proposed_contract=proposed)
    review = {**verdict, "index": 1, "timestamp": "synthetic",
              "proposal_sha256": AUDIT.digest(json.dumps(proposed, ensure_ascii=False, sort_keys=True))}
    run.update(contract_reviews=[review], contract_review=review)
    events.append({"type": "contract_review", "data": review})
    verification(before, "passed" if baseline_passed else "failed", "imported_baseline")
    decision({"action": "edit", "summary": "Synthetic direct edit", "files": after})
    patch = {"candidate_sha256": source_hash(after), "snapshot": "evidence/main-applied.json"}
    write_json(base / patch["snapshot"], {"action": "applied", "files": after, "before_sha256": source_hash(before),
                                        "candidate_sha256": source_hash(after), "changed_paths": ["x.py"]})
    events.append({"type": "main_edit", "data": {"kind": "main_edit", "summary": "Synthetic direct edit", "changed_paths": ["x.py"],
                    "before_sha256": source_hash(before), "after_sha256": source_hash(after), "patch": patch}})
    run["verified_hash"] = None
    if tool:
        decision({"action": "tool", "summary": "Synthetic application check", "tool": "run_application",
                  "arguments": {"entrypoint": "x.py", "args": []}})
        scope = {"tool": "run_application", "arguments": {"entrypoint": "x.py", "args": []},
                 "candidate_sha256": source_hash(after), "verification_evidence": False}
        events.append({"type": "tool_started", "data": {**scope, "status": "running"}})
        if tool == "success":
            events.append({"type": "tool_result", "data": {**scope, "kind": "tool_result", "status": "completed",
                           "result": {"candidate_sha256": source_hash(after), "exit_code": 0, "status": "succeeded",
                                      "isolated": True, "container_invoked": True, "data_scope": "per_run_scratch_only"}}})
        elif tool == "error":
            events.append({"type": "tool_result", "data": {**scope, "kind": "tool_result", "status": "failed",
                           "error": "Synthetic callback error", "recoverable": True}})
        elif tool in {"cancel", "timeout"}:
            events.append({"type": "tool_interrupted", "data": {**scope, "status": "blocked", "completion_unknown": True,
                           "error_type": "CancelledError" if tool == "cancel" else "TimeoutError"}})
    if tool not in {"cancel", "timeout", "unknown"}:
        decision({"action": "verify", "summary": "Synthetic independent acceptance"})
        verification(after, "passed", "natural_candidate")
        decision({"action": "finish", "summary": "Synthetic successful delivery"})
        verdict = {"passed": True, "issues": [], "summary": "Synthetic original-requirement match"}
        request("final_review", verdict, project=after)
        review = {**verdict, "index": 1, "timestamp": "synthetic", "candidate_hash": source_hash(after)}
        run.update(quality_reviews=[review], quality_review=review,
                   publication={"status": "saved", "revision_id": "revision-2", "number": prior_rounds + 1, "source_sha256": source_hash(after)})
        events.append({"type": "quality_review", "data": review})
    else:
        run.update(status="blocked", summary="Synthetic tool interrupted or unknown", quality_reviews=[], quality_review=None)
    events.append({"type": "terminal", "data": {"status": run["status"], "summary": run["summary"], "publication": run.get("publication")}})
    count = len(records)
    run["model_metrics"] = {"model_calls": count, "tokens": count * 20, "budget_tokens": count * 20, "metrics_complete": True,
                            "unknown_usage_calls": 0, "contract_retries": 0, "in_flight": 0, "reserved_tokens": 0}
    for record in records:
        write_json(base / "model_requests" / f"{record['sequence']:04d}.json", record)
    write_db(data, [run], events)
    save_fixture(data, run, events)
    return run, base, events


def assert_fails(data, message):
    result = AUDIT.audit(data)
    assert not result["passed"]
    assert any(message in issue["check"] for issue in result["issues"]), result["issues"]


@pytest.mark.parametrize("baseline_passed,prior_rounds", [(False, 1), (True, 1), (False, 2), (True, 3)])
def test_project_baselines_and_repeated_regression_suites_are_not_natural_repairs(tmp_path, baseline_passed, prior_rounds):
    data = tmp_path / "input"
    run, _, _ = project_fixture(data, baseline_passed=baseline_passed, prior_rounds=prior_rounds)
    before = {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    assert before == {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    counts = result["aggregate"]
    assert counts["imported_baseline_verifications"] == counts["main_edits"] == 1
    assert counts["imported_baseline_failures"] == int(not baseline_passed)
    assert counts["tasks"] == counts["natural_repair_runs"] == counts["generated_acceptance_repair_runs"] == 0
    assert counts["natural_candidate_failures"] == counts["generated_acceptance_candidate_failures"] == 0
    assert counts["model_calls"] == 6 and counts["known_tokens"] == 120
    assert result["runs"][0]["model_metrics"] == run["model_metrics"]


@pytest.mark.parametrize("kind", ["before_sha256", "after_sha256", "changed_paths", "snapshot"])
def test_direct_edit_requires_continuous_source_hashes_and_exact_snapshot(tmp_path, kind):
    data = tmp_path / "input"
    run, base, events = project_fixture(data)
    edit = next(e["data"] for e in events if e["type"] == "main_edit")
    if kind == "snapshot":
        snapshot = AUDIT._read_json(base / edit["patch"]["snapshot"])
        snapshot["files"]["x.py"] = "# fake source never executed\n"
        write_json(base / edit["patch"]["snapshot"], snapshot)
    else:
        edit[kind] = [] if kind == "changed_paths" else "0" * 64
    save_fixture(data, run, events)
    assert_fails(data, "Main edit")


def test_edit_invalidates_previously_passed_baseline_without_new_verification(tmp_path):
    data = tmp_path / "input"
    run, _, events = project_fixture(data, baseline_passed=True)
    run["verifications"].pop()
    events[:] = [e for e in events if not (e["type"] == "verification" and e["data"]["index"] == 2)]
    run["verified_hash"] = run["project_context"]["source_sha256"]
    save_fixture(data, run, events)
    assert_fails(data, "retained stale verification")


@pytest.mark.parametrize("tool", ["success", "error", "cancel", "timeout", "unknown"])
def test_tool_receipts_and_incomplete_outcomes_stay_distinct_from_acceptance(tmp_path, tool):
    data = tmp_path / "input"
    project_fixture(data, tool=tool)
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    counts = result["aggregate"]
    assert counts["project_tools_started"] == 1
    assert counts["project_tools_completed"] == int(tool in {"success", "error"})
    assert counts["project_tools_failed"] == int(tool == "error")
    assert counts["project_tools_interrupted"] == int(tool in {"cancel", "timeout"})
    assert counts["project_tools_unknown"] == int(tool == "unknown")
    assert counts["successful_application_tools"] == int(tool == "success")
    assert counts["passed_verifications"] == int(tool in {"success", "error"})
    assert result["runs"][0]["project_evidence"]["tool_results_are_acceptance"] is False


@pytest.mark.parametrize("tamper,expected", [
    ("missing_start", "lacks a matching start"), ("unknown_tool", "Unknown project tool"),
    ("scope", "scope differs"), ("source", "receipt belongs to another candidate"),
    ("application_source", "Application execution source hash differs"),
    ("timeout", "disguised as an ordinary result"), ("acceptance", "claims acceptance authority"),
    ("not_isolated", "lacks actual isolated execution evidence"),
])
def test_tool_corruption_does_not_become_successful_acceptance(tmp_path, tamper, expected):
    data = tmp_path / "input"
    run, _, events = project_fixture(data, tool="success")
    started = next(e["data"] for e in events if e["type"] == "tool_started")
    result = next(e["data"] for e in events if e["type"] == "tool_result")
    if tamper == "missing_start":
        events[:] = [e for e in events if e["type"] != "tool_started"]
    elif tamper == "unknown_tool":
        started["tool"] = result["tool"] = "shell"
    elif tamper == "scope":
        result["arguments"] = {"entrypoint": "another.py", "args": []}
    elif tamper == "source":
        result["result"]["candidate_sha256"] = "0" * 64
    elif tamper == "application_source":
        result["result"]["source_sha256"] = "0" * 64
    elif tamper == "timeout":
        result["result"]["timed_out"] = True
    elif tamper == "acceptance":
        result["verification_evidence"] = True
    else:
        result["result"]["isolated"] = False
    save_fixture(data, run, events)
    assert_fails(data, expected)


def test_cli_exit_zero_cannot_establish_a_verified_candidate(tmp_path):
    data = tmp_path / "input"
    run, _, events = project_fixture(data, tool="success")
    run["verifications"].pop()
    events[:] = [e for e in events if not (e["type"] == "verification" and e["data"]["index"] == 2)]
    save_fixture(data, run, events)
    assert_fails(data, "treated a tool result as acceptance")


def test_controller_must_not_continue_after_tool_cancellation(tmp_path):
    data = tmp_path / "input"
    run, _, events = project_fixture(data, tool="success")
    result = next(e for e in events if e["type"] == "tool_result")
    result["type"] = "tool_interrupted"
    result["data"].update(status="blocked", completion_unknown=True, error_type="CancelledError")
    save_fixture(data, run, events)
    assert_fails(data, "continued after a timed out or cancelled")


def test_cancelled_direct_edit_retains_unknown_source_state_without_claiming_success(tmp_path):
    data = tmp_path / "input"
    run, _, events = project_fixture(data, baseline_passed=True)
    # Interruption after verified_hash was cleared, before the edit was committed.
    cutoff = next(i for i, event in enumerate(events) if event["type"] == "main_edit")
    events[:] = events[:cutoff]
    run.update(status="blocked", summary="Synthetic interrupted mutation", verified_hash=None,
               candidate_origin="imported_baseline", quality_reviews=[], quality_review=None)
    run.pop("publication")
    run["controller_decisions"] = run["controller_decisions"][:1]
    run["verifications"] = run["verifications"][:1]
    # Retain only requests that were actually attempted before interruption.
    base = data / "runs" / run["id"]
    for number in (4, 5, 6):
        (base / "model_requests" / f"{number:04d}.json").unlink()
    run["model_metrics"].update(model_calls=3, tokens=60, budget_tokens=60)
    events.append({"type": "terminal", "data": {"status": run["status"], "summary": run["summary"], "publication": None}})
    save_fixture(data, run, events)
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    assert result["runs"][0]["project_evidence"]["source_history_complete"] is False
    assert result["runs"][0]["project_evidence"]["last_independent_verified_hash"] is None


def test_natural_candidate_cannot_be_relabelled_as_an_imported_baseline(tmp_path):
    data = tmp_path / "input"
    run, _, events = project_fixture(data)
    run["verifications"][-1].update(candidate_origin="imported_baseline", classification="imported_baseline")
    save_fixture(data, run, events)
    assert_fails(data, "origin differs from committed source-edit history")


@pytest.mark.parametrize("tamper,expected", [
    ("source", "Imported project source hash mismatch"), ("catalog", "profile catalog differs"),
    ("excerpt", "excerpt differs from frozen content"), ("knowledge_event", "lack a matching retrieval event"),
    ("model", "changed the frozen model"), ("published", "Published project source hash differs"),
    ("usage", "Persisted model metric mismatch"), ("export", "Final JSON export differs"),
])
def test_project_configuration_publication_usage_and_export_bindings(tmp_path, tamper, expected):
    data = tmp_path / "input"
    run, base, events = project_fixture(data)
    path = base / "model_requests/0003.json"
    record = AUDIT._read_json(path)
    payload = json.loads(record["request"]["messages"][1]["content"])
    if tamper == "source":
        run["project_context"]["files"]["x.py"] += "# changed imported source\n"
    elif tamper == "catalog":
        payload["context"]["configuration"]["profiles"][0]["description"] = "Other guidance"
    elif tamper == "excerpt":
        payload["context"]["configuration"]["knowledge"][0]["excerpt"] = "invented"
    elif tamper == "knowledge_event":
        events[:] = [e for e in events if e["type"] != "knowledge_retrieved"]
    elif tamper == "model":
        record["request"]["model"] = "another-model"
    elif tamper == "published":
        run["publication"]["source_sha256"] = "0" * 64
    elif tamper == "usage":
        run["model_metrics"]["tokens"] += 1
    record["request"]["messages"][1]["content"] = json.dumps(payload)
    write_json(path, record)
    save_fixture(data, run, events)
    if tamper == "export":
        exported = AUDIT._read_json(base / "evidence.json")
        exported["run"]["summary"] = "Export changed"
        write_json(base / "evidence.json", exported)
    assert_fails(data, expected)


@pytest.mark.parametrize("tamper,expected", [("overwrite", "overwrote an imported source"),
                                            ("lost_regression", "lost or changed regression"),
                                            ("test_count", "omitted frozen regression")])
def test_intake_cannot_overwrite_imports_or_drop_regression_tests(tmp_path, tamper, expected):
    data = tmp_path / "input"
    run, base, events = project_fixture(data)
    if tamper == "overwrite":
        record = AUDIT._read_json(base / "model_requests/0001.json")
        record["output"]["files"] = {"x.py": "def value(x):\n    raise NotImplementedError\n"}
        record["content"] = json.dumps(record["output"])
        write_json(base / "model_requests/0001.json", record)
    elif tamper == "lost_regression":
        run["bundle"]["acceptance"] = suite("new-requirement")
    else:
        run["verifications"][-1]["test_count"] = 3
    save_fixture(data, run, events)
    assert_fails(data, expected)
