"""Preregistered, paired three-arm live comparison; no host candidate execution.

Every trial is new and retained. Old evaluation results are not comparators.
No automatic resubmission or selective retry is performed by this runner.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from agentforge.comparison import BudgetRuntime, ComparisonEngine, SingleDirectEngine, arm_definition
from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.storage import Store
from agentforge.task_catalog import TASKS, public_task
from comparison_report import classify, summarize
from evaluate_tasks import canonical, digest, freeze_sources, now, safe_settings, verify_frozen, write_json
from evaluate_seeded_workflows import validate_control_evidence
from run_live_demo import redact
from comparison_continuation import load_parent, validate_conditions, verify_parent, claim_continuation

SCHEMA = "agentforge-paired-three-arm-v1"
ARMS = ("single_direct", "fixed_serial", "dynamic_parallel")


def trial_order(task_ids, repetitions=2):
    """Rotate arm positions and reverse the second repetition before results."""
    trials = []
    for repetition in range(1, repetitions + 1):
        for index, task_id in enumerate(task_ids):
            order = list(ARMS[index % 3:] + ARMS[:index % 3])
            if repetition % 2 == 0:
                order.reverse()
            for position, arm in enumerate(order, 1):
                trials.append({"trial": len(trials) + 1, "task_id": task_id, "arm": arm,
                               "repetition": repetition, "block": (repetition - 1) * len(task_ids) + index + 1,
                               "arm_position": position, "state": "pending", "run_id": None})
    return trials


async def detail_for(store, runtime, run_id):
    detail = store.run(run_id)
    detail.update(events=store.events(run_id), steps=store.steps(run_id),
                  snapshots=store.snapshots(run_id, include_context=True))
    try:
        detail["diff"] = await runtime.diff(run_id)
    except (OSError, ValueError, RuntimeErrorSafe):
        detail["diff"] = {"files": []}
    return detail


def export_trial(directory, trial, detail, task, ledger, secret):
    folder = directory / "trials" / f"{trial['trial']:02d}-{trial['task_id']}-r{trial['repetition']}-{trial['arm']}"
    folder.mkdir(parents=True, exist_ok=True)
    report = classify(detail, task, ledger)
    candidates = []
    for event in detail.get("events", []):
        output = event.get("data", {}).get("output") or {}
        if event.get("type") != "agent" or not isinstance(output, dict):
            continue
        for item in output.get("files", []) or []:
            if not isinstance(item, dict) or item.get("path") != "todo.py" or not isinstance(item.get("content"), str):
                continue
            source = item["content"]
            safe = redact(source, secret)
            name = f"candidate-event-{event['id']}.py.txt"
            (folder / name).write_bytes(safe.encode())
            candidates.append({"path": name, "event_id": event["id"], "node_id": event.get("node_id"),
                               "source_sha256": digest(source.encode()), "redacted": source != safe})
    evidence = {"schema": SCHEMA, "captured_at": now(), "trial": {k: v for k, v in trial.items() if k != "report"},
                "run": detail, "budget_ledger": ledger, "report": report, "candidate_artifacts": candidates}
    write_json(folder / "evidence.json", evidence, secret)
    write_json(folder / "classification.json", report, secret)
    trial.update(report=report, evidence_path=(folder / "evidence.json").relative_to(directory).as_posix(),
                 native_status=detail.get("status"))
    return report


async def run_batch(args, root=ROOT):
    parent_path = getattr(args, "continue_unsubmitted", None)
    parent = load_parent(root, parent_path) if parent_path else None
    directory = args.output.resolve() if args.output else root / ".agentforge/comparisons" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    if not directory.is_relative_to((root / ".agentforge/comparisons").resolve()):
        raise RuntimeErrorSafe("Output must be a new directory inside .agentforge/comparisons.")
    directory.mkdir(parents=True, exist_ok=False)
    discovery = Runtime(root, directory)
    secret = discovery.settings["api_key"]
    code, image_id, _ = await discovery._command(discovery._docker_command(
        "image", "inspect", discovery.settings["image"], "--format", "{{.Id}}"))
    image_id = image_id.strip()
    if code or not image_id.startswith("sha256:"):
        raise RuntimeErrorSafe("Docker image identity unavailable; no model requests sent.")
    runtime = BudgetRuntime(root, directory, max_requests=args.max_requests,
                            max_payload_bytes=180000, max_tokens=4096, image_id=image_id)
    health = await runtime.health()
    if not health.get("sandbox", {}).get("available") or not health.get("model", {}).get("configured"):
        raise RuntimeErrorSafe("Model configuration and Docker must be ready before preregistration.")
    control_reference, control_evidence = validate_control_evidence(args.controls.resolve(), runtime)
    if control_reference["docker_image_id"] != image_id:
        raise RuntimeErrorSafe("Control image differs from the pinned comparison image.")
    write_json(directory / "controls/evidence.json", control_evidence, secret)
    store = Store(directory / "agentforge.sqlite3")
    engines = {"single_direct": SingleDirectEngine(store, runtime),
               "fixed_serial": ComparisonEngine(store, runtime, arm="fixed_serial"),
               "dynamic_parallel": ComparisonEngine(store, runtime, arm="dynamic_parallel")}
    tasks = {t: runtime.snapshot_task(t) for t in args.tasks}
    definitions = {arm: arm_definition(arm, store.agents()) for arm in ARMS}
    trials = deepcopy(parent["trials"]) if parent else trial_order(args.tasks, args.repetitions)
    manifest = freeze_sources(root, directory, args.tasks, secret)
    plan = {"schema": SCHEMA, "created_at": now(), "batch_id": directory.name,
            "task_order": args.tasks, "task_catalog": {t: public_task(s) for t, s in tasks.items()},
            "arms": definitions, "trials": deepcopy(trials), "repetitions": args.repetitions,
            "model_config": runtime.model_config(), "model_settings": safe_settings(runtime.settings),
            "docker_image_id": image_id, "docker_image_original_tag": discovery.settings["image"],
            "limits": {"http_requests_per_trial": args.max_requests, "output_tokens_per_request": 4096,
                       "utf8_payload_bytes_per_request": 180000, "code_repairs_per_trial": 2,
                       "seconds_per_trial": args.timeout, "requests_per_agent_invocation": 4,
                       "format_retries_per_invocation": 1, "minimum_request_start_interval_seconds": 4.2},
            "budget_semantics": "Identical hard resource ceilings, not identical consumption. Every sent HTTP attempt including errors and format retries counts. Unused budget is not spent artificially. Total input/output tokens are measured, not fixed equal.",
            "ordering": "Tasks paired within repetition; cyclic arm position in repetition one and reverse in repetition two; no concurrent trials or selective resubmission.",
            "primary_endpoint": "Final unchanged live-generated candidate passes the same complete independent ten-test Docker suite; does not require a model review.",
            "secondary_endpoint": "Native strategy completion and multi-agent review gate recorded separately; single-direct has no independent model reviewer.",
            "candidate_origin": "live_model_development", "fault_injection": False,
            "controls": control_reference, "source_manifest": manifest,
            "source_manifest_sha256": digest(canonical(manifest)),
            "host": {"python": platform.python_version(), "platform": platform.platform()},
            "stopping_rules": "Every terminal failure remains. A trial timeout, unknown in-flight request, changed frozen source or environment stops new submissions; remaining trials retained as not_submitted. No run resume or restart in this batch."}
    if parent:
        plan["continuation"] = validate_conditions(parent, plan)
    write_json(directory / "preregistration.json", plan, secret)
    state = {"schema": SCHEMA, "batch_id": directory.name, "status": "preregistered", "trials": trials,
             "preregistration_sha256": digest((directory / "preregistration.json").read_bytes())}
    started = time.monotonic()

    def save():
        state.update(updated_at=now(), elapsed_seconds=round(time.monotonic() - started, 2))
        write_json(directory / "state.json", state, secret)
        summary = summarize(plan, trials)
        summary.update(status=state["status"], batch_id=directory.name, elapsed_seconds=state["elapsed_seconds"],
                       preregistration_sha256=state["preregistration_sha256"])
        write_json(directory / "summary.json", summary, secret)

    print(f"Comparison evidence: {directory}", flush=True)
    save()
    if args.dry_run:
        print("Frozen plan only; no live models or candidate execution. Use a new directory for execution.", flush=True)
        return 0
    try:
        if parent:
            state["continuation_claim"] = claim_continuation(root, parent, directory, state["preregistration_sha256"])
        state["status"] = "running"
        for trial in trials:
            if parent:
                verify_parent(parent)
            verify_frozen(root, directory, manifest)
            if digest((directory / "preregistration.json").read_bytes()) != state["preregistration_sha256"]:
                raise RuntimeErrorSafe("Preregistration changed; no further trial submitted.")
            if runtime.model_config() != plan["model_config"]:
                raise RuntimeErrorSafe("Model configuration changed during the batch.")
            task_id, arm = trial["task_id"], trial["arm"]
            engine = engines[arm]
            run = engine.configure_run(tasks[task_id]["requirement"], task_id=task_id, task_snapshot=tasks[task_id])
            if any(run[k] != definitions[arm][k] for k in ("workflow", "agent_versions")):
                raise RuntimeErrorSafe("Run definition differs from preregistered arm.")
            run_id = run["id"]
            trial.update(state="running", run_id=run_id, started_at=now())
            store.event(run_id, "experiment", "同期三组对照；真实开发，无缺陷注入，统一预算上限。",
                        data={"arm": arm, "trial": trial["trial"], "limits": plan["limits"],
                              "preregistration_sha256": state["preregistration_sha256"]})
            pending = None
            trial_start = time.monotonic()
            timed_out = False
            in_flight = False
            audit_failed = False
            observer_interrupted = False
            try:
                save()
                print(f"trial={trial['trial']} phase_slots={len(trials)} task={task_id} arm={arm} run={run_id}", flush=True)
                pending = asyncio.create_task(engine.execute(run_id))
                while not pending.done():
                    await asyncio.wait({pending}, timeout=5)
                    detail = await detail_for(store, runtime, run_id)
                    export_trial(directory, trial, detail, tasks[task_id], runtime.budget_record(run_id), secret)
                    save()
                    if not pending.done() and time.monotonic() - trial_start > args.timeout:
                        timed_out = True
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                        break
                if not timed_out:
                    await pending
            except (Exception, asyncio.CancelledError):
                observer_interrupted = True
                raise
            finally:
                if pending is None:
                    store.update_run(run_id, status="blocked", summary="Local observation failed before workflow execution; no request submitted.")
                elif not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                detail = await detail_for(store, runtime, run_id)
                ledger = runtime.budget_record(run_id)
                in_flight = any(r.get("sent") is True and r.get("status") == "pending"
                                for r in ledger.get("requests", []))
                audit_failed = ledger.get("audit_complete", True) is not True
                trial.update(state="interrupted" if timed_out or in_flight or audit_failed or observer_interrupted else "complete", finished_at=now(),
                             elapsed_seconds=round(time.monotonic() - trial_start, 3))
                report = export_trial(directory, trial, detail, tasks[task_id], ledger, secret)
                save()
            print(json.dumps({"trial": trial["trial"], "arm": arm, "task_id": task_id,
                              "status": detail["status"], "calls": detail.get("model_calls"),
                              "tokens": detail.get("model_tokens")}, ensure_ascii=False), flush=True)
            if timed_out:
                raise RuntimeErrorSafe("Trial wall-clock budget exhausted; remaining trials not submitted.")
            if in_flight:
                raise RuntimeErrorSafe("Model request remains in flight; remaining trials not submitted.")
            if audit_failed:
                raise RuntimeErrorSafe("Request evidence could not be fully persisted; remaining trials not submitted.")
            verify_frozen(root, directory, manifest)
        state["status"] = "completed"
        return 0
    except (Exception, asyncio.CancelledError) as exc:
        state.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "blocked",
                     error=redact(str(exc), secret))
        for trial in trials:
            if trial["state"] == "pending":
                trial.update(state="not_submitted", reason="Batch stop condition reached; all planned trials retained.")
        return 2
    finally:
        state["finished_at"] = now()
        state["task_inputs_unchanged"] = all(runtime.snapshot_task(t) == snapshot for t, snapshot in tasks.items())
        state["applied"] = any(r.get("applied") for r in store.runs())
        save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", nargs="+", choices=list(TASKS), default=list(TASKS))
    parser.add_argument("--repetitions", type=int, choices=(1, 2), default=2)
    parser.add_argument("--max-requests", type=int, default=24, choices=range(12, 53))
    parser.add_argument("--timeout", type=int, default=600, choices=range(60, 1801))
    parser.add_argument("--controls", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue-unsubmitted", type=Path,
                        help="New preregistered phase for only never-submitted slots of a stopped parent; never retries a run.")
    args = parser.parse_args(argv)
    if len(args.tasks) != len(set(args.tasks)):
        parser.error("Task IDs must be unique.")
    try:
        return asyncio.run(run_batch(args))
    except (OSError, ValueError, RuntimeErrorSafe) as exc:
        print("ERROR: " + type(exc).__name__ + ": comparison preparation failed; no batch certified.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
