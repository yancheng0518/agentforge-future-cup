"""Conservative evidence classification for the three contemporaneous arms.

No model request, candidate import, shell command or file mutation occurs here.
Functional acceptance is a common endpoint; a strategy's own review/gate is a
separate endpoint. Missing evidence is never converted into a passing review.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.engine import candidate_fingerprint

ARMS = ("single_direct", "fixed_serial", "dynamic_parallel")
TERMINAL = {"succeeded", "failed", "blocked", "needs_human", "cancelled", "timed_out"}
SCHEMA = "agentforge-comparison-report-v1"


def mapping(value):
    return value if isinstance(value, dict) else {}


def items(value):
    return value if isinstance(value, list) else []


def integer(value):
    return type(value) is int and value >= 0


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def source_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def seconds(start, end):
    try:
        return max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except (ValueError, TypeError):
        return None


def fingerprint(value):
    files = items(mapping(value).get("files"))
    if (len(files) != 1 or not isinstance(files[0], dict) or files[0].get("path") != "todo.py"
            or not isinstance(files[0].get("after"), str)):
        return None
    return candidate_fingerprint({"files": [{"path": "todo.py", "after": files[0]["after"]}]})


def complete_suite(test, task):
    test = mapping(test)
    passed = test.get("status") == "passed"
    expected = task.get("expected_tests")
    marker = "AGENTFORGE_ACCEPTANCE=" + json.dumps({"tests": expected, "passed": passed})
    return bool(expected == 10 and test.get("status") in {"passed", "failed"}
                and test.get("isolated") is True and type(test.get("exit_code")) is int
                and test["exit_code"] == (0 if passed else 1)
                and sha(task.get("acceptance_sha256"))
                and test.get("acceptance_sha256") == task["acceptance_sha256"]
                and test.get("task_id") == task.get("id") and test.get("expected_tests") == expected
                and marker in str(test.get("stdout", ""))
                and re.search(rf"Ran {expected} tests? in ", str(test.get("stderr", ""))) is not None)


def budget_audit(detail, ledger):
    """Reconcile every actually sent request with exactly one Agent event."""
    ledger = mapping(ledger)
    requests, denials = items(ledger.get("requests")), items(ledger.get("denials"))
    errors = []
    if ledger.get("audit_complete", True) is not True:
        errors.append("request_archive_audit_incomplete")
    ids = [mapping(r).get("request_id") for r in requests]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        errors.append("invalid_or_duplicate_request_ids")
    if [mapping(r).get("sequence") for r in requests] != list(range(1, len(requests) + 1)):
        errors.append("request_sequence_mismatch")
    if any(mapping(r).get("sent") is not True for r in requests):
        errors.append("unsent_request_in_sent_ledger")
    if ledger.get("sent_requests") != len(requests) or ledger.get("denied_requests") != len(denials):
        errors.append("ledger_count_mismatch")
    cap = ledger.get("max_requests", mapping(ledger.get("limits")).get("max_requests"))
    if not integer(cap) or cap < 1 or len(requests) > cap:
        errors.append("invalid_or_exceeded_request_cap")
    known_tokens = sum(mapping(r.get("usage")).get("total_tokens", 0)
                       for r in requests if isinstance(r, dict) and integer(mapping(r.get("usage")).get("total_tokens")))
    complete = all(mapping(r).get("status") == "completed" and mapping(r).get("metrics_complete") is True for r in requests)
    if any(mapping(r).get("metrics_complete") is True and not integer(mapping(mapping(r).get("usage")).get("total_tokens")) for r in requests):
        errors.append("complete_usage_without_token_count")
    if ledger.get("model_tokens_known") != known_tokens or ledger.get("metrics_complete") is not complete:
        errors.append("ledger_usage_mismatch")
    configured_model = mapping(detail.get("model_config")).get("model")
    for request in requests:
        request = mapping(request)
        if (request.get("model") != configured_model or request.get("max_tokens") != 4096
                or request.get("temperature") != 0.1):
            errors.append("request_model_or_parameters_mismatch")
            break
    events = [mapping(e.get("data")) for e in items(detail.get("events")) if isinstance(e, dict) and e.get("type") == "agent"]
    covered, calls, tokens = [], 0, 0
    event_complete = True
    request_map = {r.get("request_id"): r for r in requests if isinstance(r, dict) and isinstance(r.get("request_id"), str)}
    for event in events:
        request_ids = items(event.get("request_ids"))
        if not integer(event.get("model_calls")) or not integer(event.get("tokens")):
            errors.append("invalid_agent_metrics")
            continue
        selected = [request_map[i] for i in request_ids if isinstance(i, str) and i in request_map]
        invocation = event.get("invocation_id")
        if (not isinstance(invocation, str) or not invocation or len(selected) != len(request_ids)
                or event["model_calls"] != len(selected)
                or any(r.get("invocation_id") != invocation for r in selected)):
            errors.append("agent_request_link_mismatch")
        sent_tokens = sum(mapping(r.get("usage")).get("total_tokens", 0) for r in selected
                          if integer(mapping(r.get("usage")).get("total_tokens")))
        sent_complete = all(r.get("status") == "completed" and r.get("metrics_complete") is True for r in selected)
        if event["tokens"] != sent_tokens or event.get("metrics_complete") is not sent_complete:
            errors.append("agent_usage_mismatch")
        covered.extend(request_ids)
        calls += event["model_calls"]
        tokens += event["tokens"]
        event_complete = event_complete and event.get("metrics_complete") is True
    if (any(not isinstance(i, str) for i in covered) or len(covered) != len(ids)
            or set(covered) != set(ids) or len(set(covered)) != len(covered)):
        errors.append("unaccounted_or_reused_http_request")
    if (detail.get("model_calls") != calls or detail.get("model_tokens") != tokens
            or detail.get("model_metrics_complete") is not event_complete
            or calls != len(requests) or tokens != known_tokens or event_complete != complete):
        errors.append("run_agent_ledger_totals_mismatch")
    return {"verified": not errors, "errors": sorted(set(errors)), "sent_requests": len(requests),
            "denied_requests": len(denials), "model_tokens_known": known_tokens,
            "archive_audit_complete": ledger.get("audit_complete", True) is True,
            "metrics_complete": complete, "pending_requests": sum(mapping(r).get("status") == "pending" for r in requests),
            "max_requests": cap, "denial_reasons": [mapping(d).get("reason") for d in denials]}


def _raw_test(test):
    return {key: value for key, value in mapping(test).items() if key != "evidence_scope"}


def _scoped_tests(detail):
    """Only trusted Engine/storage fields, never model-generated test claims."""
    output = []
    for step in items(detail.get("steps")):
        value = mapping(mapping(step).get("output"))
        if isinstance(value.get("test"), dict):
            output.append(value["test"])
    contexts = [mapping(detail.get("context"))]
    contexts += [mapping(mapping(snapshot).get("context")) for snapshot in items(detail.get("snapshots"))]
    for context in contexts:
        for value in (context.get("test"), mapping(context.get("previous_validation")).get("test")):
            if isinstance(value, dict):
                output.append(value)
    for event in items(detail.get("events")):
        if mapping(event).get("type") in {"validation", "feedback"}:
            value = mapping(event.get("data")).get("test")
            if isinstance(value, dict):
                output.append(value)
    return output


def _live(data):
    data = mapping(data)
    return (data.get("model_mode") == "live" and integer(data.get("model_calls"))
            and data["model_calls"] > 0 and data.get("status") != "failed")


def classify(detail, task, budget_ledger):
    detail, task = mapping(detail), mapping(task)
    events = [e for e in items(detail.get("events")) if isinstance(e, dict)]
    event_ids = [e.get("id") for e in events]
    ordered = all(type(i) is int for i in event_ids) and all(a < b for a, b in zip(event_ids, event_ids[1:]))
    steps = {s["id"]: s for s in items(detail.get("steps")) if isinstance(s, dict) and isinstance(s.get("id"), str)}
    arm = detail.get("comparison_arm")
    budget = budget_audit(detail, budget_ledger)
    injected = bool(detail.get("fault_injection") is not False or detail.get("synthetic_defect") or detail.get("preset_fault")
                    or any(mapping(mapping(e.get("data")).get("output")).get("preset_fault") is True for e in events))
    provenance = bool(ordered and arm in ARMS and detail.get("mode") == "live" and not injected
                      and detail.get("candidate_origin") == "live_model_development"
                      and detail.get("workflow_id") == "development" and detail.get("task_id") == task.get("id")
                      and mapping(detail.get("task")).get("id") == task.get("id")
                      and sha(task.get("snapshot_sha256")) and mapping(detail.get("task")).get("snapshot_sha256") == task["snapshot_sha256"]
                      and isinstance(task.get("requirement"), str)
                      and str(detail.get("requirement", "")).strip() == task["requirement"].strip()
                      and detail.get("applied") is False)
    scoped_tests = _scoped_tests(detail)
    candidates = []
    all_writes = [(index, e) for index, e in enumerate(events) if e.get("type") == "tool"
                  and mapping(e.get("data")).get("tool") == "candidate_diff"
                  and mapping(e.get("data")).get("source") == "runtime"
                  and mapping(e.get("data")).get("status") == "completed"]
    for index, event in enumerate(events):
        node, data = event.get("node_id"), mapping(event.get("data"))
        match = re.fullmatch(r"round-([12])/fix", node) if isinstance(node, str) else None
        if event.get("type") != "agent" or not (node == "develop" or match) or not _live(data):
            continue
        output = mapping(data.get("output"))
        files = items(output.get("files"))
        if (len(files) != 1 or not isinstance(files[0], dict) or files[0].get("path") != "todo.py"
                or not isinstance(files[0].get("content"), str) or not files[0]["content"]
                or steps.get(node, {}).get("status") != "succeeded" or steps[node].get("output") != output
                or output.get("preset_fault") is True):
            continue
        number = 0 if node == "develop" else int(match.group(1))
        source = files[0]["content"]
        hash_value = fingerprint({"files": [{"path": "todo.py", "after": source}]})
        scope = {"id": f"round-{number}/verify", "attempt": number, "candidate_hash": hash_value}
        writes = [(i, e) for i, e in all_writes if i > index and e.get("node_id") == node
                  and fingerprint(mapping(e.get("data")).get("output")) == hash_value]
        tests = []
        for ti, te in enumerate(events):
            td = mapping(te.get("data"))
            if (not writes or ti <= writes[0][0] or te.get("type") != "tool"
                    or te.get("node_id") != scope["id"] + "/test"
                    or td.get("source") != "runtime" or td.get("tool") != "run_acceptance"
                    or td.get("status") not in {"completed", "failed"}):
                continue
            test = mapping(td.get("output"))
            matched = next((st for st in scoped_tests if st.get("evidence_scope") == scope and _raw_test(st) == test), None)
            preceding_writes = [(wi, we) for wi, we in all_writes if wi < ti]
            last_write = preceding_writes[-1][1] if preceding_writes else {}
            if (matched is None or last_write.get("node_id") != node
                    or fingerprint(mapping(last_write.get("data")).get("output")) != hash_value):
                continue
            if any(e.get("type") == "validation" and e.get("node_id") == scope["id"] for e in events[index + 1:]):
                continue
            tests.append((ti, te, test, matched))
        latest = tests[-1] if tests else None
        raw = latest[2] if latest else {}
        scope_test = latest[3] if latest else {}
        complete = bool(provenance and latest and complete_suite(raw, task))
        review_node = scope["id"] + "/review"
        step_review = mapping(steps.get(review_node, {}).get("output"))
        raw_review = {k: v for k, v in step_review.items() if k != "evidence_scope"}
        reviews = [e for e in events if e.get("type") == "agent" and e.get("node_id") == review_node
                   and _live(e.get("data")) and mapping(e.get("data")).get("output") == raw_review
                   and type(raw_review.get("passed")) is bool and isinstance(raw_review.get("issues"), list)]
        valid_review = bool(arm != "single_direct" and latest and reviews
                            and steps.get(review_node, {}).get("status") == "succeeded"
                            and step_review.get("evidence_scope") == scope and reviews[-1]["id"] > latest[1]["id"])
        gates = [e for e in events if e.get("type") == "validation" and e.get("node_id") == f"round-{number}/gate"
                 and mapping(e.get("data")).get("test") == scope_test and latest and e["id"] > latest[1]["id"]
                 and type(mapping(e.get("data")).get("passed")) is bool]
        gate = gates[-1] if gates else {}
        gate_data = mapping(gate.get("data"))
        if arm != "single_direct" and (not valid_review or gate_data.get("review") != step_review):
            gate_data = {}
        candidates.append({"round": number, "candidate_node": node, "candidate_event_id": event["id"],
                           "candidate_sha256": hash_value, "source_sha256": source_hash(source),
                           "write_event_id": writes[0][1]["id"] if writes else None,
                           "test_event_id": latest[1]["id"] if latest else None,
                           "test_timestamp": latest[1].get("timestamp") if latest else None,
                           "test_suite_completed": complete, "test_status": raw.get("status", "not_observed"),
                           "test_passed": complete and raw.get("status") == "passed",
                           "natural_test_failure": complete and raw.get("status") == "failed",
                           "acceptance_sha256": raw.get("acceptance_sha256"),
                           "independent_review_observed": valid_review,
                           "review_passed": raw_review.get("passed") if valid_review else None,
                           "gate_passed": gate_data.get("passed"), "gate_event_id": gate.get("id") if gate_data else None,
                           "_scoped_test": scope_test, "_review": step_review if valid_review else None})
    unique = len({c["candidate_node"] for c in candidates}) == len(candidates)
    if arm == "single_direct":
        agent_ids = {mapping(e.get("data")).get("agent_id") for e in events if e.get("type") == "agent"}
        provenance = provenance and agent_ids == {"single_direct"}
    evidence_valid = bool(provenance and unique and budget["verified"])
    final_hash = fingerprint(detail.get("diff"))
    final = candidates[-1] if candidates else {}
    last_write_hash = fingerprint(mapping(all_writes[-1][1].get("data")).get("output")) if all_writes else None
    final_linked = bool(evidence_valid and final_hash and final_hash == final.get("candidate_sha256") == last_write_hash)
    complete_final = final_linked and final.get("test_suite_completed") is True
    terminal = detail.get("status") in TERMINAL
    primary_success = bool(terminal and complete_final and final.get("test_passed"))
    repairs = []
    for previous, current in zip(candidates, candidates[1:]):
        feedback = [e for e in events if e.get("type") == "feedback"
                    and e.get("node_id") == f"round-{previous['round']}/gate"
                    and mapping(e.get("data")).get("next_attempt") == current["round"]
                    and mapping(e.get("data")).get("test") == previous["_scoped_test"]
                    and previous.get("test_event_id") is not None
                    and previous.get("gate_event_id") is not None
                    and previous["test_event_id"] < previous["gate_event_id"] < e["id"] < current["candidate_event_id"]]
        is_natural = previous["natural_test_failure"]
        is_review = previous["test_passed"] and previous["review_passed"] is False
        linked = bool(evidence_valid and current["round"] == previous["round"] + 1 and len(feedback) == 1
                      and current["candidate_sha256"] != previous["candidate_sha256"]
                      and previous["test_suite_completed"] and current["test_suite_completed"]
                      and previous["gate_passed"] is False
                      and (is_natural or is_review))
        repairs.append({"from_round": previous["round"], "to_round": current["round"],
                        "feedback_event_id": feedback[0]["id"] if feedback else None,
                        "linked": linked, "trigger": "natural_test_failure" if is_natural else "review_rejection" if is_review else "unverified",
                        "retest_passed": linked and current["test_passed"]})
    natural = evidence_valid and any(c["natural_test_failure"] for c in candidates)
    review_rejected = evidence_valid and any(c["test_passed"] and c["review_passed"] is False for c in candidates)
    agent_events = [mapping(e.get("data")) for e in events if e.get("type") == "agent"]
    contract_retries = sum(e.get("contract_retries", 0) for e in agent_events if integer(e.get("contract_retries", 0)))
    diagnostics = sum(len(items(e.get("model_diagnostics"))) for e in agent_events)
    unresolved_protocol = any(e.get("status") == "failed" and any(t in str(e.get("error", "")) for t in
                              ("JSON", "契约", "强制最终输出", "choices/message", "模型工具")) for e in agent_events)
    native_success = bool(primary_success and detail.get("status") == "succeeded" and final.get("gate_passed") is True
                          and final.get("round") == detail.get("attempt")
                          and (arm == "single_direct" or final.get("review_passed") is True))
    primary_result = ("ineligible" if not evidence_valid else "complete_verified" if complete_final else
                      "incomplete" if candidates else "not_reached")
    first_pass = next((c for c in candidates if c["test_passed"]), None)
    return {"schema": SCHEMA, "run_id": detail.get("id"), "task_id": task.get("id"), "arm": arm,
            "native_status": detail.get("status"), "terminal": terminal, "evidence_valid": evidence_valid,
            "provenance_verified": bool(provenance and unique), "budget_audit": budget,
            "primary_result": primary_result, "primary_success": primary_success,
            "functional_test_passed": bool(complete_final and final.get("test_passed")),
            "strategy_completed": native_success, "independent_review_applicable": arm != "single_direct",
            "independent_review_observed": final.get("independent_review_observed", False),
            "natural_test_failure_observed": bool(natural), "review_only_failure_observed": bool(review_rejected),
            "automatic_code_repair_observed": any(r["linked"] for r in repairs),
            "natural_failure_repair_succeeded": bool(primary_success and any(r["linked"] and r["trigger"] == "natural_test_failure" and r["retest_passed"] for r in repairs)),
            "review_triggered_repair_succeeded": bool(primary_success and any(r["linked"] and r["trigger"] == "review_rejection" and r["retest_passed"] for r in repairs)),
            "contract_retries": contract_retries, "format_error_responses": diagnostics,
            "unresolved_protocol_failure": unresolved_protocol,
            "synthetic_defect_present": injected, "fault_injection": detail.get("fault_injection"),
            "model_calls": budget["sent_requests"], "model_tokens_known": budget["model_tokens_known"],
            "model_metrics_complete": budget["metrics_complete"], "budget_denials": budget["denied_requests"],
            "run_duration_seconds": seconds(detail.get("created_at"), detail.get("updated_at")),
            "time_to_first_passing_test_seconds": seconds(detail.get("created_at"), first_pass.get("test_timestamp")) if first_pass and evidence_valid else None,
            "final_test_completed_seconds": seconds(detail.get("created_at"), final.get("test_timestamp")) if complete_final else None,
            "repair_rounds": detail.get("attempt", 0), "applied": detail.get("applied"),
            "failure_reason": None if native_success else detail.get("summary"),
            "rounds": [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates], "repair_chain": repairs}


def summarize(plan, trials):
    """Use every preregistered trial as the denominator, including missing runs."""
    planned = items(mapping(plan).get("trials"))
    actual_trials = [t for t in items(trials) if isinstance(t, dict)]
    observed = {t.get("trial"): t for t in actual_trials}
    planned_ids = [mapping(t).get("trial") for t in planned]
    duplicate_ids = [trial for trial in observed if sum(t.get("trial") == trial for t in actual_trials) > 1]
    unexpected = [t for t in actual_trials if t.get("trial") not in planned_ids]
    rows = []
    for intended in planned:
        intended = mapping(intended)
        actual = mapping(observed.get(intended.get("trial")))
        report = mapping(actual.get("report"))
        matches = (not report or (report.get("arm") == intended.get("arm") and report.get("task_id") == intended.get("task_id")))
        if report and (not matches or intended.get("trial") in duplicate_ids):
            report = {**report, "summary_integrity_error": "trial_metadata_mismatch_or_duplicate",
                      "evidence_valid": False, "primary_success": False, "strategy_completed": False,
                      "natural_test_failure_observed": False, "natural_failure_repair_succeeded": False,
                      "review_triggered_repair_succeeded": False}
        rows.append({**intended, "state": actual.get("state", actual.get("status", "not_submitted")),
                     "run_id": actual.get("run_id"), "report": report or None})
    by_arm = {}
    for arm in ARMS:
        group = [r for r in rows if r.get("arm") == arm]
        reports = [r["report"] for r in group if r["report"]]
        finished = [r for r in reports if r.get("terminal")]
        durations = [r["run_duration_seconds"] for r in finished if isinstance(r.get("run_duration_seconds"), (int, float))]
        by_arm[arm] = {
            "planned": len(group), "observed": len(reports), "finished": len(finished),
            "not_observed": len(group) - len(reports),
            "primary_successes": sum(r.get("primary_success") is True for r in reports),
            "primary_non_successes_including_unobserved": sum(r.get("primary_success") is not True for r in reports) + len(group) - len(reports),
            "strategy_completed": sum(r.get("strategy_completed") is True for r in reports),
            "natural_test_failure_trials": sum(r.get("natural_test_failure_observed") is True for r in reports),
            "natural_failure_repair_successes": sum(r.get("natural_failure_repair_succeeded") is True for r in reports),
            "review_triggered_repair_successes": sum(r.get("review_triggered_repair_succeeded") is True for r in reports),
            "contract_retries": sum(r.get("contract_retries", 0) for r in reports),
            "unresolved_protocol_failure_trials": sum(r.get("unresolved_protocol_failure") is True for r in reports),
            "model_calls": sum(r.get("model_calls", 0) for r in reports),
            "model_tokens_known": sum(r.get("model_tokens_known", 0) for r in reports),
            "model_metrics_complete_for_observed": all(r.get("model_metrics_complete") is True for r in reports),
            "budget_denials": sum(r.get("budget_denials", 0) for r in reports),
            "evidence_verified_trials": sum(r.get("evidence_valid") is True for r in reports),
            "median_duration_seconds_all_finished": statistics.median(durations) if durations else None,
            "duration_observations": len(durations),
        }
    return {"schema": SCHEMA, "batch_id": plan.get("batch_id"), "planned_trials": len(planned),
            "finished_trials": sum(r["report"] is not None and r["report"].get("terminal") is True for r in rows),
            "all_trials_retained": True, "primary_endpoint": "Final candidate passes all frozen independent Docker tests; native review/gate reported separately.",
            "same_budget_means": "Same ceilings, not equal actual token consumption or equal spending.",
            "synthetic_trials_included": False,
            "synthetic_trials_detected": sum(bool(r["report"] and r["report"].get("synthetic_defect_present")) for r in rows),
            "duplicate_observation_ids": duplicate_ids, "unexpected_trial_records": unexpected,
            "comparative_claim": "Descriptive small-sample results only; no superiority or significance claim.",
            "by_arm": by_arm, "trials": rows}
