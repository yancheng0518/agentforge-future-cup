"""Read-only audit of every autonomous run and its reproducibility evidence.

Does not call models, start Docker, import candidates, instantiate Store, or modify
the input database. The sole write is a new explicitly named JSON audit report.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.runtime import Runtime


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _contained(base, relative):
    path = base / relative
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("Evidence path leaves its run directory")
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("Evidence path uses a link or junction")
        if item == base:
            break
    return path


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_database(data_dir):
    database = data_dir / "agentforge.sqlite3"
    if not database.is_file():
        raise ValueError("Input data directory has no agentforge.sqlite3")
    # A read transaction gives one consistent SQLite/WAL snapshot without writes.
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        runs = [_read_row(r[0]) for r in connection.execute("SELECT body FROM runs ORDER BY rowid")]
        result = {"runs": runs, "events": {}, "steps": {}, "snapshots": {}}
        for table in ("events", "steps", "snapshots"):
            for run_id, body in connection.execute(f"SELECT run_id,body FROM {table} ORDER BY rowid"):
                result[table].setdefault(run_id, []).append(_read_row(body))
        return result
    finally:
        connection.close()


def _read_row(body):
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("Database evidence row is not an object")
    return value


def audit(data_dir, secret=""):
    data_dir = Path(data_dir).resolve()
    database = _read_database(data_dir)
    issues, reports = [], []
    aggregate = Counter()
    aggregate_keys = ("runs", "succeeded_runs", "non_successful_runs", "model_calls", "known_tokens",
                      "unknown_usage_calls", "format_retries", "protocol_failed_responses", "transport_failed_requests",
                      "cancelled_requests", "interrupted_requests", "controller_decisions", "decision_rejections",
                      "tasks", "failed_tasks", "verifications", "passed_verifications", "blocked_verifications",
                      "natural_candidate_failures", "input_baseline_failures", "seeded_baseline_failures",
                      "seeded_candidate_failures", "natural_repair_runs", "seeded_repair_runs",
                      "generated_acceptance_candidate_failures", "generated_acceptance_repair_runs",
                      "posthoc_rejected_runs", "quality_rejected_runs",
                      "unlinked_validation_results")
    aggregate.update({key: 0 for key in aggregate_keys})

    def issue(run_id, check, severity="error"):
        # Only fixed diagnostic text/path labels are emitted, never secret content.
        issues.append({"run_id": run_id, "check": check, "severity": severity})

    def check(run_id, condition, label):
        if not condition:
            issue(run_id, label)

    # Scan loaded DB JSON even if a credential were split across SQLite pages.
    secret_hits = []
    if secret and secret in json.dumps(database, ensure_ascii=False):
        secret_hits.append("database_rows")

    for run in database["runs"]:
        run_id = run.get("id")
        aggregate["runs"] += 1
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            issue(None, "Invalid run identifier")
            continue
        base = _contained(data_dir, "runs/" + run_id)
        events = database["events"].get(run_id, [])
        steps = database["steps"].get(run_id, [])
        snapshots = database["snapshots"].get(run_id, [])
        run_issue_start = len(issues)
        check(run_id, run.get("engine") == "model-directed-v1", "Unexpected orchestration engine revision")
        check(run_id, run.get("mode") == "live", "Run is not labelled live")
        if run.get("status") in {"running", "queued"}:
            issue(run_id, "Run is still active; audit is provisional", "warning")
        counts = Counter({key: 0 for key in aggregate_keys if key not in {"runs", "succeeded_runs", "non_successful_runs"}})
        records, record_files = [], []
        request_dir = _contained(base, "model_requests")
        for path in sorted(request_dir.glob("*.json")):
            try:
                record = _read_json(_contained(base, path.relative_to(base)))
                records.append(record)
                record_files.append({"path": path.relative_to(base).as_posix(), "sha256": digest(path.read_bytes())})
            except (ValueError, OSError, TypeError):
                issue(run_id, "Unreadable model request evidence: " + path.name)
        records.sort(key=lambda value: value.get("sequence", -1))
        check(run_id, [r.get("sequence") for r in records] == list(range(1, len(records) + 1)), "Request sequences are missing or duplicated")
        check(run_id, all(r.get("run_id") == run_id for r in records), "Request evidence belongs to a different run")
        counts["model_calls"] = len(records)
        completed_unknown, pending_count, pending_reserve, charged = 0, 0, 0, 0
        for record in records:
            usage = record.get("usage", {}).get("total_tokens")
            known = record.get("usage_known") is True
            check(run_id, known == (type(usage) is int and usage >= 0), "Request usage_known disagrees with provider token evidence")
            if known:
                counts["known_tokens"] += usage
            if record.get("status") == "in_flight":
                pending_count += 1
                pending_reserve += record.get("reserved_budget_tokens", 0)
                counts["interrupted_requests"] += 1
            else:
                completed_unknown += int(not known)
                charged += record.get("budget_charge_tokens", 0)
                check(run_id, record.get("budget_charge_tokens") == (usage if known else record.get("reserved_budget_tokens")), "Request budget charge is inconsistent")
            counts["format_retries"] += int(record.get("format_retry") is True)
            counts["protocol_failed_responses"] += int(record.get("status") == "protocol_failed")
            counts["transport_failed_requests"] += int(record.get("status") == "transport_failed")
            counts["cancelled_requests"] += int(record.get("status") == "cancelled")
            check(run_id, "reasoning_content" not in record, "Provider reasoning unexpectedly retained as a request field")
            if record.get("status") == "accepted":
                try:
                    check(run_id, json.loads(record["content"]) == record.get("output"), "Accepted model output differs from retained final content")
                except (ValueError, KeyError, TypeError):
                    issue(run_id, "Accepted model request lacks valid JSON final content")
        recovered = any(e.get("type") == "interrupted_recovery" for e in events)
        expected_unknown = completed_unknown + (pending_count if recovered else 0)
        expected_inflight = 0 if recovered else pending_count
        expected_reserved = 0 if recovered else pending_reserve
        expected_charged = charged + (pending_reserve if recovered else 0)
        counts["unknown_usage_calls"] = completed_unknown + pending_count
        metrics = run.get("model_metrics", {})
        expected_metrics = {"model_calls": counts["model_calls"], "tokens": counts["known_tokens"],
                            "contract_retries": counts["format_retries"], "unknown_usage_calls": expected_unknown,
                            "in_flight": expected_inflight, "reserved_tokens": expected_reserved,
                            "budget_tokens": expected_charged,
                            "metrics_complete": expected_unknown == 0 and expected_inflight == 0}
        for key, value in expected_metrics.items():
            default = True if key == "metrics_complete" else 0
            check(run_id, metrics.get(key, default) == value, "Persisted model metric mismatch: " + key)
        starts = [e for e in events if e.get("type") == "model_request"]
        check(run_id, sorted(e.get("data", {}).get("sequence", -1) for e in starts) == list(range(1, len(records) + 1)), "Model request events do not cover every attempted request")

        decisions = run.get("controller_decisions", [])
        decision_events = [e.get("data") for e in events if e.get("type") == "controller_decision"]
        check(run_id, decisions == decision_events, "Controller decisions disagree with append-only events")
        check(run_id, [d.get("index") for d in decisions] == list(range(1, len(decisions) + 1)), "Controller decision sequence is incomplete")
        accepted_main = [r.get("output") for r in records if r.get("role") == "main" and r.get("status") == "accepted"]
        decision_outputs = [{k: v for k, v in d.items() if k not in {"index", "timestamp"}} for d in decisions]
        check(run_id, accepted_main[:len(decisions)] == decision_outputs, "Controller decisions are not backed by accepted main-Agent requests")
        if len(accepted_main) > len(decisions) and run.get("status") == "blocked":
            issue(run_id, "Accepted main response was interrupted before decision commit", "warning")
        else:
            check(run_id, len(accepted_main) == len(decisions), "Accepted main response count differs from committed decisions")
        counts["controller_decisions"] = len(decisions)
        counts["decision_rejections"] = sum(e.get("type") == "decision_rejected" for e in events)
        graph = run.get("task_graph", [])
        by_id = {t.get("id"): t for t in graph}
        check(run_id, len(by_id) == len(graph), "Task graph contains duplicate IDs")
        step_by_id = {s.get("id"): s for s in steps}
        expected_step_ids = {t["id"] for t in graph if t.get("status") != "pending"}
        check(run_id, set(step_by_id) == expected_step_ids, "Persisted steps do not cover exactly all started/cancelled tasks")
        for task in graph:
            if task.get("status") != "pending":
                check(run_id, step_by_id.get(task["id"]) == task, "Task and step evidence differ: " + task["id"])
            if task.get("status") == "succeeded":
                check(run_id, any(r.get("task_id") == task["id"] and r.get("role") == "worker" and r.get("status") == "accepted" for r in records), "Successful task lacks an accepted worker request: " + task["id"])
        counts["tasks"] = len(graph)
        counts["failed_tasks"] = sum(t.get("status") == "failed" for t in graph)

        hashes = {}
        manifest = None
        bundle = run.get("bundle") if isinstance(run.get("bundle"), dict) else {}
        intake = run.get("intake") if isinstance(run.get("intake"), dict) else None
        unprepared_intake = (intake is not None and not bundle and not graph and not decisions
                             and not run.get("verifications"))
        intake_records = [record for record in records if record.get("role") == "intake"
                          and record.get("status") == "accepted"]
        if intake and intake.get("status") in ("completed", "needs_input"):
            check(run_id, len(intake_records) >= 1, "Completed intake lacks an accepted intake response")
            if intake_records:
                output = intake_records[-1].get("output", {})
                # A ready contract may later need input if its scaffold already
                # passes the generated suite; retain the original ready response.
                contract_rejected = (run.get("contract_review") or {}).get("passed") is False
                expected_status = "ready" if bundle or contract_rejected else "needs_input"
                check(run_id, output.get("status") == expected_status, "Intake status differs from accepted model response")
                if bundle:
                    check(run_id, bundle.get("files") == output.get("files") and bundle.get("acceptance") == output.get("acceptance"),
                          "Frozen bundle differs from accepted intake scaffold or tests")
                    check(run_id, intake.get("specification") == output.get("specification"), "Frozen specification differs from intake response")
                    check(run_id, intake.get("acceptance_origin") == "model_generated"
                          and bundle.get("provenance", {}).get("acceptance_origin") == "model_generated",
                          "Generated acceptance provenance is missing")
        if "intake_proposals" in run:
            proposed_outputs = [{k: v for k, v in proposal.items() if k not in {"index", "timestamp"}}
                                for proposal in run["intake_proposals"]]
            check(run_id, proposed_outputs == [record.get("output") for record in intake_records],
                  "Intake proposal history differs from accepted model responses")
        for plural, singular, role in (("contract_reviews", "contract_review", "contract_review"),
                                       ("quality_reviews", "quality_review", "final_review")):
            if plural not in run:
                continue  # Historical adapter versions had no semantic review.
            reviews = run[plural]
            review_records = [record for record in records if record.get("role") == role and record.get("status") == "accepted"]
            check(run_id, reviews == [event.get("data") for event in events if event.get("type") == singular],
                  "Review history disagrees with append-only events: " + singular)
            check(run_id, len(reviews) == len(review_records), "Review history lacks corresponding model responses: " + singular)
            for review, record in zip(reviews, review_records):
                check(run_id, {key: review.get(key) for key in ("passed", "issues", "summary")} == record.get("output"),
                      "Review verdict differs from accepted model output: " + singular)
                try:
                    context = json.loads(record["request"]["messages"][1]["content"])["context"]
                    check(run_id, context.get("original_requirement") == run.get("requirement"),
                          "Review did not receive the original requirement: " + singular)
                    if role == "contract_review":
                        proposed = context["proposed_contract"]
                        check(run_id, digest(json.dumps(proposed, ensure_ascii=False, sort_keys=True)) == review.get("proposal_sha256"),
                              "Contract review was bound to a different proposal")
                        proposal_index = review.get("index")
                        check(run_id, type(proposal_index) is int and 1 <= proposal_index <= len(intake_records)
                              and proposed == intake_records[proposal_index - 1].get("output"),
                              "Contract review proposal differs from its accepted intake proposal")
                    else:
                        check(run_id, digest(canonical(context["project"])) == review.get("candidate_hash"),
                              "Semantic review was bound to a different candidate")
                except (KeyError, TypeError, ValueError, IndexError):
                    issue(run_id, "Review request context cannot establish its binding: " + singular)
            if run.get(singular) is not None:
                check(run_id, run[singular] in reviews, "Current review is absent from retained history: " + singular)
        if bundle and "contract_reviews" in run:
            check(run_id, (run.get("contract_review") or {}).get("passed") is True,
                  "Generated contract was frozen without a passing consistency review")
        if run.get("status") == "succeeded" and "quality_reviews" in run:
            review = run.get("quality_review") or {}
            check(run_id, review.get("passed") is True and review.get("candidate_hash") == run.get("verified_hash"),
                  "Successful chat run lacks a passing semantic review of the verified candidate")
        try:
            if not unprepared_intake:
                expected_run_bundle = digest(json.dumps(run["bundle"], ensure_ascii=False, sort_keys=True))
                hashes["run_bundle_sha256"] = expected_run_bundle
                check(run_id, run.get("bundle_sha256") == expected_run_bundle, "Run bundle hash differs from frozen DB bundle")
            if (base / "autonomous_manifest.json").is_file():
                manifest = _read_json(_contained(base, "autonomous_manifest.json"))
                frozen = _read_json(_contained(base, "bundle.json"))
                check(run_id, frozen == run["bundle"], "Disk task bundle differs from frozen DB bundle")
                check(run_id, digest(canonical(frozen)) == manifest["bundle_sha256"], "Sandbox bundle hash mismatch")
                suite = _contained(base, "trusted/test_acceptance.py").read_text(encoding="utf-8")
                check(run_id, digest(suite) == manifest["acceptance_sha256"], "Frozen acceptance suite hash mismatch")
                check(run_id, suite == frozen["acceptance"], "Acceptance suite differs from frozen task bundle")
                check(run_id, digest(canonical(frozen["files"])) == manifest["baseline_sha256"], "Baseline source hash mismatch")
                for filename, expected in manifest["runner_sha256"].items():
                    runner = _contained(base, "runner/" + filename)
                    check(run_id, digest(runner.read_text(encoding="utf-8")) == expected, "Frozen runner hash mismatch: " + filename)
                candidate = {}
                for path in sorted(_contained(base, "candidate").rglob("*")):
                    safe = _contained(base, path.relative_to(base))
                    if safe.is_file():
                        check(run_id, safe.stat().st_nlink == 1, "Candidate file is a hard link")
                        candidate[safe.relative_to(base / "candidate").as_posix()] = safe.read_text(encoding="utf-8")
                hashes.update(candidate_sha256=digest(canonical(candidate)), acceptance_sha256=digest(suite),
                              sandbox_bundle_sha256=manifest["bundle_sha256"], runner_sha256=manifest["runner_sha256"])
                if run.get("status") == "succeeded":
                    check(run_id, run.get("verified_hash") == hashes["candidate_sha256"], "Successful run candidate differs from independently verified hash")
            elif unprepared_intake and run.get("status") != "succeeded":
                issue(run_id, "Intake ended before any candidate was prepared; request evidence retained", "warning")
            elif records or decisions or run.get("status") == "succeeded":
                issue(run_id, "Executed run is missing its frozen sandbox manifest")
            else:
                issue(run_id, "Run has no prepared candidate; preserved as an unexecuted run", "warning")
            implementation_path = _contained(base, "implementation-manifest.json")
            if implementation_path.exists():
                implementation = _read_json(implementation_path)
                for filename, expected in implementation.items():
                    check(run_id, digest(_contained(base, "implementation/" + filename).read_bytes()) == expected,
                          "Frozen implementation copy hash mismatch: " + filename)
                hashes["implementation_manifest_sha256"] = digest(implementation_path.read_bytes())
                hashes["implementation_files"] = len(implementation)
            else:
                issue(run_id, "No implementation snapshot; API runs require separate source archiving", "warning")
        except (ValueError, OSError, KeyError, TypeError):
            issue(run_id, "Frozen source/runner/manifest evidence is missing or unreadable")

        verifications = run.get("verifications", [])
        check(run_id, verifications == [e.get("data") for e in events if e.get("type") == "verification"], "Verification records disagree with append-only events")
        linked_results = set()
        seeded = bundle.get("provenance", {}).get("kind") == "seeded_defect"
        generated_acceptance = bundle.get("provenance", {}).get("acceptance_origin") == "model_generated"
        counts["posthoc_rejected_runs"] = int((run.get("posthoc_review") or {}).get("passed") is False)
        counts["quality_rejected_runs"] = int((run.get("quality_review") or {}).get("passed") is False)
        for verification in verifications:
            counts["verifications"] += 1
            classification = verification.get("classification")
            expected_class = ("seeded_defect_baseline" if seeded else "input_baseline") if verification.get("candidate_origin") == "baseline" else ("seeded_experiment_candidate" if seeded else "natural_candidate")
            check(run_id, classification == expected_class, "Verification failure provenance is inconsistent")
            status = verification.get("status")
            counts["passed_verifications"] += int(status == "passed")
            counts["blocked_verifications"] += int(status == "blocked")
            if status == "failed":
                category = {"natural_candidate": "natural_candidate_failures", "input_baseline": "input_baseline_failures",
                            "seeded_defect_baseline": "seeded_baseline_failures", "seeded_experiment_candidate": "seeded_candidate_failures"}.get(classification)
                if category:
                    # AI-generated tests can themselves be wrong. Test failure
                    # alone does not establish a natural implementation defect.
                    counts["generated_acceptance_candidate_failures" if generated_acceptance and category == "natural_candidate_failures" else category] += 1
            if status == "passed":
                check(run_id, verification.get("isolated") is True and verification.get("exit_code") == 0 and type(verification.get("test_count")) is int and verification["test_count"] > 0, "Passed verification lacks successful nonempty isolated test evidence")
                check(run_id, verification.get("container_invoked") is True and bool(re.fullmatch(r"sha256:[a-f0-9]{64}", verification.get("image") or "")), "Passed verification lacks pinned Docker image evidence")
                attestation = verification.get("attestation", {})
                check(run_id, attestation.get("passed") is True and attestation.get("tests") == attestation.get("discovered") == verification.get("test_count"), "Passed verification lacks a complete test attestation")
            if manifest:
                check(run_id, verification.get("acceptance_sha256") == manifest["acceptance_sha256"], "Verification used a different acceptance suite")
                check(run_id, verification.get("runner_sha256") == manifest["runner_sha256"], "Verification used a different runner")
            try:
                relative = verification["result_record"]
                stored_result = _read_json(_contained(base, relative))
                linked_results.add(relative)
                check(run_id, all(verification.get(k) == v for k, v in stored_result.items()), "Validation result file differs from DB verification")
                snapshot = _read_json(_contained(base, verification["snapshot"]))
                snapshot_hash = digest(canonical(snapshot["files"]))
                check(run_id, snapshot_hash == verification.get("candidate_hash") == verification.get("candidate_sha256") == snapshot.get("candidate_sha256"), "Validation-start source snapshot hash mismatch")
            except (ValueError, OSError, KeyError, TypeError):
                issue(run_id, "Verification lacks a complete result/source snapshot record")
        result_files = {p.relative_to(base).as_posix() for p in (base / "evidence").glob("*-validation-result-*.json")}
        unlinked = sorted(result_files - linked_results)
        counts["unlinked_validation_results"] = len(unlinked)
        if unlinked:
            issue(run_id, "Uncommitted validation results retained; inspect interruption evidence", "warning")
        if run.get("status") == "succeeded":
            check(run_id, any(v.get("status") == "passed" and v.get("candidate_hash") == run.get("verified_hash") for v in verifications), "Successful run has no matching passed verification record")
            check(run_id, not any(t.get("status") in {"pending", "running"} for t in graph), "Successful run has unfinished tasks")
            check(run_id, bool(decisions) and decisions[-1].get("action") == "finish", "Successful run lacks a final main-Agent finish decision")
        for earlier_index, earlier in enumerate(verifications):
            if earlier.get("status") != "failed":
                continue
            later_success = any(v.get("status") == "passed" and v.get("candidate_hash") != earlier.get("candidate_hash") for v in verifications[earlier_index + 1:])
            if later_success and earlier.get("classification") == "natural_candidate":
                counts["generated_acceptance_repair_runs" if generated_acceptance else "natural_repair_runs"] = 1
            if later_success and earlier.get("classification") in {"seeded_defect_baseline", "seeded_experiment_candidate"}:
                counts["seeded_repair_runs"] = 1

        if secret:
            for path in base.rglob("*"):
                try:
                    safe = _contained(base, path.relative_to(base))
                    if safe.is_file() and secret.encode("utf-8") in safe.read_bytes():
                        secret_hits.append(run_id + "/" + safe.relative_to(base).as_posix())
                except (OSError, ValueError):
                    issue(run_id, "Could not safely scan an evidence file for credential leakage")
        export_path = _contained(base, "evidence.json")
        if export_path.exists():
            try:
                exported = _read_json(export_path)
                check(run_id, exported.get("run") == run, "Final JSON export differs from SQLite run")
                check(run_id, [{k: v for k, v in e.items() if k != "id"} for e in exported.get("events", [])] == events, "Final JSON export events differ from SQLite")
                check(run_id, exported.get("steps") == steps, "Final JSON export steps differ from SQLite")
                check(run_id, [{k: v for k, v in s.items() if k != "id"} for s in exported.get("snapshots", [])] == snapshots, "Final JSON export snapshots differ from SQLite")
                hashes["export_sha256"] = digest(export_path.read_bytes())
            except (OSError, ValueError):
                issue(run_id, "Final JSON export cannot be read")
        else:
            issue(run_id, "No final JSON export; SQLite and request records remain primary evidence", "warning")

        aggregate.update(counts)
        aggregate["succeeded_runs" if run.get("status") == "succeeded" else "non_successful_runs"] += 1
        reports.append({"run_id": run_id, "status": run.get("status"), "provenance": bundle.get("provenance"),
                        "intake_status": intake.get("status") if intake else None,
                        "posthoc_review": run.get("posthoc_review"), "quality_review": run.get("quality_review"),
                        "model_config": run.get("model_config"), "budget": run.get("budget"), "model_metrics": metrics,
                        "recomputed_metrics": expected_metrics, "counts": dict(counts), "hashes": hashes,
                        "controller_actions": [d.get("action") for d in decisions],
                        "tasks": [{key: t.get(key) for key in ("id", "title", "agent_id", "status", "depends_on", "write_paths")} for t in graph],
                        "verification_results": [{key: v.get(key) for key in ("index", "status", "test_count", "classification", "candidate_hash", "image")} for v in verifications],
                        "request_records": record_files, "unlinked_validation_results": unlinked,
                        "events": len(events), "steps": len(steps), "snapshots": len(snapshots),
                        "passed": not any(i["severity"] == "error" for i in issues[run_issue_start:])})
    if secret_hits:
        issue(None, "Local configured credential detected in evidence; report does not disclose its value")
    report = {"schema": "agentforge-autonomous-audit-v1", "data_dir": str(data_dir),
            "read_only_inputs": True, "models_invoked": False, "docker_invoked": False,
            "secret_scan": {"configured_key_available": bool(secret), "passed": not secret_hits,
                            "locations": secret_hits},
            "passed": not any(i["severity"] == "error" for i in issues), "aggregate": dict(aggregate),
            "runs": reports, "issues": issues,
            "limitations": ["Hashes establish internal evidence consistency, not resistance to coordinated malicious evidence rewriting.",
                            "Unknown provider usage remains unknown; budget charge is a conservative estimate, not actual billed tokens.",
                            "No model or candidate execution occurs during this audit; live evidence is inspected, not rerun."]}
    if secret:
        # The report itself must remain safe even when inspecting a leaky archive.
        report = json.loads(json.dumps(report, ensure_ascii=False).replace(secret, "[REDACTED]"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True, help="New JSON file; existing files are never overwritten")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        parser.error("Output already exists; choose a new audit filename")
    if output.is_relative_to(data_dir):
        parser.error("Audit output must be outside the read-only input data directory")
    # Settings are read locally solely for an exact credential-leak check.
    secret = Runtime(ROOT, data_dir).settings.get("api_key", "")
    report = audit(data_dir, secret=secret)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": report["passed"], "output": str(output), "aggregate": report["aggregate"],
                      "issues": len(report["issues"])}, ensure_ascii=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
