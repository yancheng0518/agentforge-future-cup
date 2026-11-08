"""Verify and combine a stopped comparison with its explicitly registered phase.

Reads frozen evidence only: no model calls, candidate execution or parent writes.
Submitted parent failures remain in the original denominator. A local observer
interruption is an inference recorded separately from the original run state.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from comparison_report import classify, summarize
from comparison_continuation import IDENTITY, OBSERVER_CHANGES, SAME_CONDITIONS

SCHEMA = "agentforge-comparison-phase-merge-v1"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def inside(directory, relative):
    """Reject traversal, absolute paths and symlink components before reading."""
    require(isinstance(relative, str) and bool(relative), "Missing artifact path")
    path = PurePosixPath(relative.replace("\\", "/"))
    require(not path.is_absolute() and ":" not in relative and ".." not in path.parts,
            "Unsafe artifact path")
    base = Path(directory).resolve()
    target = base.joinpath(*path.parts)
    require(target.resolve().is_relative_to(base), "Artifact escapes its phase")
    require(not any(p.is_symlink() for p in [target, *target.parents] if p != base and p.is_relative_to(base)),
            "Artifact symlinks are forbidden")
    return target


def read_json(path):
    raw = Path(path).read_bytes()
    return json.loads(raw), digest(raw)


def load_phase(directory):
    directory = Path(directory).resolve()
    plan, plan_hash = read_json(inside(directory, "preregistration.json"))
    state, state_hash = read_json(inside(directory, "state.json"))
    require(plan.get("batch_id") == state.get("batch_id") == directory.name, "Phase batch ID mismatch")
    require(state.get("preregistration_sha256") == plan_hash, "Preregistration hash mismatch")
    manifest = plan.get("source_manifest", {})
    require(digest(canonical(manifest)) == plan.get("source_manifest_sha256"), "Manifest hash mismatch")
    for relative, item in manifest.items():
        raw = inside(directory / "frozen", relative).read_bytes()
        require(digest(raw) == item.get("sha256") and len(raw) == item.get("bytes"),
                "Frozen source changed: " + relative)
    # These are the current classifier's executable dependencies. Other source
    # differences are checked between phases, rather than importing archive code.
    for relative in ("scripts/comparison_report.py", "backend/agentforge/engine.py"):
        require(relative in manifest and digest((ROOT / relative).read_bytes()) == manifest[relative]["sha256"],
                "Current classifier dependency differs from frozen source: " + relative)
    return {"directory": directory, "plan": plan, "state": state,
            "preregistration_sha256": plan_hash, "state_sha256": state_hash}


def identity(row):
    return {key: row.get(key) for key in IDENTITY}


def inventory(phase):
    planned, observed = phase["plan"]["trials"], phase["state"]["trials"]
    require(len(planned) == len(observed), "Phase trial inventory mismatch")
    ids = [row.get("trial") for row in planned]
    require(len(ids) == len(set(ids)) and all(type(i) is int and i > 0 for i in ids), "Duplicate or invalid trial IDs")
    require([identity(t) for t in planned] == [identity(t) for t in observed], "Trial identity/order mismatch")
    return observed


def verify_artifacts(phase, evidence_path, evidence):
    run, ledger = evidence["run"], evidence["budget_ledger"]
    directory = phase["directory"]
    requests_verified, candidates_verified, unavailable = 0, 0, []
    for request in ledger.get("requests", []):
        if request.get("audit_error") or not request.get("artifact_path"):
            require(ledger.get("audit_complete") is False, "Unexplained missing request archive")
            unavailable.append({"kind": "request_archive", "request_id": request.get("request_id")})
            continue
        artifact, checksum = read_json(inside(directory, request["artifact_path"]))
        require(checksum == request.get("artifact_sha256"), "Request archive hash mismatch")
        expected = {k: v for k, v in request.items() if k not in {"artifact_path", "artifact_sha256"}}
        require(artifact.get("request") == expected, "Request archive ledger mismatch")
        payload = artifact.get("payload")
        body = json.dumps(payload).encode()
        require(digest(body) == request.get("payload_sha256") and len(body) == request.get("payload_bytes"),
                "Archived HTTP payload hash mismatch")
        require(payload.get("model") == phase["plan"]["model_config"]["model"]
                and payload.get("temperature") == phase["plan"]["model_config"].get("temperature")
                and payload.get("max_tokens") == phase["plan"]["limits"]["output_tokens_per_request"],
                "Archived HTTP configuration mismatch")
        require(len(body) <= phase["plan"]["limits"]["utf8_payload_bytes_per_request"],
                "Archived HTTP payload exceeds frozen budget")
        requests_verified += 1
    expected_candidates = {}
    for event in run.get("events", []):
        output = event.get("data", {}).get("output")
        if event.get("type") != "agent" or not isinstance(output, dict):
            continue
        for item in output.get("files", []):
            if item.get("path") == "todo.py" and isinstance(item.get("content"), str):
                expected_candidates[event["id"]] = (event.get("node_id"), item["content"])
    artifacts = evidence.get("candidate_artifacts", [])
    require(len(artifacts) == len(expected_candidates)
            and {a.get("event_id") for a in artifacts} == set(expected_candidates), "Candidate artifact inventory mismatch")
    for item in artifacts:
        node, source = expected_candidates[item["event_id"]]
        require(item.get("node_id") == node and item.get("redacted") is False, "Unverifiable candidate archive")
        raw = inside(evidence_path.parent, item["path"]).read_bytes()
        require(raw == source.encode("utf-8") and digest(raw) == item.get("source_sha256"), "Candidate archive hash mismatch")
        candidates_verified += 1
    return {"request_archives_verified": requests_verified, "candidate_archives_verified": candidates_verified,
            "unavailable_archives": unavailable}


def failure_items(run, ledger, report, observer_interruption=False):
    """Supplementary attribution; never changes the frozen primary classifier."""
    failures, infrastructure = [], []
    agents = {e.get("data", {}).get("invocation_id"): e.get("data", {}) for e in run.get("events", [])
              if e.get("type") == "agent"}
    for request in ledger.get("requests", []):
        if request.get("status") != "failed":
            continue
        error = str(agents.get(request.get("invocation_id"), {}).get("error", ""))
        match = re.search(r"\bHTTP(?:Error)?\s*[:=]?\s*([45]\d{2})\b", error, re.IGNORECASE)
        failures.append({"request_id": request.get("request_id"), "invocation_id": request.get("invocation_id"),
                         "http_status": int(match[1]) if match else None,
                         "status_source": "linked_agent_error" if match else "unknown",
                         "transport_error_type": request.get("error_type")})
    for event in run.get("events", []):
        data = event.get("data", {})
        if event.get("type") == "tool" and data.get("source") == "runtime" and data.get("status") in {"failed", "blocked"}:
            output = data.get("output")
            # A complete failed acceptance suite is a code failure, not infra.
            natural_ids = {r.get("test_event_id") for r in report.get("rounds", []) if r.get("natural_test_failure")}
            if event.get("id") not in natural_ids:
                infrastructure.append({"kind": "runtime_tool_incomplete", "event_id": event.get("id"),
                                       "tool": data.get("tool"), "status": data.get("status"),
                                       "output_status": output.get("status") if isinstance(output, dict) else None})
    if ledger.get("audit_complete") is False:
        infrastructure.append({"kind": "request_archive_failure"})
    if observer_interruption:
        infrastructure.append({"kind": "local_observer_interruption", "attribution": "inferred",
                               "basis": "Stopped phase has local filesystem error; last submitted run is blocked with an incomplete runtime tool."})
    return {"transport_failures": failures,
            "http_status_failures": dict(sorted(Counter(str(f["http_status"]) for f in failures if f["http_status"] is not None).items())),
            "budget_denials_before_http": deepcopy(ledger.get("denials", [])),
            "infrastructure_items": infrastructure, "local_observer_interruption_inferred": observer_interruption,
            "natural_failure": report.get("natural_test_failure_observed") is True,
            "unresolved_protocol_failure": report.get("unresolved_protocol_failure") is True,
            "format_retries": report.get("contract_retries", 0),
            "synthetic_defect": report.get("synthetic_defect_present") is True}


def verify_trial(phase, row, infer_observer=False):
    if not row.get("run_id"):
        require(row.get("state") in {"not_submitted", "pending"} and not row.get("report"), "Ambiguous unsubmitted slot")
        return deepcopy(row), None
    path = inside(phase["directory"], row.get("evidence_path"))
    evidence, evidence_hash = read_json(path)
    require(identity(evidence["trial"]) == identity(row) and evidence["trial"].get("run_id") == row["run_id"],
            "Evidence trial identity mismatch")
    run, ledger = evidence["run"], evidence["budget_ledger"]
    require(run.get("id") == row["run_id"] and run.get("comparison_arm") == row["arm"]
            and run.get("task_id") == row["task_id"], "Evidence run identity mismatch")
    require(run.get("model_config") == phase["plan"]["model_config"], "Run model configuration differs from registration")
    require(ledger.get("run_id", row["run_id"]) == row["run_id"], "Ledger run identity mismatch")
    require(ledger.get("max_requests") == phase["plan"]["limits"]["http_requests_per_trial"],
            "Ledger request ceiling differs from frozen budget")
    task = phase["plan"]["task_catalog"][row["task_id"]]
    report = classify(run, task, ledger)
    stored_classification, _ = read_json(inside(path.parent, "classification.json"))
    require(report == evidence.get("report") == row.get("report") == stored_classification,
            "Frozen classification differs from evidence reclassification")
    require(row.get("native_status") == run.get("status"), "Original native status differs from evidence")
    artifact_audit = verify_artifacts(phase, path, evidence)
    blocked_tool = any(e.get("type") == "tool" and e.get("data", {}).get("source") == "runtime"
                       and e.get("data", {}).get("status") == "blocked" for e in run.get("events", []))
    observer = bool(infer_observer and run.get("status") == "blocked" and blocked_tool and not report["primary_success"])
    row = deepcopy(row)
    row.update(phase=phase["directory"].name, failure_attribution=failure_items(run, ledger, report, observer))
    return row, {"trial": row["trial"], "phase": phase["directory"].name, "evidence_path": row["evidence_path"],
                 "evidence_sha256": evidence_hash, "reclassification_equal": True, **artifact_audit}


def phase_summary(phase, rows):
    summary = summarize({**phase["plan"], "trials": [identity(r) for r in rows]}, rows)
    return {"batch_id": phase["directory"].name, "original_status": phase["state"].get("status"),
            "preregistration_sha256": phase["preregistration_sha256"], "state_sha256": phase["state_sha256"],
            "retained_trial_ids": [r["trial"] for r in rows], "planned_trials": len(phase["plan"]["trials"]),
            "included_trials": len(rows), "by_arm": summary["by_arm"],
            "original_error": phase["state"].get("error"),
            "preflight_controls": deepcopy(phase["plan"].get("controls")),
            "preflight_controls_in_live_denominator": False,
            "started_at": phase["state"].get("started_at", phase["plan"].get("created_at")),
            "finished_at": phase["state"].get("finished_at")}


def merge(parent_dir, child_dir, output=None):
    """Return verified JSON data and exclusively create child/combined-summary.json."""
    parent, child = load_phase(parent_dir), load_phase(child_dir)
    require(parent["directory"] != child["directory"], "Parent and child must differ")
    require(not child["directory"].is_relative_to(parent["directory"]), "Child must be outside immutable parent")
    require(parent["state"].get("status") in {"blocked", "interrupted"}, "Parent must be stopped")
    require(child["state"].get("status") in {"completed", "blocked", "interrupted"}, "Child must be terminal before merging")
    reference = child["plan"].get("continuation", {})
    require(reference.get("parent_batch") == parent["directory"].name, "Continuation parent mismatch")
    for key in ("preregistration_sha256", "state_sha256"):
        require(reference.get("parent_" + key) == parent[key], "Continuation parent hash mismatch: " + key)
    for key in SAME_CONDITIONS:
        require(parent["plan"].get(key) == child["plan"].get(key), "Frozen condition changed: " + key)
    old, new = parent["plan"]["source_manifest"], child["plan"]["source_manifest"]
    changes = {key: {"before": old.get(key), "after": new.get(key)}
               for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)}
    require(not (set(changes) - OBSERVER_CHANGES), "Non-observer source changed between phases")
    require(reference.get("source_changes") == changes, "Source-change declaration is incomplete or incorrect")
    original, continued = inventory(parent), inventory(child)
    retained = [r for r in original if r.get("run_id")]
    selected = [r for r in original if not r.get("run_id")]
    require(all(r.get("state") in {"complete", "interrupted", "observer_interrupted"} for r in retained),
            "Ambiguous parent submission state")
    require(all(r.get("state") == "not_submitted" and not r.get("report") for r in selected),
            "Only never-submitted parent slots may continue")
    require(reference.get("retained_parent_trials") == [r["trial"] for r in retained]
            and reference.get("selected_trial_ids") == [r["trial"] for r in selected], "Continuation selection mismatch")
    require([identity(r) for r in selected] == [identity(r) for r in continued], "Continuation changes original trial slots")
    run_ids = [r["run_id"] for r in retained + continued if r.get("run_id")]
    require(len(run_ids) == len(set(run_ids)), "Duplicate submitted run IDs")
    local_error = bool(re.search(r"WinError\s*5|PermissionError|Permission denied|拒绝访问", str(parent["state"].get("error", "")), re.IGNORECASE))
    rows, audits, groups = [], [], []
    for phase, records in ((parent, retained), (child, continued)):
        group = []
        for row in records:
            infer = bool(phase is parent and local_error and row is retained[-1])
            checked, audit = verify_trial(phase, row, infer)
            group.append(checked)
            rows.append(checked)
            if audit:
                audits.append(audit)
        groups.append(group)
    result = summarize(parent["plan"], rows)
    lookup = {r["trial"]: r for r in rows}
    for row in result["trials"]:
        actual = lookup[row["trial"]]
        row.update(phase=actual.get("phase", child["directory"].name),
                   original_state=actual.get("state"), original_native_status=actual.get("native_status"),
                   failure_attribution=actual.get("failure_attribution"), evidence_path=actual.get("evidence_path"))
    statuses = Counter()
    for row in rows:
        statuses.update(row.get("failure_attribution", {}).get("http_status_failures", {}))
    result.update(combined_schema=SCHEMA, continuation_batch_id=child["directory"].name,
                  phases=[phase_summary(p, g) for p, g in zip((parent, child), groups)],
                  source_changes=changes, evidence_audits=audits,
                  stage_break={"present": True, "parent_status_preserved": parent["state"]["status"],
                               "local_observer_failure_inferred": local_error,
                               "affected_trial_ids": [r["trial"] for r in rows if r.get("failure_attribution", {}).get("local_observer_interruption_inferred")],
                               "limitation": "Separate temporal phases with disclosed observer-source changes; no submitted parent trial was rerun and no original state was relabelled."},
                  http_status_failures=dict(sorted(statuses.items())),
                  all_original_planned_slots_retained=len(rows) == len(original))
    destination = Path(output).resolve() if output else child["directory"] / "combined-summary.json"
    require(destination == child["directory"] / "combined-summary.json", "Output must be child/combined-summary.json")
    # Exclusive creation makes repeated merges explicit and never alters evidence.
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True, type=Path)
    parser.add_argument("--child", required=True, type=Path)
    args = parser.parse_args(argv)
    result = merge(args.parent, args.child)
    print(json.dumps({"combined_summary": str(args.child.resolve() / "combined-summary.json"),
                      "planned_trials": result["planned_trials"], "finished_trials": result["finished_trials"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
