"""Unit evidence fixtures only: no model, Docker, or candidate execution.

Engine exercises its real scheduling/gates against explicit test doubles. Fake
Docker text in this file is never product evidence and stays in temporary dirs.
"""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("seeded_workflows", ROOT / "scripts/evaluate_seeded_workflows.py")
seeded = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seeded)


def fake_test(task, failed_names=()):
    """Synthetic test text for classifier unit tests; does not execute sources."""
    failed_names = list(failed_names)
    passed = not failed_names
    return {"status": "passed" if passed else "failed", "exit_code": 0 if passed else 1, "isolated": True,
            "stdout": 'AGENTFORGE_ACCEPTANCE=' + json.dumps({"tests": 10, "passed": passed}) + "\n",
            "stderr": "\n".join("FAIL: " + name + " (unit.evidence.Fixture)" for name in failed_names) + "\nRan 10 tests in 0.1s\n",
            "acceptance_sha256": task["acceptance_sha256"], "task_id": task["id"], "duration_ms": 1, "expected_tests": 10}


def fake_settings(_):
    return {"model": "unit-test-model", "base_url": "https://example.invalid/v1", "api_key": "unit-secret",
            "timeout": 1, "min_request_interval": 0, "image": "unit-image", "docker_backend": "native"}


class SeededWorkflowEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture, self.source = seeded.fixture_record(ROOT, "bulk_edit")
        with patch("agentforge.runtime._settings", side_effect=fake_settings):
            self.runtime = seeded.SeededWorkflowRuntime(ROOT, Path(self.temp.name), {"bulk_edit": (self.fixture, self.source)})
        self.task = self.runtime.snapshot_task("bulk_edit")

    async def make_detail(self, fail_twice=False, never_repair=False, fail_kind=None, pending_kind=None):
        store = seeded.Store(Path(self.temp.name) / ("test-" + str(len(list(Path(self.temp.name).glob("*.sqlite3")))) + ".sqlite3"))
        engine = seeded.Engine(store, self.runtime)
        invocations = []
        tests = 0
        fixes = 0
        pending_reached = asyncio.Event()
        pending_release = asyncio.Event()
        original_invoke = self.runtime.invoke

        async def invoke_dispatch(agent, task, context):
            if task["kind"] == fail_kind:
                raise seeded.RuntimeErrorSafe("Explicit unit-test invocation failure")
            if task["kind"] == pending_kind:
                pending_reached.set()
                await pending_release.wait()
            return await original_invoke(agent, task, context)

        async def invoke(_runtime, agent, task, context):
            nonlocal fixes
            invocations.append(task["kind"])
            self.assertEqual(task["mode"], "live")
            self.assertNotEqual(task["kind"], "develop", "Initial fixture must bypass the model")
            output = {"summary": "Explicit unit-test model double"}
            if task["kind"] == "fix":
                fixes += 1
                output["files"] = [{"path": "todo.py", "content": f"# unit fixture corrected candidate {fixes}\n"}]
            if task["kind"] in ("inspect", "review"):
                output.update(passed=True, issues=[])
            return {"output": output, "model_mode": "live", "model_calls": 1, "tokens": 2, "metrics_complete": True}

        async def test(_run_id):
            nonlocal tests
            tests += 1
            failed = never_repair or tests == 1 or (fail_twice and tests == 2)
            return fake_test(self.task, self.fixture["expected_failure_test_names"] if failed else ())

        run = engine.create_run(self.task["requirement"], "development", "live", "bulk_edit")
        store.update_run(run["id"], candidate_origin=seeded.ORIGIN, fault_injection=True,
                         synthetic_defect=self.fixture, natural_model_development_failure=False)
        store.event(run["id"], "provenance", "Unit test declared fixture",
                    data={"candidate_origin": seeded.ORIGIN, "fault_injection": True, "synthetic_defect": self.fixture})
        with patch.object(seeded.Runtime, "invoke", new=invoke), patch.object(self.runtime, "invoke", side_effect=invoke_dispatch), patch.object(self.runtime, "test", side_effect=test), patch.object(self.runtime, "retrieve", new=AsyncMock(return_value=[])):
            if pending_kind:
                pending = asyncio.create_task(engine.execute(run["id"]))
                try:
                    await asyncio.wait_for(pending_reached.wait(), timeout=5)
                    return await seeded.collect_detail(store, self.runtime, run["id"])
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            await engine.execute(run["id"])
        detail = await seeded.collect_detail(store, self.runtime, run["id"])
        self.assertNotIn("develop", invocations)
        return detail

    def classify(self, detail):
        return seeded.classify_seeded_run(detail, self.task, self.fixture)

    async def test_real_engine_evidence_links_fixture_failure_and_live_repair(self):
        detail = await self.make_detail()
        result = self.classify(detail)
        self.assertEqual(detail["status"], "succeeded")
        self.assertTrue(result["automatic_workflow_repair"], result)
        self.assertFalse(result["natural_model_development_failure"])
        self.assertTrue(result["declared_initial_failure_observed"])
        self.assertEqual(len(result["repair_chain"]), 1)
        self.assertEqual(detail["model_calls"], 8)  # plan + 2*(test, inspect, review) + fix
        self.assertEqual(next(e for e in detail["events"] if e["type"] == "agent" and e["node_id"] == "develop")["data"]["model_calls"], 0)
        self.assertTrue(all("context" in s for s in detail["snapshots"]))
        self.assertFalse(detail["applied"])

    async def test_standard_engine_two_round_chain_and_limit_are_preserved(self):
        repaired = self.classify(await self.make_detail(fail_twice=True))
        self.assertTrue(repaired["automatic_workflow_repair"])
        self.assertEqual(len(repaired["repair_chain"]), 2)
        failed = await self.make_detail(never_repair=True)
        self.assertEqual(failed["status"], "needs_human")
        self.assertEqual(failed["attempt"], 2)
        self.assertFalse(self.classify(failed)["automatic_workflow_repair"])
        self.assertFalse(self.classify(failed)["natural_model_development_failure"])

    async def test_declared_source_and_pre_execution_provenance_are_mandatory(self):
        original = await self.make_detail()
        for mutation in ("origin", "flag", "natural", "source_hash", "late", "missing", "workflow", "task", "applied"):
            with self.subTest(mutation=mutation):
                detail = deepcopy(original)
                if mutation == "origin":
                    detail["candidate_origin"] = "live_model_development"
                elif mutation == "flag":
                    detail["fault_injection"] = False
                elif mutation == "natural":
                    detail["natural_model_development_failure"] = True
                elif mutation == "source_hash":
                    detail["synthetic_defect"]["sha256"] = "0" * 64
                elif mutation == "late":
                    declaration = next(e for e in detail["events"] if e["type"] == "provenance")
                    detail["events"].remove(declaration)
                    detail["events"].append(declaration)
                    for index, event in enumerate(detail["events"], 1):
                        event["id"] = index
                elif mutation == "missing":
                    detail["events"] = [e for e in detail["events"] if e["type"] != "provenance"]
                elif mutation == "workflow":
                    detail["workflow"]["max_repairs"] = 3
                elif mutation == "task":
                    detail["task_snapshot"]["acceptance"] += "changed"
                else:
                    detail["applied"] = True
                outcome = self.classify(detail)
                self.assertFalse(outcome["automatic_workflow_repair"])
                self.assertFalse(outcome["natural_model_development_failure"])

    async def test_partial_or_wrong_suite_docker_evidence_cannot_claim_repair(self):
        original = await self.make_detail()
        for mutation in ("timeout", "missing_count", "duplicate_marker", "wrong_hash", "model_tool", "unexpected_failure", "passed_initially"):
            with self.subTest(mutation=mutation):
                detail = deepcopy(original)
                # Mutate all matching test copies consistently, so linkage alone
                # cannot conceal an incomplete or wrongly attributed suite.
                def change(value):
                    if isinstance(value, dict):
                        if value.get("task_id") == "bulk_edit" and "exit_code" in value and value.get("status") == "failed":
                            if mutation == "timeout":
                                value["exit_code"] = None
                            elif mutation == "missing_count":
                                value["stderr"] = "timed out"
                            elif mutation == "duplicate_marker":
                                value["stdout"] *= 2
                            elif mutation == "wrong_hash":
                                value["acceptance_sha256"] = "0" * 64
                            elif mutation == "unexpected_failure":
                                value["stderr"] = "FAIL: test_unknown (unit.fixture)\nRan 10 tests in 0.1s\n"
                            elif mutation == "passed_initially":
                                value.update(fake_test(self.task))
                        for child in value.values():
                            change(child)
                    elif isinstance(value, list):
                        for child in value:
                            change(child)
                change(detail)
                if mutation == "model_tool":
                    for event in detail["events"]:
                        if event.get("data", {}).get("tool") == "run_acceptance":
                            event["data"]["source"] = "model"
                self.assertFalse(self.classify(detail)["automatic_workflow_repair"])

    async def test_feedback_live_fix_write_retest_and_live_review_are_all_required(self):
        original = await self.make_detail()
        for mutation in ("feedback", "mock_fix", "write", "retest", "mock_review", "mock_tester", "mock_plan", "mock_inspect", "stale", "changed_final"):
            with self.subTest(mutation=mutation):
                detail = deepcopy(original)
                events = detail["events"]
                if mutation == "feedback":
                    detail["events"] = [e for e in events if e["type"] != "feedback"]
                elif mutation.startswith("mock_"):
                    node = {"mock_fix": "round-1/fix", "mock_review": "round-1/verify/review", "mock_tester": "round-1/verify/test",
                            "mock_plan": "plan", "mock_inspect": "round-1/verify/inspect"}[mutation]
                    next(e for e in events if e["type"] == "agent" and e["node_id"] == node)["data"]["model_mode"] = "mock"
                elif mutation == "write":
                    detail["events"] = [e for e in events if not (e.get("node_id") == "round-1/fix" and e.get("data", {}).get("tool") == "candidate_diff")]
                elif mutation == "retest":
                    detail["events"] = [e for e in events if not (e.get("node_id") == "round-1/verify/test" and e.get("data", {}).get("tool") == "run_acceptance")]
                elif mutation == "stale":
                    events.append({"id": events[-1]["id"] + 1, "type": "validation", "node_id": "round-1/verify", "data": {}})
                else:
                    detail["diff"]["files"][0]["after"] += "changed after test"
                self.assertFalse(self.classify(detail)["automatic_workflow_repair"])

    async def test_null_node_ids_and_failed_trials_are_exported_without_retry(self):
        detail = await self.make_detail(never_repair=True)
        self.assertTrue(any(e["node_id"] is None for e in detail["events"]))
        trial = {"trial": 1, "task_id": "bulk_edit", "repetition": 1}
        seeded.export_trial(Path(self.temp.name), trial, detail, self.task, self.fixture, "unit-secret")
        evidence = json.loads((Path(self.temp.name) / trial["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(len(evidence["candidate_artifacts"]), 3)
        self.assertEqual(evidence["run"]["events"], detail["events"])
        self.assertEqual(len(evidence["run"]["snapshots"]), len(detail["snapshots"]))
        self.assertEqual(trial["status"], "needs_human")
        self.assertFalse(trial["report"]["natural_model_development_failure"])

    async def test_failed_engine_steps_with_null_outputs_preserve_complete_evidence(self):
        for index, kind in enumerate(("plan", "develop", "test", "inspect", "review", "fix"), 1):
            with self.subTest(kind=kind):
                detail = await self.make_detail(fail_kind=kind)
                self.assertEqual(detail["status"], "failed")
                failed = [step for step in detail["steps"] if step.get("node_id") == kind and step["status"] in ("failed", "blocked")]
                self.assertTrue(failed, detail["steps"])
                self.assertTrue(all(step["output"] is None for step in failed))
                trial = {"trial": index, "task_id": "bulk_edit", "repetition": 1}
                seeded.export_trial(Path(self.temp.name), trial, detail, self.task, self.fixture, "unit-secret")
                evidence = json.loads((Path(self.temp.name) / trial["evidence"]).read_text(encoding="utf-8"))
                self.assertEqual(evidence["run"], detail)
                self.assertFalse(evidence["outcome"]["automatic_workflow_repair"])
                self.assertFalse(evidence["outcome"]["natural_model_development_failure"])

    async def test_pending_engine_steps_with_null_outputs_are_exportable(self):
        for index, kind in enumerate(("plan", "develop", "test", "inspect", "review", "fix"), 1):
            with self.subTest(kind=kind):
                detail = await self.make_detail(pending_kind=kind)
                self.assertEqual(detail["status"], "running")
                pending = [step for step in detail["steps"] if step.get("node_id") == kind and step["status"] == "running"]
                self.assertTrue(pending, detail["steps"])
                self.assertTrue(all(step["output"] is None for step in pending))
                trial = {"trial": index, "task_id": "bulk_edit", "repetition": 1}
                seeded.export_trial(Path(self.temp.name), trial, detail, self.task, self.fixture, "unit-secret")
                evidence = json.loads((Path(self.temp.name) / trial["evidence"]).read_text(encoding="utf-8"))
                self.assertEqual(evidence["run"], detail)
                self.assertFalse(evidence["outcome"]["automatic_workflow_repair"])

    async def test_null_agent_output_and_stale_gate_step_outputs_are_rejected_without_crashing(self):
        original = await self.make_detail()
        for kind in ("develop", "test", "inspect", "review", "verify"):
            with self.subTest(kind=kind):
                detail = deepcopy(original)
                node = "develop" if kind == "develop" else "round-1/verify" + ("" if kind == "verify" else "/" + kind)
                next(step for step in detail["steps"] if step["id"] == node)["output"] = None
                for event in detail["events"]:
                    if event["type"] == "agent" and event["node_id"] == node:
                        event["data"]["output"] = None
                trial = {"trial": 1, "task_id": "bulk_edit", "repetition": 1}
                seeded.export_trial(Path(self.temp.name), trial, detail, self.task, self.fixture, "unit-secret")
                self.assertFalse(trial["report"]["automatic_workflow_repair"])

    async def test_runtime_only_develop_bypasses_base_model_and_preserves_rate_limit(self):
        self.assertEqual(self.runtime.settings["min_request_interval"], 4.2)
        context = {"task_contract": {"id": "bulk_edit"}}
        with patch.object(seeded.Runtime, "invoke", new=AsyncMock(return_value={"unit": "base called"})) as base:
            output = await self.runtime.invoke({}, {"kind": "develop", "mode": "live"}, context)
            self.assertEqual(output["model_mode"], "synthetic_fixture")
            self.assertEqual(output["model_calls"], 0)
            base.assert_not_awaited()
            for kind in ("plan", "test", "inspect", "review", "fix"):
                self.assertEqual(await self.runtime.invoke({}, {"kind": kind, "mode": "live"}, context), {"unit": "base called"})
            self.assertEqual(base.await_count, 5)
            with self.assertRaises(seeded.RuntimeErrorSafe):
                await self.runtime.invoke({}, {"kind": "develop", "mode": "mock"}, context)
        with patch.object(seeded, "fixture_record", return_value=({**self.fixture, "sha256": "changed"}, self.source)):
            with self.assertRaises(seeded.RuntimeErrorSafe):
                await self.runtime.invoke({}, {"kind": "develop", "mode": "live"}, context)

    async def test_controls_flags_do_not_substitute_for_all_eight_raw_results(self):
        # Intentionally incomplete evidence: no runtime/docker call is made.
        path = Path(self.temp.name) / "controls.json"
        path.write_text(json.dumps({"kind": "frozen_task_suites_synthetic_controls_real_docker", "passed": True,
                                   "model_calls": 0, "baselines_unchanged": True, "applied": False,
                                   "runner_manifest": self.runtime._acceptance_manifest(), "tasks": [], "samples": []}), encoding="utf-8")
        with self.assertRaisesRegex(seeded.RuntimeErrorSafe, "eight"):
            seeded.validate_control_evidence(path, self.runtime)


if __name__ == "__main__":
    unittest.main()
