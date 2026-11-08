"""Preregister and run controlled tasks through the real local workflow API.

No candidate is generated, injected, edited, applied, or executed by this script.
Each planned trial is submitted once; failed trials stay in the denominator.
Resume observes an existing run rather than restarting it. A lost POST response
is left uncertain instead of risking duplicate billable submissions.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from agentforge.engine import candidate_fingerprint
from agentforge.runtime import Runtime
from run_live_demo import api_request, extract_outcome, redact

TASKS = ("completion", "bulk_edit", "priority_query", "json_merge")
SCHEMA = "agentforge-real-task-evaluation-v1"
TERMINAL = {"succeeded", "needs_human", "failed", "blocked"}
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_json(path, value, secret=""):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(redact(value, secret), ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows readers/antivirus can briefly deny atomic replacement. Retry
    # only this same serialized file, never the surrounding model/workflow.
    # Six attempts sleep at most 1.55 seconds in total; preserve the pending
    # file and propagate the final original exception when access stays denied.
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == len(_REPLACE_RETRY_DELAYS):
                raise
            time.sleep(_REPLACE_RETRY_DELAYS[attempt])


@contextmanager
def batch_lock(directory):
    path = directory / ".evaluation.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("Evaluation lock exists. Check that its process has stopped before removing a stale lock; do not launch a second evaluator.") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at": now()}, handle)
        yield
    finally:
        path.unlink(missing_ok=True)


def source_files(root, task_ids):
    """Only project implementation and declared tasks; never .env or answers."""
    paths = set()
    for folder, suffixes in (("backend/agentforge", {".py"}), ("acceptance", {".py"}),
                             ("knowledge", {".md"}), ("scripts", {".py", ".ps1", ".sh"})):
        directory = root / folder
        if directory.exists():
            paths.update(p for p in directory.rglob("*") if p.is_file() and p.suffix in suffixes)
    for relative in ("backend/requirements-lock.txt", "sandbox/Dockerfile", "examples/todo/todo.py"):
        if (root / relative).is_file():
            paths.add(root / relative)
    for task_id in task_ids:
        if task_id != "completion":
            directory = root / "benchmarks/tasks" / task_id
            for name in ("todo.py", "requirement.md", "test_todo.py"):
                path = directory / name
                if not path.is_file():
                    raise RuntimeError(f"Task source missing: benchmarks/tasks/{task_id}/{name}")
                paths.add(path)
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("Symlink or external source cannot be frozen.")
    return sorted(paths)


def freeze_sources(root, directory, task_ids, secret):
    manifest = {}
    for path in source_files(root, task_ids):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        if secret and secret.encode() in content:
            raise RuntimeError("A project source contains the configured credential; refusing to archive it.")
        destination = directory / "frozen" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        manifest[relative] = {"sha256": digest(content), "bytes": len(content)}
    return manifest


def verify_frozen(root, directory, manifest, compare_current=True):
    for relative, item in manifest.items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise RuntimeError("Invalid frozen manifest path.")
        archive = directory / "frozen" / relative
        if not archive.is_file() or archive.is_symlink() or digest(archive.read_bytes()) != item["sha256"]:
            raise RuntimeError(f"Frozen archive changed: {relative}")
        if compare_current:
            current = root / relative
            if not current.is_file() or current.is_symlink() or digest(current.read_bytes()) != item["sha256"]:
                raise RuntimeError(f"Project changed after preregistration: {relative}. Preserve this batch and start a separately declared batch.")


def safe_settings(settings):
    return {key: settings.get(key) for key in ("model", "base_url", "timeout", "image", "docker_backend", "wsl_distro", "min_request_interval")}


def expected_model_config(settings):
    return {"model": settings.get("model"), "base_url": settings.get("base_url"), "temperature": 0.1,
            "max_output_tokens": 4096, "tool_rounds": 4, "min_request_interval": settings.get("min_request_interval", 0.0),
            "timeout_seconds": settings.get("timeout"), "adapter_revision": 2, "contract_retries": 1,
            "automatic_tool_rounds": 2}


def local_api_url(value):
    parsed = urlsplit(value)
    return (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and not any((parsed.username, parsed.password, parsed.query, parsed.fragment)))


def preregister(root, directory, base, task_ids, repetitions, interval, max_calls, timeout, settings, request=api_request):
    catalog = request(base, "/tasks")
    if isinstance(catalog, dict):
        catalog = catalog.get("tasks", [])
    selected = {item["id"]: item for item in catalog if isinstance(item, dict) and item.get("id") in task_ids}
    if set(selected) != set(task_ids):
        raise RuntimeError("Local API does not expose all preregistered task IDs.")
    for item in selected.values():
        if not isinstance(item.get("requirement"), str) or not item["requirement"].strip() or item.get("expected_tests") != 10:
            raise RuntimeError("Every task must declare its full requirement and exactly ten independent tests.")
        if not re.fullmatch(r"[a-f0-9]{64}", str(item.get("snapshot_sha256", ""))):
            raise RuntimeError("Every task must expose its immutable snapshot hash.")
        for kind in ("baseline", "requirement", "acceptance"):
            relative = item.get("paths", {}).get(kind)
            if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise RuntimeError("Task catalog must expose controlled baseline/requirement/acceptance paths.")
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError("Task catalog path escapes the project.")
            if digest(path.read_text(encoding="utf-8-sig").encode()) != item.get(kind + "_sha256"):
                raise RuntimeError("Task catalog hashes do not match the local sources; do not preregister a different server checkout.")
    agents, workflows = request(base, "/agents"), request(base, "/workflows")
    workflow = next((w for w in workflows if w.get("id") == "development"), None)
    if workflow is None or workflow.get("max_repairs") != 2:
        raise RuntimeError("The evaluated development workflow must declare at most two repair rounds, exactly as preregistered.")
    directory.mkdir(parents=True, exist_ok=False)
    manifest = freeze_sources(root, directory, task_ids, settings.get("api_key", ""))
    trials = [{"trial": index + 1, "task_id": task, "repetition": repetition, "state": "pending", "run_id": None}
              for index, (repetition, task) in enumerate((repeat, task) for repeat in range(1, repetitions + 1) for task in task_ids)]
    plan = {"schema": SCHEMA, "batch_id": directory.name, "created_at": now(), "api_base": base,
            "task_order": list(task_ids), "repetitions": repetitions, "task_catalog": selected,
            "candidate_provenance": "live_developer_output_without_injection", "synthetic_defects": False,
            "selection_policy": "All preregistered trials retained, including failures; no selective retries.",
            "interval_seconds": interval, "max_model_calls": max_calls,
            "budget_semantics": "Submission guard checked before each new trial; an active workflow may finish beyond this count.",
            "observation_timeout_seconds": timeout, "max_repairs": 2,
            "model_settings": safe_settings(settings),
            "model_config": expected_model_config(settings),
            "model_request_parameters": {"temperature": 0.1, "max_tokens": 4096, "max_tool_rounds": 4,
                                         "adapter_revision": 2, "max_contract_retries": 1,
                                         "automatic_tool_rounds": 2,
                                         "contract_retry_budget": "Within four total invocation requests: at most two automatic tool rounds, then a tools-disabled answer, reserving one tools-disabled contract retry.",
                                         "min_request_interval": settings.get("min_request_interval"),
                                         "thinking": "disabled for Omniedu DeepSeek V4", "source_of_truth": "frozen/backend/agentforge/runtime.py"},
            "agent_versions": agents, "workflow": workflow, "source_manifest": manifest,
            "source_manifest_sha256": digest(canonical(manifest)), "trials": trials}
    write_json(directory / "preregistration.json", plan, settings.get("api_key", ""))
    state = {"schema": SCHEMA, "batch_id": directory.name, "status": "preregistered", "created_at": now(),
             "preregistration_sha256": digest((directory / "preregistration.json").read_bytes()),
             "trials": deepcopy(trials), "last_submission_epoch": None}
    write_json(directory / "state.json", state)
    return plan, state


def valid_live_candidate(event, steps):
    data, node = event.get("data", {}), event.get("node_id", "")
    if not isinstance(node, str):
        return False
    files = data.get("output", {}).get("files", [])
    valid_node = node == "develop" or re.fullmatch(r"round-[12]/fix", node)
    step = steps.get(node, {})
    return bool(valid_node and event.get("type") == "agent" and data.get("model_mode") == "live"
                and data.get("model_calls", 0) > 0 and data.get("status") != "failed"
                and step.get("status") == "succeeded" and step.get("output") == data.get("output")
                and len(files) == 1 and isinstance(files[0], dict) and files[0].get("path") == "todo.py"
                and isinstance(files[0].get("content"), str) and data["output"].get("preset_fault") is not True)


def complete_suite(test, expected=10):
    status = test.get("status")
    if status not in {"passed", "failed"} or test.get("isolated") is not True:
        return False
    passed = status == "passed"
    marker = 'AGENTFORGE_ACCEPTANCE=' + json.dumps({"tests": expected, "passed": passed})
    return (type(test.get("exit_code")) is int and test["exit_code"] == (0 if passed else 1)
            and marker in test.get("stdout", "")
            and re.search(rf"Ran {expected} tests? in ", test.get("stderr", "")) is not None)


def classify_run(detail, declared_task, provenance="live_developer_output_without_injection"):
    """Conservative natural-failure attribution, separate from server status."""
    outcome = extract_outcome(detail)
    events = detail.get("events", [])
    ids = [event.get("id") for event in events]
    ordered = all(type(i) is int for i in ids) and all(a < b for a, b in zip(ids, ids[1:]))
    steps = {step["id"]: step for step in detail.get("steps", [])}
    eligible = (ordered and provenance == "live_developer_output_without_injection" and detail.get("mode") == "live"
                and detail.get("workflow_id") == "development" and detail.get("task_id", "completion") == declared_task["id"]
                and detail.get("task", {}).get("id") == declared_task["id"]
                and re.fullmatch(r"[a-f0-9]{64}", str(declared_task.get("snapshot_sha256", "")))
                and detail.get("task", {}).get("snapshot_sha256") == declared_task["snapshot_sha256"]
                and str(detail.get("requirement", "")).strip() == declared_task["requirement"].strip()
                and detail.get("candidate_origin") == "live_model_development" and detail.get("fault_injection") is False
                and not detail.get("synthetic_defect") and not detail.get("preset_fault"))
    rounds = []
    for position, event in enumerate(events):
        if not valid_live_candidate(event, steps):
            continue
        node = event["node_id"]
        round_number = 0 if node == "develop" else int(node.split("/")[0].split("-")[1])
        source = event["data"]["output"]["files"][0]["content"]
        fingerprint = candidate_fingerprint({"files": [{"path": "todo.py", "after": source}]})
        verify_id = f"round-{round_number}/verify"
        step_output = steps.get(verify_id + "/test", {}).get("output") or {}
        step_test = step_output.get("test") or {}
        scope = step_test.get("evidence_scope", {})
        raw_test = {key: value for key, value in step_test.items() if key != "evidence_scope"}
        writes = [(i, e) for i, e in enumerate(events[position + 1:], position + 1)
                  if e.get("type") == "tool" and e.get("node_id") == node and e.get("data", {}).get("source") == "runtime"
                  and e["data"].get("tool") == "candidate_diff" and e["data"].get("status") == "completed"
                  and candidate_fingerprint(e["data"].get("output", {})) == fingerprint]
        tests = [(i, e) for i, e in enumerate(events[position + 1:], position + 1)
                 if e.get("type") == "tool" and e.get("node_id") == verify_id + "/test"
                 and e.get("data", {}).get("source") == "runtime" and e["data"].get("tool") == "run_acceptance"
                 and e["data"].get("output") == raw_test]
        linked = bool(eligible and writes and tests and writes[0][0] < tests[-1][0]
                      and scope == {"id": verify_id, "attempt": round_number, "candidate_hash": fingerprint})
        if any(e.get("type") == "validation" and e.get("node_id") == verify_id for e in events[position + 1:]):
            linked = False
        gates = [e for e in events if e.get("type") == "validation" and e.get("node_id") == f"round-{round_number}/gate"
                 and e.get("data", {}).get("test") == step_test
                 and e["data"].get("review", {}).get("evidence_scope") == scope]
        gate = gates[-1].get("data", {}) if gates else {}
        acceptance_matches = bool(re.fullmatch(r"[a-f0-9]{64}", str(declared_task.get("acceptance_sha256", "")))
                                  and raw_test.get("acceptance_sha256") == declared_task["acceptance_sha256"])
        completed = bool(linked and acceptance_matches and complete_suite(raw_test, declared_task.get("expected_tests", 10)))
        rounds.append({"round": round_number, "candidate_node": node, "candidate_event_id": event["id"],
                       "candidate_origin": "live_developer" if round_number == 0 else "live_fixer",
                       "candidate_sha256": fingerprint, "source_sha256": digest(source.encode()),
                       "write_event_id": writes[0][1]["id"] if writes else None,
                       "test_event_id": tests[-1][1]["id"] if tests else None,
                       "acceptance_sha256": raw_test.get("acceptance_sha256"), "acceptance_matches_preregistration": acceptance_matches,
                       "evidence_linked": linked, "test_suite_completed": completed,
                       "test_status": raw_test.get("status", "not_observed"),
                       "test_passed": completed and raw_test.get("status") == "passed",
                       "natural_test_failure": completed and raw_test.get("status") == "failed",
                       "review_passed": gate.get("review", {}).get("passed"), "gate_passed": gate.get("passed")})
    first = next((r for r in rounds if r["round"] == 0), {})
    first_result = ("test_failed" if first.get("natural_test_failure") else
                    "passed" if first.get("test_passed") and first.get("gate_passed") is True else
                    "review_rejected" if first.get("test_passed") and first.get("review_passed") is False else
                    "unverified" if first else "not_reached")
    natural = any(r["natural_test_failure"] for r in rounds)
    review_only = any(r["test_passed"] and r["review_passed"] is False for r in rounds)
    corrected = bool(eligible and natural and outcome["repair_after_test_failure_observed"])
    success = bool(eligible and outcome["workflow_succeeded"]
                   and any(r["round"] == detail.get("attempt") and r["test_passed"] for r in rounds))
    return {"task_id": declared_task["id"], "run_id": detail.get("id"), "status": detail.get("status"),
            "mode": detail.get("mode"), "candidate_provenance": provenance,
            "task_and_provenance_match": bool(eligible), "first_round_result": first_result,
            "natural_test_failure_observed": natural, "review_only_failure_observed": review_only,
            "automatic_repair_observed": bool(eligible and outcome["repair_observed"]),
            "automatic_repair_after_natural_failure": corrected,
            "automatic_repair_after_natural_failure_succeeded": corrected and success,
            "workflow_succeeded": success,
            "rounds": rounds, "strict_outcome": outcome,
            "model_calls": detail.get("model_calls", 0), "model_tokens": detail.get("model_tokens", 0),
            "model_metrics_complete": detail.get("model_metrics_complete", False),
            "repair_rounds": detail.get("attempt", 0), "applied": detail.get("applied", False),
            "failure_reason": None if success else detail.get("summary", "No fully linked terminal result yet.")}


def export_trial(directory, trial, detail, task, secret, elapsed):
    folder = directory / "trials" / f"{trial['trial']:02d}-{trial['task_id']}-r{trial['repetition']}"
    folder.mkdir(parents=True, exist_ok=True)
    report = classify_run(detail, task)
    duration = None
    try:
        duration = max(0, (datetime.fromisoformat(detail["updated_at"]) - datetime.fromisoformat(detail["created_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        pass
    report.update(trial=trial["trial"], repetition=trial["repetition"], observation_elapsed_seconds=round(elapsed, 2),
                  run_duration_seconds=duration)
    evidence = {"schema": SCHEMA, "captured_at": now(), "source": "actual local API/SQLite; no candidate injection",
                "observation_incomplete": detail.get("status") not in TERMINAL, "report": report, "run": detail}
    write_json(folder / "evidence.json", evidence, secret)
    steps = {step["id"]: step for step in detail.get("steps", [])}
    artifacts = []
    for event in detail.get("events", []):
        if valid_live_candidate(event, steps):
            source = event["data"]["output"]["files"][0]["content"]
            name = event["node_id"].replace("/", "-") + f"-event-{event['id']}.py.txt"
            safe = redact(source, secret)
            (folder / name).write_text(safe, encoding="utf-8", newline="\n")
            artifacts.append({"path": name, "event_id": event["id"], "original_sha256": digest(source.encode()),
                              "redacted": safe != source})
    write_json(folder / "rounds.json", {"rounds": report["rounds"], "candidate_artifacts": artifacts,
                                       "validation_and_feedback": [e for e in detail.get("events", []) if e.get("type") in {"validation", "feedback"}]}, secret)
    trial.update(report=report, evidence_path=(folder / "evidence.json").relative_to(directory).as_posix())
    return report


def summarize(plan, state):
    reports = [trial["report"] for trial in state["trials"] if trial.get("report")]
    completed = sum(trial.get("state") == "complete" for trial in state["trials"])
    return {"schema": SCHEMA, "batch_id": plan["batch_id"], "updated_at": now(), "status": state["status"],
            "planned_trials": len(plan["trials"]), "completed_trials": completed,
            "unobserved_trials": len(plan["trials"]) - completed,
            "selection_policy": plan["selection_policy"], "synthetic_defects": False,
            "successes": sum(r["workflow_succeeded"] for r in reports),
            "first_pass_successes": sum(r["first_round_result"] == "passed" and r["workflow_succeeded"] for r in reports),
            "natural_failure_trials": sum(r["natural_test_failure_observed"] for r in reports),
            "natural_failure_auto_repaired_trials": sum(r["automatic_repair_after_natural_failure_succeeded"] for r in reports),
            "review_only_failure_trials": sum(r["review_only_failure_observed"] for r in reports),
            "model_calls_observed": sum(r["model_calls"] for r in reports),
            "model_tokens_known": sum(r["model_tokens"] for r in reports),
            "model_metrics_complete_for_observed_runs": bool(reports) and all(r["model_metrics_complete"] for r in reports),
            "not_a_comparative_benchmark": True, "trials": state["trials"]}


def run_batch(root, directory, plan, state, settings, request=api_request, clock=time.time, sleep=time.sleep):
    base, secret = plan["api_base"], settings.get("api_key", "")
    def save():
        state["updated_at"] = now()
        write_json(directory / "state.json", state, secret)
        write_json(directory / "summary.json", summarize(plan, state), secret)
    with batch_lock(directory):
        verify_frozen(root, directory, plan["source_manifest"], compare_current=False)
        for trial in state["trials"]:
            if trial.get("state") == "complete":
                continue
            if trial.get("state") == "submitting" and not trial.get("run_id"):
                state["status"] = "submission_uncertain"
                save()
                print("BLOCKED: an earlier POST response is uncertain; no duplicate trial was submitted.")
                return 6
            if not trial.get("run_id"):
                verify_frozen(root, directory, plan["source_manifest"])
                if safe_settings(settings) != plan["model_settings"]:
                    raise RuntimeError("Model settings changed after preregistration; start a separate batch.")
                agents, workflows = request(base, "/agents"), request(base, "/workflows")
                if agents != plan["agent_versions"] or next((w for w in workflows if w.get("id") == "development"), None) != plan["workflow"]:
                    raise RuntimeError("Agent/workflow configuration changed after preregistration.")
                used = sum(t.get("report", {}).get("model_calls", 0) for t in state["trials"])
                if used >= plan["max_model_calls"]:
                    state["status"] = "budget_exhausted_before_next_submission"
                    save()
                    return 5
                while state.get("last_submission_epoch") is not None:
                    wait = plan["interval_seconds"] - (clock() - state["last_submission_epoch"])
                    if wait <= 0:
                        break
                    sleep(min(wait, 5))
                health = request(base, "/health")
                if not health.get("model", {}).get("configured") or not health.get("sandbox", {}).get("available"):
                    state["status"] = "prerequisites_blocked"
                    save()
                    return 2
                if any(health.get("model", {}).get(key) != plan["model_settings"][key] for key in ("model", "base_url")):
                    raise RuntimeError("Backend model configuration differs from the frozen local settings; restart the intended backend before submitting.")
                task = plan["task_catalog"][trial["task_id"]]
                trial.update(state="submitting", submitted_at=now(), health_at_start=health)
                state.update(status="running", last_submission_epoch=clock())
                save()  # Durable intent precedes a possibly billable POST.
                try:
                    run = request(base, "/runs", {"requirement": task["requirement"], "workflow_id": "development",
                                                  "mode": "live", "task_id": trial["task_id"]})
                except Exception:
                    state["status"] = "submission_uncertain"
                    save()
                    raise
                if not re.fullmatch(r"[a-fA-F0-9]{32}", str(run.get("id", ""))):
                    raise RuntimeError("POST response omitted a valid run ID; submission remains uncertain.")
                trial.update(run_id=run["id"], state="observing")
                save()
                print(f"trial={trial['trial']} task={trial['task_id']} run={run['id']} submitted live", flush=True)
            started, marker = clock(), None
            while True:
                detail = request(base, "/runs/" + trial["run_id"])
                if detail.get("mode") != "live" or detail.get("task_id", "completion") != trial["task_id"]:
                    raise RuntimeError("Run identity/mode does not match its preregistered trial.")
                expected_task = plan["task_catalog"][trial["task_id"]]
                if detail.get("task", {}).get("snapshot_sha256") != expected_task.get("snapshot_sha256"):
                    raise RuntimeError("Run task snapshot differs from preregistration; preserve the run and investigate without resubmitting.")
                if detail.get("model_config") != plan["model_config"]:
                    raise RuntimeError("Run model parameters differ from preregistration; preserve the run without resubmitting.")
                elapsed = clock() - started + trial.get("observed_seconds", 0)
                report = export_trial(directory, trial, detail, plan["task_catalog"][trial["task_id"]], secret, elapsed)
                current = (detail.get("status"), detail.get("attempt"), detail.get("model_calls"))
                if current != marker:
                    print(f"trial={trial['trial']} status={current[0]} repair_round={current[1]} model_calls={current[2]}", flush=True)
                    marker = current
                if detail.get("status") in TERMINAL:
                    trial.update(state="complete", finished_at=now(), observed_seconds=elapsed)
                    save()
                    break
                if clock() - started >= plan["observation_timeout_seconds"]:
                    trial.update(state="observing", observed_seconds=elapsed)
                    state["status"] = "observation_timed_out"
                    save()
                    print("Observation timed out. The same backend run may continue; resume this batch, not a new submission.")
                    return 4
                save()
                sleep(1)
        state["status"] = "complete"
        save()
        return 0 if all(t.get("report", {}).get("workflow_succeeded") for t in state["trials"]) else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000/api")
    parser.add_argument("--tasks", nargs="+", choices=TASKS, default=list(TASKS))
    parser.add_argument("--repetitions", type=int, choices=(1, 2), default=1)
    parser.add_argument("--interval", type=int, default=60, help="Minimum seconds between new workflow starts (60 or more).")
    parser.add_argument("--max-model-calls", type=int, default=120, help="Stop new submissions when observed calls reach this count; an active run is allowed to finish.")
    parser.add_argument("--timeout", type=int, default=900, help="Observation timeout per run; does not cancel or repeat backend work.")
    parser.add_argument("--output", type=Path, help="New batch directory; must not already exist.")
    parser.add_argument("--resume", type=Path, help="Continue a preregistered batch without repeating finished/failed trials.")
    parser.add_argument("--dry-run", action="store_true", help="Freeze the plan using read-only local API calls; submit no model work.")
    args = parser.parse_args(argv)
    if not local_api_url(args.api_base):
        parser.error("Only a local loopback HTTP API is accepted.")
    if not 60 <= args.interval <= 3600 or not 1 <= args.timeout <= 3600 or not 1 <= args.max_model_calls <= 500:
        parser.error("interval must be 60..3600, timeout 1..3600, and max-model-calls 1..500")
    if len(set(args.tasks)) != len(args.tasks) or (args.resume and args.output):
        parser.error("Task IDs must be unique; --resume and --output are mutually exclusive.")
    settings = Runtime(ROOT, ROOT / ".agentforge").settings
    try:
        if args.resume:
            directory = args.resume.resolve()
            plan = json.loads((directory / "preregistration.json").read_text(encoding="utf-8"))
            state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
            if plan.get("schema") != SCHEMA or state.get("schema") != SCHEMA or state.get("preregistration_sha256") != digest((directory / "preregistration.json").read_bytes()):
                raise RuntimeError("The saved preregistration is invalid or changed.")
            if not local_api_url(plan.get("api_base", "")):
                raise RuntimeError("Saved batch must target a local loopback HTTP API.")
        else:
            directory = args.output.resolve() if args.output else ROOT / ".agentforge/evaluations" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])
            plan, state = preregister(ROOT, directory, args.api_base.rstrip("/"), args.tasks, args.repetitions,
                                     args.interval, args.max_model_calls, args.timeout, settings)
        print(f"Evaluation directory: {directory}", flush=True)
        if args.dry_run:
            write_json(directory / "summary.json", summarize(plan, state), settings.get("api_key", ""))
            print("Preregistered only; no workflow was submitted and no model was called.")
            return 0
        return run_batch(ROOT, directory, plan, state, settings)
    except (RuntimeError, KeyError, ValueError, OSError) as exc:
        print("ERROR: " + redact(str(exc), settings.get("api_key", "")))
        return 2


if __name__ == "__main__":
    sys.exit(main())
