"""Freeze four independent task suites and run eight declared Docker controls.

No LLM requests, workflow database writes, target application, or host execution
of candidate sources. Positive controls must pass before their negative control.
The sources, contracts and expected negative failures are frozen before testing.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.runtime import MOCK_SOURCE, Runtime, RuntimeErrorSafe
from agentforge.task_catalog import TASKS


EXPECTED_FAILURES = {
    "completion": {"test_filter_completed", "test_filter_incomplete"},
    "bulk_edit": {"test_batch_validation_is_atomic"},
    "priority_query": {"test_sort_uses_descending_priority_then_ascending_id",
                       "test_combined_completion_and_priority_filters", "test_filter_sort_then_paginate"},
    "json_merge": {"test_import_preserves_sparse_ids_and_advances_allocator",
                   "test_replace_keeps_existing_positions_and_appends_in_input_order",
                   "test_skip_preserves_existing_values_but_validates_skipped_rows",
                   "test_roundtrip_copies_and_monotonic_next_id", "test_empty_import_and_independent_instances"},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha_bytes(content):
    return hashlib.sha256(content).hexdigest()


def sha_text(content):
    return sha_bytes(content.encode("utf-8"))


class ControlsOnlyRuntime(Runtime):
    def _post(self, payload):
        raise AssertionError("Control validation must never request a model")

    async def invoke(self, *args, **kwargs):
        raise AssertionError("Control validation must never invoke an Agent")


def assess_result(result, expected_failures=()):
    """A complete suite with named assertion failures is distinct from failure to run."""
    expected = set(expected_failures)
    stdout, stderr = result.get("stdout", ""), result.get("stderr", "")
    matches = re.findall(r"^AGENTFORGE_ACCEPTANCE=(\{[^\n]+\})$", stdout, re.MULTILINE)
    try:
        attestation = json.loads(matches[0]) if len(matches) == 1 else None
    except (ValueError, TypeError):
        attestation = None
    failed_names = sorted(set(re.findall(r"^FAIL: (test_\w+)\b", stderr, re.MULTILINE)))
    error_names = sorted(set(re.findall(r"^ERROR: (test_\w+)\b", stderr, re.MULTILINE)))
    completed = (result.get("isolated") is True and type(result.get("exit_code")) is int
                 and bool(re.search(r"^Ran 10 tests? in ", stderr, re.MULTILINE))
                 and isinstance(attestation, dict) and type(attestation.get("tests")) is int
                 and attestation["tests"] == 10 and type(attestation.get("passed")) is bool)
    if expected:
        matched = (completed and result.get("status") == "failed" and result["exit_code"] == 1
                   and attestation["passed"] is False and set(failed_names) == expected and not error_names)
    else:
        matched = (completed and result.get("status") == "passed" and result["exit_code"] == 0
                   and attestation["passed"] is True and not failed_names and not error_names)
    return {"expected_outcome_verified": bool(matched), "full_suite_completed": bool(completed),
            "failure_test_names": failed_names, "error_test_names": error_names,
            "expected_failure_test_names": sorted(expected), "attestation": attestation}


def frozen_sources(task_id):
    if task_id == "completion":
        positive = MOCK_SOURCE.replace("{filter}", " if completed is None or task['completed'] == completed")
        negative = MOCK_SOURCE.replace("{filter}", "")
        return {"reference": positive, "synthetic": negative,
                "reference_origin": "Runtime.MOCK_SOURCE with completion filter (maintainer positive control)",
                "synthetic_origin": "Runtime.MOCK_SOURCE missing filter (declared synthetic negative control)"}
    return {"reference": (ROOT / "benchmarks/reference" / f"{task_id}.py.txt").read_text(encoding="utf-8"),
            "synthetic": (ROOT / "benchmarks/synthetic" / f"{task_id}.py.txt").read_text(encoding="utf-8"),
            "reference_origin": f"benchmarks/reference/{task_id}.py.txt",
            "synthetic_origin": f"benchmarks/synthetic/{task_id}.py.txt"}


async def main():
    started = time.monotonic()
    directory = ROOT / ".agentforge/task-controls" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])
    directory.mkdir(parents=True)
    runtime = ControlsOnlyRuntime(ROOT, directory)
    evidence = {"kind": "frozen_task_suites_synthetic_controls_real_docker", "started_at": now(),
                "model_calls": 0, "model_mode": "none", "natural_model_failure": False, "applied": False,
                "passed": False, "tasks": [], "samples": []}
    output = directory / "evidence.json"

    def save():
        evidence["updated_at"] = now()
        evidence["duration_seconds"] = round(time.monotonic() - started, 2)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    frozen = {}
    try:
        evidence["health_at_start"] = await runtime.health()
        if not evidence["health_at_start"].get("sandbox", {}).get("available"):
            evidence["blocked"] = "Docker unavailable; no candidate executed"
            return 2
        image_code, image_id, _ = await runtime._command(runtime._docker_command("image", "inspect", runtime.settings["image"], "--format", "{{.Id}}"))
        if image_code:
            raise RuntimeErrorSafe("Cannot record Docker image identity")
        evidence["docker_image_id"] = image_id.strip()
        runner_manifest = runtime._acceptance_manifest()
        evidence["runner_manifest"] = runner_manifest
        for relative, expected_hash in runner_manifest.items():
            content = (ROOT / "acceptance" / relative).read_bytes()
            if sha_bytes(content) != expected_hash:
                raise RuntimeErrorSafe("Runner changed while freezing control inputs")
            destination = directory / "frozen/runner" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        # Freeze every suite/control before running the first reference.
        for task_id in TASKS:
            snapshot, sources = runtime.snapshot_task(task_id), frozen_sources(task_id)
            target = directory / "frozen/tasks" / task_id
            target.mkdir(parents=True)
            for name, content in (("baseline.py.txt", snapshot["baseline"]), ("requirement.md", snapshot["requirement"]),
                                  ("test_todo.py.txt", snapshot["acceptance"]), ("reference.py.txt", sources["reference"]),
                                  ("synthetic.py.txt", sources["synthetic"])):
                (target / name).write_text(content, encoding="utf-8", newline="\n")
            record = {"task_id": task_id, "task_snapshot": snapshot,
                      "reference_sha256": sha_text(sources["reference"]), "synthetic_sha256": sha_text(sources["synthetic"]),
                      "reference_origin": sources["reference_origin"], "synthetic_origin": sources["synthetic_origin"],
                      "expected_synthetic_failure_tests": sorted(EXPECTED_FAILURES[task_id])}
            evidence["tasks"].append(record)
            frozen[task_id] = (snapshot, sources)
        evidence["frozen_at"] = now()
        save()
        for task_id, (snapshot, sources) in frozen.items():
            for kind in ("reference", "synthetic"):
                if runtime._acceptance_manifest() != runner_manifest or runtime.snapshot_task(task_id) != snapshot:
                    raise RuntimeErrorSafe("Inputs changed after freeze; restart controls after investigation")
                run_id = f"control-{task_id}-{kind}-{uuid4().hex[:12]}"
                sample = {"task_id": task_id, "kind": kind, "run_id": run_id, "source_sha256": sha_text(sources[kind]),
                          "provenance": sources[kind + "_origin"], "started_at": now()}
                evidence["samples"].append(sample)
                sample["manifest"] = await runtime.prepare(run_id, snapshot)
                await runtime.apply_candidate(run_id, [{"path": "todo.py", "content": sources[kind]}])
                save()
                sample["result"] = await runtime.test(run_id)
                sample["finished_at"] = now()
                sample["assessment"] = assess_result(sample["result"], EXPECTED_FAILURES[task_id] if kind == "synthetic" else ())
                candidate = (await runtime.read_project(run_id))["files"][0]["content"]
                sample["candidate_unchanged"] = sha_text(candidate) == sample["source_sha256"]
                sample["contract_and_runner_unchanged"] = runtime.snapshot_task(task_id) == snapshot and runtime._acceptance_manifest() == runner_manifest
                save()
                print(json.dumps({"task_id": task_id, "kind": kind, **sample["assessment"],
                                  "duration_ms": sample["result"].get("duration_ms")}, ensure_ascii=False), flush=True)
                if not sample["assessment"]["expected_outcome_verified"] or not sample["candidate_unchanged"] or not sample["contract_and_runner_unchanged"]:
                    evidence["blocked"] = f"Unexpected {task_id}/{kind} control outcome; inspect raw result before any live task"
                    return 2
        evidence["baselines_unchanged"] = all(runtime.snapshot_task(task_id) == snapshot for task_id, (snapshot, _) in frozen.items())
        code, remaining, _ = await runtime._command(runtime._docker_command("ps", "--filter", "name=agentforge-", "--format", "{{.Names}}"))
        evidence["cleanup_observation"] = {"command_exit_code": code, "running_agentforge_containers": remaining.splitlines()}
        evidence["passed"] = evidence["baselines_unchanged"] and len(evidence["samples"]) == 8 and code == 0 and not remaining.strip()
        return 0 if evidence["passed"] else 2
    except Exception as exc:
        evidence["error"] = str(exc) if isinstance(exc, RuntimeErrorSafe) else type(exc).__name__
        return 2
    finally:
        save()
        print(json.dumps({"passed": evidence["passed"], "samples": len(evidence["samples"]), "model_calls": 0,
                          "evidence": str(output), "duration_seconds": evidence["duration_seconds"],
                          "blocked": evidence.get("blocked"), "error": evidence.get("error")}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
