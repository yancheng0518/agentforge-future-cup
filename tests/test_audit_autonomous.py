"""Synthetic evidence-only audit tests; never execute candidates or call services."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

SPEC = importlib.util.spec_from_file_location("audit_autonomous", Path(__file__).resolve().parents[1] / "scripts/audit_autonomous.py")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_db(data, runs, events=None):
    data.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data / "agentforge.sqlite3") as db:
        db.executescript("CREATE TABLE runs(id TEXT,body TEXT); CREATE TABLE events(run_id TEXT,body TEXT); CREATE TABLE steps(run_id TEXT,body TEXT); CREATE TABLE snapshots(run_id TEXT,body TEXT);")
        for run in runs:
            db.execute("INSERT INTO runs VALUES(?,?)", (run["id"], json.dumps(run)))
        for event in events or []:
            db.execute("INSERT INTO events VALUES(?,?)", (runs[0]["id"], json.dumps(event)))


def base_run(identifier="fixture-1"):
    bundle = {"requirement": "Synthetic audit evidence only", "files": {"x.py": "# never executed\n"},
              "acceptance": "# synthetic fixture, not a real test suite\n", "provenance": {"kind": "natural"}}
    return {"id": identifier, "engine": "model-directed-v1", "mode": "live", "status": "blocked",
            "bundle": bundle, "bundle_sha256": AUDIT.digest(json.dumps(bundle, ensure_ascii=False, sort_keys=True)),
            "controller_decisions": [], "task_graph": [], "verifications": [],
            "model_metrics": {"model_calls": 0, "tokens": 0, "metrics_complete": True}, "verified_hash": None,
            "model_config": {"model": "fake-evidence-only"}, "budget": {"max_calls": 10}}


def complete_fixture(data):
    run = base_run()
    run["status"] = "succeeded"
    base = data / "runs" / run["id"]
    bundle = run["bundle"]
    write_json(base / "bundle.json", bundle)
    (base / "candidate").mkdir(parents=True)
    (base / "candidate/x.py").write_text(bundle["files"]["x.py"], encoding="utf-8")
    (base / "trusted").mkdir()
    (base / "trusted/test_acceptance.py").write_text(bundle["acceptance"], encoding="utf-8")
    (base / "runner").mkdir()
    (base / "runner/runner.py").write_text("# synthetic runner\n", encoding="utf-8")
    manifest = {"bundle_sha256": AUDIT.digest(AUDIT.canonical(bundle)),
                "acceptance_sha256": AUDIT.digest(bundle["acceptance"]),
                "baseline_sha256": AUDIT.digest(AUDIT.canonical(bundle["files"])),
                "runner_sha256": {"runner.py": AUDIT.digest("# synthetic runner\n")}}
    write_json(base / "autonomous_manifest.json", manifest)
    fingerprint = AUDIT.digest(AUDIT.canonical(bundle["files"]))
    run["verified_hash"] = fingerprint
    snapshot = "evidence/1-validation-start.json"
    write_json(base / snapshot, {"files": bundle["files"], "candidate_sha256": fingerprint})
    result = {"status": "passed", "isolated": True, "exit_code": 0, "test_count": 1,
              "candidate_sha256": fingerprint, "acceptance_sha256": manifest["acceptance_sha256"],
              "runner_sha256": manifest["runner_sha256"], "snapshot": snapshot,
              "result_record": "evidence/2-validation-result-synthetic.json",
              "container_invoked": True, "image": "sha256:" + "1" * 64,
              "attestation": {"tests": 1, "discovered": 1, "passed": True, "nonce": "synthetic"}}
    write_json(base / result["result_record"], result)
    run["verifications"] = [{**result, "candidate_hash": fingerprint, "candidate_origin": "baseline", "classification": "input_baseline", "index": 1}]
    events = []
    for index, action in enumerate(("verify", "finish"), 1):
        output = {"action": action, "summary": "Synthetic evidence, not a real model output"}
        record = {"run_id": run["id"], "sequence": index, "role": "main", "task_id": None,
                  "status": "accepted", "format_retry": False, "usage_known": True,
                  "usage": {"total_tokens": 20}, "budget_charge_tokens": 20, "reserved_budget_tokens": 100,
                  "content": json.dumps(output), "output": output}
        write_json(base / "model_requests" / f"{index:04d}.json", record)
        decision = {"index": index, "timestamp": "synthetic", **output}
        run["controller_decisions"].append(decision)
        events.extend([{"type": "model_request", "data": {"sequence": index}},
                       {"type": "controller_decision", "data": decision}])
    events.append({"type": "verification", "data": run["verifications"][0]})
    run["model_metrics"] = {"model_calls": 2, "tokens": 40, "budget_tokens": 40, "metrics_complete": True,
                            "unknown_usage_calls": 0, "contract_retries": 0, "in_flight": 0, "reserved_tokens": 0}
    write_db(data, [run], events)
    return run, base


def test_complete_synthetic_evidence_passes_without_mutating_inputs(tmp_path):
    data = tmp_path / "input"
    complete_fixture(data)
    before = {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    result = AUDIT.audit(data)
    after = {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    assert result["passed"], result["issues"]
    assert result["aggregate"]["runs"] == 1 and result["aggregate"]["model_calls"] == 2
    assert result["aggregate"]["known_tokens"] == 40
    assert result["aggregate"]["natural_candidate_failures"] == 0
    assert before == after


def test_audit_reads_more_than_100_runs_including_unexecuted_failures(tmp_path):
    data = tmp_path / "input"
    write_db(data, [base_run(f"record-{index}") for index in range(107)])
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    assert result["aggregate"]["runs"] == len(result["runs"]) == 107
    assert result["aggregate"]["non_successful_runs"] == 107


@pytest.mark.parametrize("tamper", ["tokens", "candidate", "acceptance", "runner", "decision", "request"])
def test_audit_detects_independent_evidence_corruption(tmp_path, tamper):
    data = tmp_path / "input"
    run, base = complete_fixture(data)
    if tamper in {"tokens", "decision"}:
        if tamper == "tokens":
            run["model_metrics"]["tokens"] += 1
        else:
            run["controller_decisions"][0]["summary"] = "Rewritten decision"
        with sqlite3.connect(data / "agentforge.sqlite3") as db:
            db.execute("UPDATE runs SET body=?", (json.dumps(run),))
    elif tamper == "request":
        (base / "model_requests/0001.json").unlink()
    else:
        path = {"candidate": "candidate/x.py", "acceptance": "trusted/test_acceptance.py", "runner": "runner/runner.py"}[tamper]
        (base / path).write_text("# rewritten\n", encoding="utf-8")
    result = AUDIT.audit(data)
    assert not result["passed"]
    assert any(i["severity"] == "error" for i in result["issues"])


def test_unknown_transport_failure_and_format_retry_stay_separate(tmp_path):
    data = tmp_path / "input"
    run, base = complete_fixture(data)
    failed = {"run_id": run["id"], "sequence": 3, "role": "worker", "task_id": "never-completed",
              "status": "transport_failed", "format_retry": True, "usage_known": False,
              "budget_charge_tokens": 100, "reserved_budget_tokens": 100, "error": "Synthetic HTTP 429"}
    write_json(base / "model_requests/0003.json", failed)
    run["model_metrics"].update(model_calls=3, budget_tokens=140, unknown_usage_calls=1, metrics_complete=False, contract_retries=1)
    with sqlite3.connect(data / "agentforge.sqlite3") as db:
        db.execute("UPDATE runs SET body=?", (json.dumps(run),))
        db.execute("INSERT INTO events VALUES(?,?)", (run["id"], json.dumps({"type": "model_request", "data": {"sequence": 3}})))
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    counts = result["aggregate"]
    assert counts["model_calls"] == 3 and counts["known_tokens"] == 40
    assert counts["unknown_usage_calls"] == counts["format_retries"] == counts["transport_failed_requests"] == 1
    assert counts["natural_candidate_failures"] == counts["seeded_candidate_failures"] == 0


def test_leaky_evidence_is_flagged_and_audit_report_redacts_secret(tmp_path):
    data = tmp_path / "input"
    run = base_run()
    secret = "synthetic-secret-never-valid"
    run["bundle"]["provenance"]["note"] = secret
    run["bundle_sha256"] = AUDIT.digest(json.dumps(run["bundle"], ensure_ascii=False, sort_keys=True))
    write_db(data, [run])
    result = AUDIT.audit(data, secret=secret)
    assert not result["passed"]
    assert not result["secret_scan"]["passed"]
    assert secret not in json.dumps(result)
    assert result["runs"][0]["provenance"]["note"] == "[REDACTED]"


def test_existing_output_is_never_overwritten(tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("preserve me", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        AUDIT.main(["--data-dir", str(tmp_path / "input"), "--output", str(output)])
    assert exc.value.code == 2
    assert output.read_text(encoding="utf-8") == "preserve me"


def test_output_cannot_be_written_inside_read_only_input_tree(tmp_path):
    with pytest.raises(SystemExit) as exc:
        AUDIT.main(["--data-dir", str(tmp_path), "--output", str(tmp_path / "audit.json")])
    assert exc.value.code == 2
    assert not (tmp_path / "audit.json").exists()


def intake_record(run_id, output=None, *, sequence=1, status="accepted", retry=False):
    """A synthetic intake response record; no service is involved."""
    record = {"run_id": run_id, "sequence": sequence, "role": "intake", "task_id": None,
              "status": status, "format_retry": retry, "usage_known": True,
              "usage": {"total_tokens": 20}, "budget_charge_tokens": 20,
              "reserved_budget_tokens": 100}
    if output is not None:
        record.update(content=json.dumps(output), output=deepcopy(output))
    else:
        record.update(content="synthetic malformed JSON", protocol_error={"kind": "json_syntax"})
    return record


def unprepared_intake_fixture(data, state):
    run = base_run()
    run.update(bundle=None, bundle_sha256=None, intake={"status": "pending", "acceptance_origin": "model_generated"})
    records = []
    if state == "pending":
        run["status"] = "queued"
    elif state == "protocol_failed":
        run["status"] = "failed"
        records = [intake_record(run["id"], status="protocol_failed"),
                   intake_record(run["id"], sequence=2, status="protocol_failed", retry=True)]
    elif state == "needs_input":
        run["status"] = "needs_human"
        output = {"status": "needs_input", "summary": "Synthetic clarification only", "questions": ["Specify input and output"]}
        run["intake"].update(output)
        records = [intake_record(run["id"], output)]
    run["model_metrics"] = {"model_calls": len(records), "tokens": len(records) * 20,
                            "budget_tokens": len(records) * 20, "metrics_complete": True,
                            "unknown_usage_calls": 0, "contract_retries": int(state == "protocol_failed"),
                            "in_flight": 0, "reserved_tokens": 0}
    base = data / "runs" / run["id"]
    for record in records:
        write_json(base / "model_requests" / f"{record['sequence']:04d}.json", record)
    events = [{"type": "model_request", "data": {"sequence": record["sequence"]}} for record in records]
    write_db(data, [run], events)
    return run, base


@pytest.mark.parametrize("state", ["pending", "protocol_failed", "needs_input"])
def test_unprepared_intake_none_bundle_is_retained_without_audit_crash(tmp_path, state):
    data = tmp_path / "input"
    unprepared_intake_fixture(data, state)
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    assert result["aggregate"]["runs"] == result["aggregate"]["non_successful_runs"] == 1
    assert result["aggregate"]["controller_decisions"] == result["aggregate"]["tasks"] == 0
    assert result["aggregate"]["verifications"] == result["aggregate"]["natural_candidate_failures"] == 0
    assert any(issue["severity"] == "warning" and "before any candidate was prepared" in issue["check"]
               for issue in result["issues"])
    if state == "protocol_failed":
        assert result["aggregate"]["protocol_failed_responses"] == 2
        assert result["aggregate"]["format_retries"] == 1
    if state == "needs_input":
        assert result["aggregate"]["model_calls"] == 1


def prepared_intake_fixture(data, *, rejected_baseline=False):
    """Augment synthetic legacy evidence with a source-linked intake record."""
    run, base = complete_fixture(data)
    run["bundle"]["provenance"]["acceptance_origin"] = "model_generated"
    run["bundle_sha256"] = AUDIT.digest(json.dumps(run["bundle"], ensure_ascii=False, sort_keys=True))
    write_json(base / "bundle.json", run["bundle"])
    manifest = json.loads((base / "autonomous_manifest.json").read_text(encoding="utf-8"))
    manifest["bundle_sha256"] = AUDIT.digest(AUDIT.canonical(run["bundle"]))
    write_json(base / "autonomous_manifest.json", manifest)
    output = {"status": "ready", "summary": "Synthetic prepared requirement",
              "specification": "Synthetic public interface, no real model generation",
              "assumptions": [], "files": deepcopy(run["bundle"]["files"]),
              "acceptance": run["bundle"]["acceptance"]}
    run["intake"] = {"status": "completed", "specification": output["specification"],
                     "acceptance_origin": "model_generated"}
    with sqlite3.connect(data / "agentforge.sqlite3") as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT body FROM events ORDER BY rowid")]
        db.execute("DELETE FROM events")
        if rejected_baseline:
            run.update(status="needs_human", controller_decisions=[])
            run["intake"].update(status="needs_input", questions=["Synthetic suite already passed: clarify behavior"])
            for path in (base / "model_requests").glob("*.json"):
                path.unlink()
            events = [event for event in events if event["type"] == "verification"]
            model_calls = 1
        else:
            # Preserve real sequencing: intake precedes both main decisions.
            for sequence in (2, 1):
                old = base / "model_requests" / f"{sequence:04d}.json"
                record = json.loads(old.read_text(encoding="utf-8"))
                record["sequence"] = sequence + 1
                write_json(base / "model_requests" / f"{sequence + 1:04d}.json", record)
            for event in events:
                if event["type"] == "model_request":
                    event["data"]["sequence"] += 1
            model_calls = 3
        write_json(base / "model_requests/0001.json", intake_record(run["id"], output))
        events.insert(0, {"type": "model_request", "data": {"sequence": 1}})
        run["model_metrics"].update(model_calls=model_calls, tokens=model_calls * 20, budget_tokens=model_calls * 20)
        db.execute("UPDATE runs SET body=?", (json.dumps(run),))
        for event in events:
            db.execute("INSERT INTO events VALUES(?,?)", (run["id"], json.dumps(event)))
    return run, base


def test_completed_intake_is_linked_to_accepted_source_and_generated_provenance(tmp_path):
    data = tmp_path / "input"
    prepared_intake_fixture(data)
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    assert result["aggregate"]["model_calls"] == 3
    assert result["aggregate"]["controller_decisions"] == 2
    assert result["runs"][0]["provenance"]["acceptance_origin"] == "model_generated"


@pytest.mark.parametrize("tamper,expected", [
    ("files", "Frozen bundle differs"), ("acceptance", "Frozen bundle differs"),
    ("specification", "Frozen specification differs"), ("origin", "Generated acceptance provenance is missing"),
])
def test_completed_intake_mismatches_fail_even_when_output_json_is_consistent(tmp_path, tamper, expected):
    data = tmp_path / "input"
    run, base = prepared_intake_fixture(data)
    path = base / "model_requests/0001.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "files":
        record["output"]["files"]["x.py"] = "# a different originally generated scaffold\n"
    elif tamper == "acceptance":
        record["output"]["acceptance"] = "# a different originally generated suite\n"
    elif tamper == "specification":
        record["output"]["specification"] = "A different interface contract"
    else:
        run["intake"]["acceptance_origin"] = "maintainer"
        with sqlite3.connect(data / "agentforge.sqlite3") as db:
            db.execute("UPDATE runs SET body=?", (json.dumps(run),))
    record["content"] = json.dumps(record["output"])
    write_json(path, record)
    result = AUDIT.audit(data)
    assert not result["passed"]
    assert any(expected in issue["check"] for issue in result["issues"])


def test_ready_intake_followed_by_already_passing_baseline_remains_needs_input(tmp_path):
    data = tmp_path / "input"
    prepared_intake_fixture(data, rejected_baseline=True)
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    assert result["aggregate"]["non_successful_runs"] == 1
    assert result["aggregate"]["model_calls"] == 1
    assert result["aggregate"]["controller_decisions"] == result["aggregate"]["tasks"] == 0
    assert result["aggregate"]["passed_verifications"] == 1
    assert result["aggregate"]["natural_repair_runs"] == result["aggregate"]["natural_candidate_failures"] == 0


def semantic_review_fixture(data):
    """Synthetic accepted intake, contract review, and candidate-bound final review."""
    run, base = prepared_intake_fixture(data)
    run["requirement"] = "Original user requirement, synthetic evidence only"
    original = [json.loads((base / "model_requests" / f"{sequence:04d}.json").read_text(encoding="utf-8"))
                for sequence in range(1, 4)]
    proposed = original[0]["output"]
    run["intake_proposals"] = [{"index": 1, "timestamp": "synthetic", **deepcopy(proposed)}]
    verdict = {"passed": True, "issues": [], "summary": "Synthetic consistent contract"}
    contract = {**verdict, "index": 1, "timestamp": "synthetic", "origin": "model_contract_review",
                "proposal_sha256": AUDIT.digest(json.dumps(proposed, ensure_ascii=False, sort_keys=True))}
    final_verdict = {"passed": True, "issues": [], "summary": "Synthetic original-requirement match"}
    final = {**final_verdict, "index": 1, "timestamp": "synthetic", "origin": "model_semantic_review",
             "candidate_hash": run["verified_hash"]}
    run.update(contract_reviews=[contract], contract_review=contract,
               quality_reviews=[final], quality_review=final)

    def review_request(role, output, context):
        record = intake_record(run["id"], output)
        record["role"] = role
        record["request"] = {"messages": [{"role": "system", "content": "Synthetic review"},
            {"role": "user", "content": json.dumps({"context": {"original_requirement": run["requirement"], **context}})}]}
        return record

    records = [original[0], review_request("contract_review", verdict, {"proposed_contract": proposed}),
               original[1], original[2], review_request("final_review", final_verdict, {"project": run["bundle"]["files"]})]
    for sequence, record in enumerate(records, 1):
        record["sequence"] = sequence
        write_json(base / "model_requests" / f"{sequence:04d}.json", record)
    run["model_metrics"].update(model_calls=5, tokens=100, budget_tokens=100)
    with sqlite3.connect(data / "agentforge.sqlite3") as db:
        retained = [json.loads(row[0]) for row in db.execute("SELECT body FROM events ORDER BY rowid")
                    if json.loads(row[0])["type"] != "model_request"]
        db.execute("DELETE FROM events")
        events = [{"type": "model_request", "data": {"sequence": number}} for number in range(1, 6)] + retained
        events += [{"type": "contract_review", "data": contract}, {"type": "quality_review", "data": final}]
        for event in events:
            db.execute("INSERT INTO events VALUES(?,?)", (run["id"], json.dumps(event)))
        db.execute("UPDATE runs SET body=?", (json.dumps(run),))
    return run, base


def test_contract_and_final_review_are_bound_to_original_request_and_source(tmp_path):
    data = tmp_path / "input"
    semantic_review_fixture(data)
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    assert result["aggregate"]["model_calls"] == 5
    assert result["aggregate"]["controller_decisions"] == 2
    assert result["runs"][0]["quality_review"]["passed"] is True


@pytest.mark.parametrize("stage,field,expected", [
    ("contract", "original_requirement", "Review did not receive the original requirement"),
    ("final", "original_requirement", "Review did not receive the original requirement"),
    ("contract", "proposed_contract", "Contract review was bound to a different proposal"),
    ("final", "project", "Semantic review was bound to a different candidate"),
    ("final", "verdict", "Review verdict differs from accepted model output"),
])
def test_semantic_review_context_or_verdict_tampering_is_detected(tmp_path, stage, field, expected):
    data = tmp_path / "input"
    _, base = semantic_review_fixture(data)
    path = base / "model_requests" / ("0002.json" if stage == "contract" else "0005.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    if field == "verdict":
        record["output"] = {"passed": False, "issues": ["Synthetic rejected original semantics"], "summary": "Different verdict"}
        record["content"] = json.dumps(record["output"])
    else:
        payload = json.loads(record["request"]["messages"][1]["content"])
        if field == "original_requirement":
            payload["context"][field] = "A different user requirement"
        elif field == "proposed_contract":
            payload["context"][field]["specification"] = "A different proposed public interface"
        else:
            payload["context"][field]["x.py"] = "# different candidate source\n"
        record["request"]["messages"][1]["content"] = json.dumps(payload)
    write_json(path, record)
    result = AUDIT.audit(data)
    assert not result["passed"]
    assert any(expected in issue["check"] for issue in result["issues"])


def test_two_rejected_contract_proposals_remain_unprepared_needs_input(tmp_path):
    data = tmp_path / "input"
    run = base_run()
    run.update(status="needs_human", requirement="Synthetic original requirement", bundle=None, bundle_sha256=None,
               intake={"status": "needs_input", "acceptance_origin": "model_generated", "questions": ["Resolve test conflicts"]},
               intake_proposals=[], contract_reviews=[], contract_review=None, quality_reviews=[], quality_review=None)
    base = data / "runs" / run["id"]
    events = []
    for index in (1, 2):
        proposed = {"status": "ready", "summary": "Synthetic proposal", "specification": f"Interface proposal {index}",
                    "assumptions": [], "files": {"x.py": "# synthetic scaffold\n"}, "acceptance": f"# synthetic suite {index}\n"}
        run["intake_proposals"].append({"index": index, "timestamp": "synthetic", **deepcopy(proposed)})
        proposed_record = intake_record(run["id"], proposed, sequence=index * 2 - 1)
        write_json(base / "model_requests" / f"{index * 2 - 1:04d}.json", proposed_record)
        verdict = {"passed": False, "issues": ["Generated expectation contradicts original requirement"], "summary": "Synthetic contract rejected"}
        review = {**verdict, "index": index, "timestamp": "synthetic", "origin": "model_contract_review",
                  "proposal_sha256": AUDIT.digest(json.dumps(proposed, ensure_ascii=False, sort_keys=True))}
        run["contract_reviews"].append(review)
        reviewed_record = intake_record(run["id"], verdict, sequence=index * 2)
        reviewed_record.update(role="contract_review", request={"messages": [{"role": "system", "content": "Synthetic review"},
            {"role": "user", "content": json.dumps({"context": {"original_requirement": run["requirement"], "proposed_contract": proposed}})}]})
        write_json(base / "model_requests" / f"{index * 2:04d}.json", reviewed_record)
        events += [{"type": "model_request", "data": {"sequence": index * 2 - 1}},
                   {"type": "model_request", "data": {"sequence": index * 2}}, {"type": "contract_review", "data": review}]
    run["contract_review"] = run["contract_reviews"][-1]
    run["model_metrics"].update(model_calls=4, tokens=80, budget_tokens=80)
    write_db(data, [run], events)
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    assert result["aggregate"]["non_successful_runs"] == 1
    assert result["aggregate"]["model_calls"] == 4
    assert result["aggregate"]["controller_decisions"] == result["aggregate"]["verifications"] == 0


def test_generated_suite_failure_then_pass_is_not_natural_code_repair(tmp_path):
    data = tmp_path / "input"
    run, base = prepared_intake_fixture(data)
    success = run["verifications"][0]
    success.update(candidate_origin="model", classification="natural_candidate", index=2)
    bad_files = {"x.py": "# a synthetic earlier candidate, never executed\n"}
    bad_hash = AUDIT.digest(AUDIT.canonical(bad_files))
    failure = {**deepcopy(success), "index": 1, "status": "failed", "exit_code": 1,
               "candidate_sha256": bad_hash, "candidate_hash": bad_hash,
               "snapshot": "evidence/0-validation-start.json", "result_record": "evidence/0-validation-result-synthetic.json",
               "attestation": {"tests": 1, "discovered": 1, "passed": False, "nonce": "synthetic"}}
    write_json(base / failure["snapshot"], {"files": bad_files, "candidate_sha256": bad_hash})
    # Runtime files do not contain the engine's classification wrapper.
    write_json(base / failure["result_record"], {k: v for k, v in failure.items()
               if k not in {"candidate_hash", "candidate_origin", "classification", "index"}})
    run["verifications"] = [failure, success]
    run["posthoc_review"] = {"passed": False, "summary": "Synthetic maintainer rejection of original requirements"}
    with sqlite3.connect(data / "agentforge.sqlite3") as db:
        db.execute("UPDATE runs SET body=?", (json.dumps(run),))
        rows = [(rowid, json.loads(body)) for rowid, body in db.execute("SELECT rowid,body FROM events")]
        for rowid, event in rows:
            if event["type"] == "verification":
                db.execute("DELETE FROM events WHERE rowid=?", (rowid,))
        for verification in run["verifications"]:
            db.execute("INSERT INTO events VALUES(?,?)", (run["id"], json.dumps({"type": "verification", "data": verification})))
    result = AUDIT.audit(data)
    assert result["passed"], result["issues"]
    counts = result["aggregate"]
    assert counts["generated_acceptance_candidate_failures"] == counts["generated_acceptance_repair_runs"] == 1
    assert counts["natural_candidate_failures"] == counts["natural_repair_runs"] == 0
    assert counts["posthoc_rejected_runs"] == 1


def test_contract_review_cannot_validate_a_different_but_self_consistent_proposal(tmp_path):
    data = tmp_path / "input"
    run, base = semantic_review_fixture(data)
    path = base / "model_requests/0002.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    payload = json.loads(record["request"]["messages"][1]["content"])
    proposed = payload["context"]["proposed_contract"]
    proposed["acceptance"] = "# different proposal: never accepted by intake\n"
    record["request"]["messages"][1]["content"] = json.dumps(payload)
    write_json(path, record)
    review = run["contract_reviews"][0]
    review["proposal_sha256"] = AUDIT.digest(json.dumps(proposed, ensure_ascii=False, sort_keys=True))
    with sqlite3.connect(data / "agentforge.sqlite3") as db:
        db.execute("UPDATE runs SET body=?", (json.dumps(run),))
        for rowid, body in db.execute("SELECT rowid,body FROM events").fetchall():
            event = json.loads(body)
            if event["type"] == "contract_review":
                event["data"] = review
                db.execute("UPDATE events SET body=? WHERE rowid=?", (json.dumps(event), rowid))
    result = AUDIT.audit(data)
    assert not result["passed"], "Review context/hash agree with each other but describe a different intake proposal"
    assert any("proposal" in issue["check"].lower() for issue in result["issues"] if issue["severity"] == "error")
