"""Synthetic Web evidence only: no candidate, dependency, Docker, or model executes."""
from copy import deepcopy
import json

import pytest

from test_audit_autonomous import AUDIT, complete_fixture, write_json
from test_audit_project_autonomous import project_fixture, save_fixture, assert_fails


WEB = {"schema": "python-web-v1", "entrypoint": "x.py", "dependency_id": "b" * 64,
       "image_id": "sha256:" + "1" * 64, "lock_sha256": "c" * 64,
       "requirements": ["Flask==3.1.3"]}
RUNTIME = AUDIT.digest(AUDIT.canonical(WEB))
IMPLEMENTATION = {"sandbox/web/runner.py", "sandbox/web/web_bridge.py", "sandbox/web/serve.py",
                  "sandbox/web/http_probe.py", "sandbox/web/download_wheels.py", "sandbox/web/Dockerfile",
                  "backend/agentforge/web_dependencies.py", "backend/agentforge/web_preview.py",
                  "backend/agentforge/web_config.py"}


def freeze_web(run, base, events):
    """Attach internally consistent, explicitly synthetic runtime receipts."""
    run["bundle"]["provenance"]["web"] = deepcopy(WEB)
    if run.get("project_context"):
        run["project_context"]["web"] = deepcopy(WEB)
        frozen = next(e["data"] for e in events if e["type"] == "project_revision_frozen")
        frozen.update(web_runtime_sha256=RUNTIME, dependency_id=WEB["dependency_id"])
    run["bundle_sha256"] = AUDIT.digest(json.dumps(run["bundle"], ensure_ascii=False, sort_keys=True))
    write_json(base / "bundle.json", run["bundle"])
    manifest = AUDIT._read_json(base / "autonomous_manifest.json")
    manifest.update(schema="autonomous-python-web-v1", web=deepcopy(WEB), web_runtime_sha256=RUNTIME,
                    bundle_sha256=AUDIT.digest(AUDIT.canonical(run["bundle"])))
    for filename in ("web_bridge.py", "serve.py"):
        content = "# synthetic trusted source; never executed\n"
        (base / "runner" / filename).write_text(content, encoding="utf-8")
        manifest["runner_sha256"][filename] = AUDIT.digest(content)
    write_json(base / "autonomous_manifest.json", manifest)
    for verification in run["verifications"]:
        verification.update(web_runtime_sha256=RUNTIME, dependency_id=WEB["dependency_id"],
                            lock_sha256=WEB["lock_sha256"], entrypoint=WEB["entrypoint"],
                            verification_kind="http", runner_sha256=manifest["runner_sha256"])
        verification["attestation"].update(verification_kind="http", http_requests=verification["test_count"],
                                           startup_error=None, server_exit_code=None)
        write_json(base / verification["result_record"], verification)
    if run.get("verified_hash"):
        run["verified_runtime_sha256"] = RUNTIME
    for review in run.get("quality_reviews", []):
        review["web_runtime_sha256"] = RUNTIME
    if run.get("publication", {}).get("status") == "saved":
        run["publication"].update(web_runtime_sha256=RUNTIME, dependency_id=WEB["dependency_id"])
    for path in (base / "model_requests").glob("*.json"):
        record = AUDIT._read_json(path)
        record.setdefault("request", {"messages": [{"role": "system", "content": "synthetic"},
                                                    {"role": "user", "content": '{"context":{}}'}]})
        payload = json.loads(record["request"]["messages"][1]["content"])
        payload["context"]["web"] = deepcopy(WEB)
        record["request"]["messages"][1]["content"] = json.dumps(payload)
        write_json(path, record)
    implementation = {}
    for relative in sorted(IMPLEMENTATION):
        path = base / "implementation" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic implementation archive; never executed\n", encoding="utf-8")
        implementation[relative] = AUDIT.digest(path.read_bytes())
    write_json(base / "implementation-manifest.json", implementation)


def web_project_fixture(data, *, http_status=None, transport_failure=False):
    run, base, events = project_fixture(data, tool="success" if http_status is not None or transport_failure else None)
    freeze_web(run, base, events)
    if http_status is not None or transport_failure:
        for decision in run["controller_decisions"]:
            if decision["action"] == "tool":
                decision.update(tool="http_request", arguments={"method": "GET", "path": "/health"})
        for path in (base / "model_requests").glob("*.json"):
            record = AUDIT._read_json(path)
            if record.get("output", {}).get("action") == "tool":
                record["output"].update(tool="http_request", arguments={"method": "GET", "path": "/health"})
                record["content"] = json.dumps(record["output"])
                write_json(path, record)
        for event in events:
            if event["type"] in {"tool_started", "tool_result"}:
                event["data"].update(tool="http_request", arguments={"method": "GET", "path": "/health"})
            if event["type"] == "tool_result":
                result = {"candidate_sha256": event["data"]["candidate_sha256"],
                          "web_runtime_sha256": RUNTIME, "dependency_id": WEB["dependency_id"],
                          "lock_sha256": WEB["lock_sha256"], "image_id": WEB["image_id"],
                          "test_claim": False, "isolated": True, "ok": not transport_failure}
                if transport_failure:
                    result["error"] = "Synthetic connection refused; no candidate executed"
                else:
                    result.update(status=http_status, body_text="Synthetic HTTP receipt")
                event["data"]["result"] = result
    save_fixture(data, run, events)
    return run, base, events


def seeded_fixture(data):
    run, base = complete_fixture(data)
    events = AUDIT._read_database(data)["events"][run["id"]]
    run["bundle"]["provenance"]["kind"] = "seeded_defect"
    initial = deepcopy(run["verifications"][0])
    initial.update(status="failed", exit_code=1, classification="seeded_defect_baseline",
                   result_record="evidence/0-validation-result-synthetic.json",
                   snapshot="evidence/0-validation-start.json")
    initial["attestation"]["passed"] = False
    write_json(base / initial["snapshot"], {"files": run["bundle"]["files"], "candidate_sha256": initial["candidate_hash"]})
    current = run["verifications"][0]
    after = {"x.py": "# synthetic edited candidate; never executed\n"}
    fingerprint = AUDIT.digest(AUDIT.canonical(after))
    (base / "candidate/x.py").write_text(after["x.py"], encoding="utf-8")
    current.update(index=2, candidate_hash=fingerprint, candidate_sha256=fingerprint,
                   candidate_origin="natural_candidate", classification="seeded_experiment_candidate")
    write_json(base / current["snapshot"], {"files": after, "candidate_sha256": fingerprint})
    run.update(verifications=[initial, current], verified_hash=fingerprint)
    events = [e for e in events if e["type"] != "verification"]
    events.extend([{"type": "verification", "data": initial}, {"type": "verification", "data": current}])
    freeze_web(run, base, events)
    save_fixture(data, run, events)
    return run, base, events


@pytest.mark.parametrize("http_status", [None, 200, 404, 500])
def test_web_audit_is_read_only_and_http_errors_do_not_become_candidate_failures(tmp_path, http_status):
    data = tmp_path / "input"
    web_project_fixture(data, http_status=http_status)
    before = {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    report = AUDIT.audit(data)
    assert report["passed"], report["issues"]
    assert before == {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    counts = report["aggregate"]
    assert counts["web_runs"] == 1
    assert counts["http_responses_received"] == int(http_status is not None)
    assert counts["http_error_responses"] == int(http_status is not None and http_status >= 400)
    assert counts["natural_candidate_failures"] == counts["generated_acceptance_candidate_failures"] == 0
    assert counts["natural_repair_runs"] == counts["seeded_repair_runs"] == 0
    assert report["runs"][0]["hashes"]["web_runtime_sha256"] == RUNTIME


def test_http_transport_error_is_distinct_from_independent_acceptance(tmp_path):
    data = tmp_path / "input"
    web_project_fixture(data, transport_failure=True)
    report = AUDIT.audit(data)
    assert report["passed"], report["issues"]
    assert report["aggregate"]["http_transport_failures"] == 1
    assert report["aggregate"]["passed_verifications"] == 1
    assert report["aggregate"]["natural_candidate_failures"] == 0


@pytest.mark.parametrize("tamper,expected", [
    ("project", "runtime differs from frozen bundle"), ("freeze_event", "Frozen project/configuration event"),
    ("quality", "semantic review used a different"), ("verified", "not bound to the frozen runtime"),
    ("publication", "Published Web version"), ("request", "did not receive the frozen runtime"),
    ("manifest", "sandbox manifest runtime binding"), ("runner", "omits its trusted HTTP runner"),
    ("implementation", "omits its runtime/dependency execution sources"),
    ("runtime", "verification used a different frozen runtime"),
    ("image", "outside its pinned dependency image"), ("empty_http", "healthy real-HTTP server"),
    ("startup", "healthy real-HTTP server"), ("server_exit", "healthy real-HTTP server"),
])
def test_web_runtime_bindings_reject_independent_tampering(tmp_path, tamper, expected):
    data = tmp_path / "input"
    run, base, events = web_project_fixture(data)
    if tamper == "project":
        run["project_context"]["web"]["requirements"] = ["Flask==3.0.0"]
    elif tamper == "freeze_event":
        next(e["data"] for e in events if e["type"] == "project_revision_frozen").pop("dependency_id")
    elif tamper == "quality":
        run["quality_review"]["web_runtime_sha256"] = "0" * 64
    elif tamper == "verified":
        run["verified_runtime_sha256"] = "0" * 64
    elif tamper == "publication":
        run["publication"]["dependency_id"] = "0" * 64
    elif tamper == "request":
        path = base / "model_requests/0001.json"
        record = AUDIT._read_json(path)
        payload = json.loads(record["request"]["messages"][1]["content"])
        payload["context"].pop("web")
        record["request"]["messages"][1]["content"] = json.dumps(payload)
        write_json(path, record)
    elif tamper in {"manifest", "runner"}:
        path = base / "autonomous_manifest.json"
        manifest = AUDIT._read_json(path)
        if tamper == "manifest":
            manifest["web_runtime_sha256"] = "0" * 64
        else:
            manifest["runner_sha256"].pop("serve.py")
        write_json(path, manifest)
    elif tamper == "implementation":
        path = base / "implementation-manifest.json"
        manifest = AUDIT._read_json(path)
        manifest.pop("sandbox/web/http_probe.py")
        write_json(path, manifest)
    else:
        result = run["verifications"][-1]
        if tamper == "runtime":
            result["lock_sha256"] = "0" * 64
        elif tamper == "image":
            result["image"] = "sha256:" + "0" * 64
        else:
            field, value = {"empty_http": ("http_requests", 0), "startup": ("startup_error", "Synthetic startup failure"),
                            "server_exit": ("server_exit_code", 0)}[tamper]
            result["attestation"][field] = value
        write_json(base / result["result_record"], result)
    save_fixture(data, run, events)
    assert_fails(data, expected)


@pytest.mark.parametrize("field,value,expected", [
    ("image_id", "sha256:" + "0" * 64, "different frozen runtime"),
    ("lock_sha256", "0" * 64, "different frozen runtime"),
    ("web_runtime_sha256", "0" * 64, "different frozen runtime"),
    ("test_claim", True, "claims independent acceptance authority"),
    ("isolated", False, "actual isolated HTTP evidence"),
    ("status", "200", "actual isolated HTTP evidence"),
])
def test_http_tool_receipt_cannot_replace_or_rebind_acceptance(tmp_path, field, value, expected):
    data = tmp_path / "input"
    run, _, events = web_project_fixture(data, http_status=200)
    next(e["data"]["result"] for e in events if e["type"] == "tool_result")[field] = value
    save_fixture(data, run, events)
    assert_fails(data, expected)


def test_seeded_standalone_web_repair_has_no_project_or_semantic_review_requirement(tmp_path):
    data = tmp_path / "input"
    seeded_fixture(data)
    report = AUDIT.audit(data)
    assert report["passed"], report["issues"]
    counts = report["aggregate"]
    assert counts["seeded_baseline_failures"] == counts["seeded_repair_runs"] == 1
    assert counts["natural_candidate_failures"] == counts["natural_repair_runs"] == 0
    assert counts["format_retries"] == 0
    assert report["runs"][0]["project_evidence"] is None


def test_web_startup_code_failure_retains_failed_isolated_test_evidence(tmp_path):
    data = tmp_path / "input"
    run, base, events = web_project_fixture(data)
    baseline = run["verifications"][0]
    baseline["attestation"].update(startup_error="Synthetic SyntaxError", server_exit_code=1, http_requests=0)
    write_json(base / baseline["result_record"], baseline)
    save_fixture(data, run, events)
    report = AUDIT.audit(data)
    assert report["passed"], report["issues"]
    assert report["aggregate"]["imported_baseline_failures"] == 1
    assert report["aggregate"]["blocked_verifications"] == 0


def test_web_failure_requires_an_actual_isolated_suite(tmp_path):
    data = tmp_path / "input"
    run, base, events = web_project_fixture(data)
    baseline = run["verifications"][0]
    baseline["isolated"] = False
    write_json(base / baseline["result_record"], baseline)
    save_fixture(data, run, events)
    assert_fails(data, "Failed Web verification lacks actual isolated")


def test_seeded_web_success_must_start_with_a_demonstrated_failing_baseline(tmp_path):
    data = tmp_path / "input"
    run, base, events = seeded_fixture(data)
    baseline = run["verifications"][0]
    baseline.update(status="passed", exit_code=0)
    baseline["attestation"]["passed"] = True
    write_json(base / baseline["result_record"], baseline)
    save_fixture(data, run, events)
    assert_fails(data, "failed baseline followed by an edited passing candidate")


def test_web_format_retry_is_separate_from_seeded_repair(tmp_path):
    data = tmp_path / "input"
    run, base, events = seeded_fixture(data)
    second = AUDIT._read_json(base / "model_requests/0002.json")
    bad_format = deepcopy(second)
    bad_format.update(status="protocol_failed", content="Synthetic non-JSON response", output=None)
    second.update(sequence=3, format_retry=True)
    write_json(base / "model_requests/0002.json", bad_format)
    write_json(base / "model_requests/0003.json", second)
    run["model_metrics"].update(model_calls=3, tokens=60, budget_tokens=60, contract_retries=1)
    events.insert(3, {"type": "model_request", "data": {"sequence": 3}})
    save_fixture(data, run, events)
    report = AUDIT.audit(data)
    assert report["passed"], report["issues"]
    counts = report["aggregate"]
    assert counts["format_retries"] == counts["protocol_failed_responses"] == counts["seeded_repair_runs"] == 1
    assert counts["natural_candidate_failures"] == counts["natural_repair_runs"] == 0


@pytest.mark.parametrize("field,value", [("entrypoint", []), ("dependency_id", "not-a-hash"),
                                         ("requirements", ["Flask"]), ("schema", "mutable-v1")])
def test_malformed_web_config_is_an_audit_failure_without_running_or_crashing(tmp_path, field, value):
    data = tmp_path / "input"
    run, _, events = seeded_fixture(data)
    run["bundle"]["provenance"]["web"][field] = value
    save_fixture(data, run, events)
    assert AUDIT.audit(data)["passed"] is False
