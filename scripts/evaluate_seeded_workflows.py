"""Declared synthetic developer fixtures, real Engine/Docker/live repair.

This is a separate experiment, never a natural-model development evaluation.
Only the initial develop invocation is replaced. The normal planner, tester,
inspect/review and fixer invoke Runtime in live mode; Engine owns every gate,
feedback and its unchanged two-repair limit. No candidate is applied or run on
the host. Failed trials are retained without selective retries.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from agentforge.definitions import WORKFLOWS
from agentforge.engine import Engine, candidate_fingerprint
from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.storage import Store
from agentforge.task_catalog import TASKS, public_task
from evaluate_tasks import canonical, digest, freeze_sources, now, safe_settings, verify_frozen, write_json
from probe_task_suites import EXPECTED_FAILURES, assess_result
from run_live_demo import extract_outcome, redact

SCHEMA = "agentforge-declared-seeded-workflows-v1"
ORIGIN = "declared_synthetic_developer_fixture"
EXTENDED_TASKS = ("bulk_edit", "priority_query", "json_merge")
DESCRIPTIONS = {
    "bulk_edit": "Maintainer fixture omits duplicate batch id rejection.",
    "priority_query": "Maintainer fixture sorts priorities ascending instead of descending.",
    "json_merge": "Maintainer fixture does not advance next_id after successful import.",
}


def fixture_record(root, task_id):
    if task_id not in EXTENDED_TASKS:
        raise ValueError("Only the three declared extended-task fixtures are allowed.")
    relative = f"benchmarks/synthetic/{task_id}.py.txt"
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeErrorSafe("Fixture must be an ordinary file inside the project.")
    source = path.read_text(encoding="utf-8")
    return {"source": relative, "sha256": digest(source.encode()), "description": DESCRIPTIONS[task_id],
            "expected_failure_test_names": sorted(EXPECTED_FAILURES[task_id]),
            "natural_model_development_failure": False}, source


def submission_budget_reached(trials, elapsed_seconds, max_calls, max_seconds):
    """No retries and no cancellation of an already active standard workflow."""
    return (sum(t.get("report", {}).get("model_calls", 0) for t in trials) >= max_calls
            or elapsed_seconds >= max_seconds)


class SeededWorkflowRuntime(Runtime):
    """Intercept only develop; never synthesize tests, review, feedback or fixes."""

    def __init__(self, root, data_dir, fixtures):
        super().__init__(root, data_dir)
        self.fixtures = deepcopy(fixtures)
        self.settings["min_request_interval"] = max(4.2, self.settings.get("min_request_interval", 0.0))

    async def invoke(self, agent, task, context):
        if task.get("mode") != "live":
            raise RuntimeErrorSafe("Seeded workflow requires live mode for all model-backed nodes.")
        if task.get("kind") != "develop":
            return await super().invoke(agent, task, context)
        task_id = context.get("task_contract", {}).get("id")
        if task_id not in self.fixtures:
            raise RuntimeErrorSafe("No preregistered fixture for this task.")
        expected, source = self.fixtures[task_id]
        current, current_source = fixture_record(self.root, task_id)
        if current != expected or current_source != source:
            raise RuntimeErrorSafe("Declared fixture changed after preregistration.")
        return {"model_mode": "synthetic_fixture", "model_calls": 0, "tokens": 0,
                "metrics_complete": True, "duration_ms": 0, "tool_calls": [],
                "output": {"summary": "Declared maintainer synthetic fixture; not live developer output or a natural failure.",
                           "files": [{"path": "todo.py", "content": source}], "preset_fault": True,
                           "candidate_origin": ORIGIN, "synthetic_defect": deepcopy(expected)}}


def validate_control_evidence(path, runtime):
    """Recheck raw results and frozen bytes, not the old report's passed flag."""
    raw = path.read_bytes()
    evidence = json.loads(raw)
    if (evidence.get("kind") != "frozen_task_suites_synthetic_controls_real_docker"
            or evidence.get("passed") is not True or evidence.get("model_calls") != 0
            or evidence.get("baselines_unchanged") is not True or evidence.get("applied") is not False
            or evidence.get("runner_manifest") != runtime._acceptance_manifest()):
        raise RuntimeErrorSafe("Existing controls lack matching immutable Docker evidence.")
    records = {item["task_id"]: item for item in evidence.get("tasks", [])}
    samples = evidence.get("samples", [])
    pairs = {(item.get("task_id"), item.get("kind")) for item in samples}
    if len(records) != 4 or set(records) != set(TASKS) or len(samples) != 8 or pairs != {(t, k) for t in TASKS for k in ("reference", "synthetic")}:
        raise RuntimeErrorSafe("Exactly eight independent reference/synthetic controls are required.")
    for relative, sha in evidence["runner_manifest"].items():
        archived = path.parent / "frozen/runner" / relative
        if archived.is_symlink() or digest(archived.read_bytes()) != sha:
            raise RuntimeErrorSafe("Archived control runner differs from its recorded hash.")
    for task_id, record in records.items():
        snapshot = runtime.snapshot_task(task_id)
        if record.get("task_snapshot") != snapshot:
            raise RuntimeErrorSafe("Control task snapshot differs from the current task.")
        folder = path.parent / "frozen/tasks" / task_id
        for name, kind in (("baseline.py.txt", "baseline"), ("requirement.md", "requirement"), ("test_todo.py.txt", "acceptance")):
            if digest((folder / name).read_bytes()) != snapshot[kind + "_sha256"]:
                raise RuntimeErrorSafe("Archived control task bytes differ from the recorded hashes.")
        for kind in ("reference", "synthetic"):
            source_hash = record[kind + "_sha256"]
            if digest((folder / (kind + ".py.txt")).read_bytes()) != source_hash:
                raise RuntimeErrorSafe("Archived control candidate hash mismatch.")
            if task_id in EXTENDED_TASKS:
                current = (runtime.root / "benchmarks" / kind / f"{task_id}.py.txt").read_text(encoding="utf-8")
                if digest(current.encode()) != source_hash:
                    raise RuntimeErrorSafe("Current fixture/reference differs from the validated controls.")
            sample = next(s for s in samples if s["task_id"] == task_id and s["kind"] == kind)
            expected = EXPECTED_FAILURES[task_id] if kind == "synthetic" else ()
            result = sample.get("result", {})
            manifest = sample.get("manifest", {})
            if (not assess_result(result, expected)["expected_outcome_verified"]
                    or sample.get("source_sha256") != source_hash or sample.get("candidate_unchanged") is not True
                    or sample.get("contract_and_runner_unchanged") is not True
                    or result.get("acceptance_sha256") != snapshot["acceptance_sha256"]
                    or result.get("task_id") != task_id or manifest.get("task_snapshot_sha256") != snapshot["snapshot_sha256"]
                    or manifest.get("runner_manifest") != evidence["runner_manifest"]):
                raise RuntimeErrorSafe("A control lacks complete, matching raw Docker verification.")
    return {"mode": "reused_eight_verified_docker_controls", "source": str(path.resolve()),
            "source_sha256": digest(raw), "docker_image_id": evidence.get("docker_image_id"),
            "verified_samples": 8, "model_calls": 0}, evidence


def _live(event):
    data = event.get("data", {})
    return (event.get("type") == "agent" and data.get("model_mode") == "live"
            and type(data.get("model_calls")) is int and data["model_calls"] > 0
            and data.get("status") != "failed" and isinstance(data.get("output"), dict))


def _live_step(events, steps, node, output):
    return (steps.get(node, {}).get("status") == "succeeded"
            and any(e.get("node_id") == node and _live(e) and e["data"]["output"] == output for e in events))


def _round(detail, task, number, fixture):
    events = detail.get("events", [])
    steps = {s["id"]: s for s in detail.get("steps", [])}
    node = "develop" if number == 0 else f"round-{number}/fix"
    verify = f"round-{number}/verify"
    candidate = [(i, e) for i, e in enumerate(events) if e.get("type") == "agent" and e.get("node_id") == node
                 and e.get("data", {}).get("status") != "failed"]
    report = {"round": number, "candidate_node": node, "origin": ORIGIN if number == 0 else "live_fixer",
              "linked": False, "full_suite_completed": False, "test_passed": False, "test_failed": False,
              "live_review": False, "gate_passed": False}
    if len(candidate) != 1:
        return report
    position, event = candidate[0]
    data, output = event.get("data", {}), (event.get("data", {}).get("output") or {})
    files = output.get("files", [])
    if (not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict)
            or files[0].get("path") != "todo.py" or not isinstance(files[0].get("content"), str)):
        return report
    source = files[0]["content"]
    fingerprint = candidate_fingerprint({"files": [{"path": "todo.py", "after": source}]})
    scope = {"id": verify, "attempt": number, "candidate_hash": fingerprint}
    report.update(candidate_event_id=event["id"], candidate_sha256=fingerprint, source_sha256=digest(source.encode()))
    candidate_valid = (data.get("model_mode") == "synthetic_fixture" and type(data.get("model_calls")) is int
                       and data["model_calls"] == 0 and output.get("preset_fault") is True
                       and output.get("candidate_origin") == ORIGIN and output.get("synthetic_defect") == fixture
                       and report["source_sha256"] == fixture["sha256"]) if number == 0 else (_live(event) and output.get("preset_fault") is not True)
    if not candidate_valid or steps.get(node, {}).get("status") != "succeeded" or steps[node].get("output") != output:
        return report
    test_output = steps.get(verify + "/test", {}).get("output") or {}
    test = test_output.get("test") or {}
    raw_test = {k: v for k, v in test.items() if k != "evidence_scope"}
    review = steps.get(verify + "/review", {}).get("output") or {}
    raw_review = {k: v for k, v in review.items() if k != "evidence_scope"}
    writes = [(i, e) for i, e in enumerate(events) if i > position and e.get("node_id") == node
              and e.get("type") == "tool" and e.get("data", {}).get("tool") == "candidate_diff"
              and e["data"].get("source") == "runtime" and e["data"].get("status") == "completed"
              and candidate_fingerprint(e["data"].get("output") or {}) == fingerprint]
    tests = [(i, e) for i, e in enumerate(events) if e.get("node_id") == verify + "/test"
             and e.get("type") == "tool" and e.get("data", {}).get("tool") == "run_acceptance"
             and e["data"].get("source") == "runtime" and e["data"].get("output") == raw_test]
    reviews = [(i, e) for i, e in enumerate(events) if e.get("node_id") == verify + "/review"
               and _live(e) and e["data"].get("output") == raw_review]
    gates = [(i, e) for i, e in enumerate(events) if e.get("type") == "validation" and e.get("node_id") == f"round-{number}/gate"
             and e.get("data", {}).get("attempt") == number and type(e["data"].get("passed")) is bool
             and e["data"].get("test") == test and e["data"].get("review") == review]
    if not (writes and tests and reviews and len(gates) == 1):
        return report
    wi, we = writes[-1]
    ti, te = tests[-1]
    ri, revent = reviews[-1]
    gi, ge = gates[0]
    verify_output = steps.get(verify, {}).get("output") or {}
    inspect = steps.get(verify + "/inspect", {}).get("output") or {}
    inspected = (inspect.get("evidence_scope") == scope and verify_output.get("inspect") == inspect
                 and _live_step(events, steps, verify + "/inspect", {k: v for k, v in inspect.items() if k != "evidence_scope"}))
    linked = (position < wi < ti < ri < gi and test.get("evidence_scope") == scope and review.get("evidence_scope") == scope
              and all(steps.get(s, {}).get("status") == "succeeded" for s in (verify, verify + "/test", verify + "/review"))
              and inspected and verify_output.get("validation_scope") == scope and verify_output.get("test") == test and verify_output.get("review") == review
              and not any(e.get("type") == "validation" and e.get("node_id") == verify for e in events[position + 1:]))
    # The tester is also live; its commentary never substitutes for Runtime.test.
    tester = any(ti < i < gi and e.get("node_id") == verify + "/test" and _live(e)
                 and e["data"]["output"] == test_output.get("assessment") for i, e in enumerate(events))
    expected = fixture["expected_failure_test_names"] if number == 0 else ()
    assessment = assess_result(raw_test, expected)
    completed = (linked and tester and assessment["full_suite_completed"] and raw_test.get("task_id") == task["id"]
                 and raw_test.get("acceptance_sha256") == task["acceptance_sha256"]
                 and ((raw_test.get("status") == "passed" and raw_test.get("exit_code") == 0 and assessment["attestation"]["passed"] is True)
                      or (raw_test.get("status") == "failed" and raw_test.get("exit_code") == 1 and assessment["attestation"]["passed"] is False)))
    report.update(linked=bool(linked and tester), full_suite_completed=bool(completed),
                  test_passed=bool(completed and raw_test.get("status") == "passed"),
                  test_failed=bool(completed and raw_test.get("status") == "failed"),
                  declared_defect_observed=bool(completed and number == 0 and assessment["expected_outcome_verified"]),
                  live_review=bool(linked), review_passed=review.get("passed") is True,
                  gate_passed=ge["data"]["passed"], write_event_id=we["id"], test_event_id=te["id"],
                  review_event_id=revent["id"], gate_event_id=ge["id"], raw_test=raw_test, scoped_test=test,
                  acceptance_sha256=raw_test.get("acceptance_sha256"), assessment=assessment)
    return report


def classify_seeded_run(detail, task, fixture):
    events = detail.get("events", [])
    steps = {s["id"]: s for s in detail.get("steps", [])}
    ids = [e.get("id") for e in events]
    ordered = all(type(i) is int for i in ids) and all(a < b for a, b in zip(ids, ids[1:]))
    declarations = [e for e in events if e.get("type") == "provenance" and e.get("data", {}).get("candidate_origin") == ORIGIN
                    and e["data"].get("fault_injection") is True and e["data"].get("synthetic_defect") == fixture]
    agent_ids = [e["id"] for e in events if e.get("type") == "agent" and type(e.get("id")) is int]
    eligible = bool(ordered and len(declarations) == 1 and agent_ids and declarations[0]["id"] < min(agent_ids)
                    and detail.get("candidate_origin") == ORIGIN and detail.get("fault_injection") is True
                    and detail.get("natural_model_development_failure") is False
                    and detail.get("synthetic_defect") == fixture and detail.get("mode") == "live"
                    and detail.get("workflow_id") == "development" and detail.get("workflow") == WORKFLOWS["development"]
                    and detail.get("task_id") == task["id"] and detail.get("task", {}).get("snapshot_sha256") == task["snapshot_sha256"]
                    and detail.get("task_snapshot") == task and detail.get("requirement") == task["requirement"] and detail.get("applied") is False
                    and _live_step(events, steps, "plan", steps.get("plan", {}).get("output")))
    rounds = [_round(detail, task, number, fixture) for number in range(3)
              if any(e.get("type") == "agent" and e.get("node_id") == ("develop" if number == 0 else f"round-{number}/fix") for e in events)]
    chain = []
    for previous, current in zip(rounds, rounds[1:]):
        feedback = [e for e in events if e.get("type") == "feedback"
                    and e.get("node_id") == f"round-{previous['round']}/gate"
                    and e.get("data", {}).get("next_attempt") == current["round"]
                    and e["data"].get("test") == previous.get("scoped_test")
                    and previous.get("gate_event_id", 0) < e["id"] < current.get("candidate_event_id", 0)]
        valid = (previous["linked"] and current["linked"] and previous["full_suite_completed"]
                 and previous["gate_passed"] is False and current["round"] == previous["round"] + 1
                 and previous.get("candidate_sha256") != current.get("candidate_sha256") and len(feedback) == 1)
        chain.append({"from_round": previous["round"], "to_round": current["round"], "verified": bool(valid),
                      "feedback_event_id": feedback[0]["id"] if feedback else None})
    # A running or failed Engine step has output=None. Normalize only the view
    # passed to the shared extractor; preserve the original nulls in run.json.
    strict_detail = {**detail, "steps": [{**step, "output": step.get("output") or {}} for step in detail.get("steps", [])],
                     "diff": detail.get("diff") or {}}
    strict = extract_outcome(strict_detail)
    first = rounds[0] if rounds else {}
    final = rounds[-1] if rounds else {}
    all_live = all(_live(e) for e in events if e.get("type") == "agent" and e.get("node_id") != "develop" and e.get("data", {}).get("status") != "failed")
    repaired = bool(eligible and all_live and first.get("round") == 0 and first.get("declared_defect_observed")
                    and chain and all(link["verified"] for link in chain) and final.get("round") == detail.get("attempt")
                    and final.get("test_passed") and final.get("review_passed") and final.get("gate_passed") is True
                    and strict["workflow_succeeded"] and strict["repair_after_test_failure_observed"])
    return {"schema": SCHEMA, "run_id": detail.get("id"), "task_id": task["id"], "status": detail.get("status"),
            "candidate_origin": ORIGIN, "natural_model_development_failure": False, "fault_injection": True,
            "provenance_verified": eligible, "declared_initial_failure_observed": bool(eligible and first.get("declared_defect_observed")),
            "automatic_workflow_repair": repaired, "workflow_succeeded": bool(eligible and strict["workflow_succeeded"]),
            "rounds": rounds, "repair_chain": chain, "strict_outcome": strict,
            "model_calls": detail.get("model_calls", 0), "model_tokens": detail.get("model_tokens", 0),
            "model_metrics_complete": detail.get("model_metrics_complete", False), "applied": detail.get("applied", False),
            "note": "Synthetic initial candidate; live repair evidence only. Never count as a natural developer failure."}


async def collect_detail(store, runtime, run_id):
    detail = store.run(run_id)
    detail.update(events=store.events(run_id), steps=store.steps(run_id), snapshots=store.snapshots(run_id, include_context=True))
    try:
        detail["diff"] = await runtime.diff(run_id)
    except (OSError, ValueError, RuntimeErrorSafe):
        detail["diff"] = {"files": []}
    return detail


def export_trial(directory, trial, detail, task, fixture, secret):
    folder = directory / "trials" / f"{trial['trial']:02d}-{trial['task_id']}-r{trial['repetition']}"
    report = classify_seeded_run(detail, task, fixture)
    artifacts = []
    for event in detail["events"]:
        node = event.get("node_id") or ""
        if event.get("type") != "agent" or (node != "develop" and not re.fullmatch(r"round-[12]/fix", node)):
            continue
        for file in (event.get("data", {}).get("output") or {}).get("files", []) or []:
            if not isinstance(file, dict) or file.get("path") != "todo.py" or not isinstance(file.get("content"), str):
                continue
            source = file["content"]
            safe = redact(source, secret)
            path = folder / "candidates" / f"{node.replace('/', '-')}-event-{event['id']}.py.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(safe.encode())
            artifacts.append({"node_id": node, "event_id": event["id"], "path": str(path.relative_to(directory)),
                              "source_sha256": digest(source.encode()), "artifact_sha256": digest(safe.encode()),
                              "redacted": safe != source, "model_mode": event["data"].get("model_mode")})
    write_json(folder / "run.json", {"schema": SCHEMA, "captured_at": now(), "run": detail,
                                   "outcome": report, "candidate_artifacts": artifacts}, secret)
    write_json(folder / "classification.json", report, secret)
    trial.update(status=detail["status"], report=report, evidence=str((folder / "run.json").relative_to(directory)))


async def reference_checks(runtime, directory, tasks, controls_path, secret):
    code, image_id, _ = await runtime._command(runtime._docker_command("image", "inspect", runtime.settings["image"], "--format", "{{.Id}}"))
    if code or not image_id.strip():
        raise RuntimeErrorSafe("Cannot verify Docker image identity before model work.")
    if controls_path:
        summary, evidence = validate_control_evidence(controls_path, runtime)
        if summary["docker_image_id"] != image_id.strip():
            raise RuntimeErrorSafe("Existing controls used another Docker image; rerun references explicitly.")
        write_json(directory / "controls/reused-evidence.json", evidence, secret)
        return summary
    evidence = {"mode": "fresh_reference_controls", "model_calls": 0, "docker_image_id": image_id.strip(), "samples": [], "passed": False}
    for task_id, task in tasks.items():
        source = (runtime.root / "benchmarks/reference" / f"{task_id}.py.txt").read_text(encoding="utf-8")
        run_id = f"seed-reference-{task_id}-{uuid4().hex[:12]}"
        manifest = await runtime.prepare(run_id, task)
        await runtime.apply_candidate(run_id, [{"path": "todo.py", "content": source}])
        result = await runtime.test(run_id)
        unchanged = (await runtime.read_project(run_id))["files"][0]["content"] == source
        sample = {"task_id": task_id, "run_id": run_id, "manifest": manifest, "source_sha256": digest(source.encode()),
                  "result": result, "assessment": assess_result(result), "candidate_unchanged": unchanged}
        evidence["samples"].append(sample)
        write_json(directory / "controls/fresh-evidence.json", evidence, secret)
        if not sample["assessment"]["expected_outcome_verified"] or not unchanged or result.get("acceptance_sha256") != task["acceptance_sha256"]:
            raise RuntimeErrorSafe("Positive reference control did not pass; no model workflow submitted.")
    evidence["passed"] = True
    write_json(directory / "controls/fresh-evidence.json", evidence, secret)
    return {"mode": evidence["mode"], "source": "controls/fresh-evidence.json",
            "source_sha256": digest((directory / "controls/fresh-evidence.json").read_bytes()),
            "docker_image_id": image_id.strip(), "verified_samples": len(tasks), "model_calls": 0}


async def run_batch(args, root=ROOT):
    directory = args.output.resolve() if args.output else root / ".agentforge/seeded-workflows" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    if not directory.resolve().is_relative_to((root / ".agentforge/seeded-workflows").resolve()):
        raise RuntimeErrorSafe("Output must be a new directory under .agentforge/seeded-workflows.")
    directory.mkdir(parents=True, exist_ok=False)
    fixtures = {task_id: fixture_record(root, task_id) for task_id in args.tasks}
    runtime = SeededWorkflowRuntime(root, directory, fixtures)
    secret = runtime.settings.get("api_key", "")
    store = Store(directory / "agentforge.sqlite3")
    engine = Engine(store, runtime)
    tasks = {task_id: runtime.snapshot_task(task_id) for task_id in args.tasks}
    manifest = freeze_sources(root, directory, args.tasks, secret)
    for task_id in args.tasks:
        for kind in ("reference", "synthetic"):
            relative = f"benchmarks/{kind}/{task_id}.py.txt"
            content = (root / relative).read_bytes()
            if secret and secret.encode() in content:
                raise RuntimeErrorSafe("Credential found in fixture; refusing to archive.")
            target = directory / "frozen" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            manifest[relative] = {"sha256": digest(content), "bytes": len(content)}
    trials = [{"trial": i + 1, "task_id": task, "repetition": repeat, "status": "pending", "run_id": None}
              for i, (repeat, task) in enumerate((r, t) for r in range(1, args.repetitions + 1) for t in args.tasks)]
    plan = {"schema": SCHEMA, "created_at": now(), "candidate_origin": ORIGIN, "fault_injection": True,
            "natural_model_development_failure": False, "task_order": args.tasks, "repetitions": args.repetitions,
            "task_catalog": {t: public_task(s) for t, s in tasks.items()}, "fixtures": {t: f[0] for t, f in fixtures.items()},
            "agent_versions": store.agents(), "workflow": WORKFLOWS["development"], "max_repairs": 2,
            "model_settings": safe_settings(runtime.settings), "model_config": runtime.model_config(),
            "max_model_calls": args.max_model_calls, "max_seconds": args.max_seconds,
            "budget_semantics": "Checked before new trials; active workflow completes with the standard two-repair limit.",
            "selection_policy": "Every planned trial is retained. No selective retry or resume. No target apply.",
            "controls_source": str(args.controls.resolve()) if args.controls else "fresh_reference_controls",
            "source_manifest": manifest, "source_manifest_sha256": digest(canonical(manifest)), "trials": deepcopy(trials)}
    write_json(directory / "preregistration.json", plan, secret)
    state = {"schema": SCHEMA, "status": "preregistered", "natural_model_development_failure": False,
             "preregistration_sha256": digest((directory / "preregistration.json").read_bytes()), "trials": trials}
    started = time.monotonic()

    def save():
        state.update(updated_at=now(), elapsed_seconds=round(time.monotonic() - started, 2),
                     model_calls=sum(t.get("report", {}).get("model_calls", 0) for t in trials),
                     model_tokens=sum(t.get("report", {}).get("model_tokens", 0) for t in trials),
                     model_metrics_complete=all(t.get("report", {}).get("model_metrics_complete") is True for t in trials if t.get("run_id")),
                     automatic_workflow_repairs=sum(t.get("report", {}).get("automatic_workflow_repair", False) for t in trials))
        write_json(directory / "summary.json", state, secret)

    print(f"Seeded workflow evidence: {directory}", flush=True)
    save()
    if args.dry_run:
        print("Preregistered only; no model requests or Docker candidate tests.", flush=True)
        return 0
    try:
        state["health_at_start"] = await runtime.health()
        if not state["health_at_start"].get("sandbox", {}).get("available") or not state["health_at_start"].get("model", {}).get("configured"):
            raise RuntimeErrorSafe("Configured model and verified Docker prerequisites required before submission.")
        state["reference_controls"] = await reference_checks(runtime, directory, tasks, args.controls, secret)
        state["status"] = "running"
        save()
        for trial in trials:
            if submission_budget_reached(trials, time.monotonic() - started, args.max_model_calls, args.max_seconds):
                trial.update(status="not_submitted_budget", reason="Preregistered submission guard reached; no retry.")
                save()
                continue
            verify_frozen(root, directory, manifest)
            task_id = trial["task_id"]
            run = engine.create_run(tasks[task_id]["requirement"], "development", "live", task_id)
            run_id = run["id"]
            store.update_run(run_id, candidate_origin=ORIGIN, fault_injection=True, synthetic_defect=fixtures[task_id][0],
                             natural_model_development_failure=False)
            store.event(run_id, "provenance", "明示合成初始候选：非真实开发输出，非自然模型失败；后续测试与修正走标准真实工作流。",
                        data={"candidate_origin": ORIGIN, "fault_injection": True, "synthetic_defect": fixtures[task_id][0],
                              "natural_model_development_failure": False, "reference_controls": state["reference_controls"]})
            trial.update(status="queued", run_id=run_id, started_at=now())
            save()
            pending = asyncio.create_task(engine.execute(run_id))
            try:
                while not pending.done():
                    await asyncio.wait({pending}, timeout=5)
                    detail = await collect_detail(store, runtime, run_id)
                    export_trial(directory, trial, detail, tasks[task_id], fixtures[task_id][0], secret)
                    save()
                await pending
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                detail = await collect_detail(store, runtime, run_id)
                export_trial(directory, trial, detail, tasks[task_id], fixtures[task_id][0], secret)
                trial["finished_at"] = now()
                save()
            verify_frozen(root, directory, manifest)
            print(json.dumps({"task_id": task_id, "run_id": run_id, "status": trial["status"],
                              "automatic_workflow_repair": trial["report"]["automatic_workflow_repair"],
                              "natural_model_development_failure": False, "model_calls": trial["report"]["model_calls"]}), flush=True)
        state["status"] = "completed"
        return 0 if all(t.get("report", {}).get("automatic_workflow_repair") for t in trials) else 3
    except (Exception, asyncio.CancelledError) as exc:
        state.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "blocked",
                     error=redact(str(exc), secret))
        for trial in trials:
            if trial["status"] == "pending":
                trial.update(status="not_submitted_blocked", reason="Batch stopped; prior failures remain recorded.")
        return 2
    finally:
        state["targets_unchanged"] = all(runtime.snapshot_task(t) == snapshot for t, snapshot in tasks.items())
        state["applied"] = any(run.get("applied") is True for run in store.runs())
        state["finished_at"] = now()
        save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", nargs="+", choices=EXTENDED_TASKS, default=list(EXTENDED_TASKS))
    parser.add_argument("--repetitions", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--max-model-calls", type=int, default=100)
    parser.add_argument("--max-seconds", type=int, default=3600)
    parser.add_argument("--controls", type=Path, help="Reuse exactly eight matching completed Docker controls; otherwise test fresh positive references first.")
    parser.add_argument("--output", type=Path, help="New directory under .agentforge/seeded-workflows only.")
    parser.add_argument("--dry-run", action="store_true", help="Freeze inputs only; do not call a model or test candidate code.")
    args = parser.parse_args(argv)
    if len(set(args.tasks)) != len(args.tasks) or not 1 <= args.max_model_calls <= 300 or not 1 <= args.max_seconds <= 14400:
        parser.error("Unique tasks, max-model-calls 1..300 and max-seconds 1..14400 required.")
    try:
        return asyncio.run(run_batch(args))
    except (OSError, ValueError, RuntimeErrorSafe) as exc:
        print("ERROR: " + type(exc).__name__ + ": input or archive preparation failed; inspect the preserved batch.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
