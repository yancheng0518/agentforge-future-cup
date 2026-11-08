"""Evidence/precondition regressions only; no model or candidate is executed."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

spec = importlib.util.spec_from_file_location("live_repair_probe", Path(__file__).resolve().parents[1] / "scripts/probe_live_repair.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def result(passed=False):
    return {"status": "passed" if passed else "failed", "isolated": True, "exit_code": 0 if passed else 1,
            "stdout": 'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": ' + str(passed).lower() + '}\n',
            "stderr": "Ran 10 tests in 0.5s\n" + ("OK\n" if passed else
                       "FAIL: test_filter_completed (test_todo.TodoAcceptance.test_filter_completed)\n"
                       "FAIL: test_filter_incomplete (test_todo.TodoAcceptance.test_filter_incomplete)\nFAILED (failures=2)\n")}


class LiveRepairEvidenceTests(unittest.TestCase):
    def fixture(self):
        fixed = probe.SEED + "\n# fake test data, not executed\n"
        return {"seed": {"sha256": probe.sha(probe.SEED), "test": result()}, "rounds": [
            {"round": 1, "input_sha256": probe.sha(probe.SEED), "feedback_test": result(),
             "output_sha256": probe.sha(fixed), "agent": {"id": "fixer"},
             "model_result": {"model_mode": "live", "model_calls": 1, "output": {"files": [{"path": "todo.py", "content": fixed}]}},
             "test": result(True), "acceptance_unchanged": True, "candidate_unchanged_during_test": True}]}

    def test_success_remains_explicitly_synthetic(self):
        outcome = probe.assess(self.fixture())
        self.assertTrue(outcome["passed"])
        self.assertFalse(outcome["natural_model_development_failure"])
        self.assertFalse(outcome["original_workflow_self_correction"])

    def test_startup_timeout_or_unrelated_failure_is_not_seed_evidence(self):
        for update in ({"exit_code": 125}, {"exit_code": None}, {"isolated": False},
                       {"stderr": "Ran 10 tests in 0.5s\nFAILED (errors=10)"}, {"stdout": ""}):
            with self.subTest(update=update):
                test = result()
                test.update(update)
                self.assertFalse(probe.expected_seed_failure(test))

    def test_mock_response_or_unlinked_feedback_cannot_pass(self):
        for field, value in (("feedback_test", result(True)), ("input_sha256", "old"),
                             ("output_sha256", "old"), ("acceptance_unchanged", False),
                             ("candidate_unchanged_during_test", False)):
            evidence = self.fixture()
            evidence["rounds"][0][field] = value
            self.assertFalse(probe.assess(evidence)["passed"], field)
        evidence = self.fixture()
        evidence["rounds"][0]["model_result"]["model_mode"] = "mock"
        self.assertFalse(probe.assess(evidence)["passed"])

    def test_requested_review_requires_current_live_passing_review(self):
        evidence = self.fixture()
        evidence["review_requested"] = True
        self.assertFalse(probe.assess(evidence)["passed"])
        evidence["review"] = {"candidate_sha256": evidence["rounds"][0]["output_sha256"],
                              "model_result": {"model_mode": "live", "model_calls": 1, "output": {"passed": True}}}
        self.assertTrue(probe.assess(evidence)["passed"])
        evidence["review"]["candidate_sha256"] = "stale"
        self.assertFalse(probe.assess(evidence)["passed"])


class LiveRepairPreconditionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_model_when_sandbox_missing(self):
        runtime = Mock(settings={"api_key": "unit-secret"})
        runtime.health = AsyncMock(return_value={"sandbox": {"available": False}, "model": {"configured": True}})
        runtime.invoke = AsyncMock()
        with tempfile.TemporaryDirectory() as folder:
            evidence = await probe.run_experiment(runtime, Path(folder))
        self.assertFalse(evidence["outcome"]["passed"])
        runtime.invoke.assert_not_awaited()

    async def test_no_model_when_seed_does_not_reproduce_declared_defect(self):
        runtime = Mock(settings={"api_key": "unit-secret"})
        runtime.health = AsyncMock(return_value={"sandbox": {"available": True}, "model": {"configured": True}})
        runtime.prepare = AsyncMock(return_value={})
        runtime.apply_candidate = AsyncMock()
        runtime.test = AsyncMock(return_value=result(True))
        runtime.invoke = AsyncMock()
        with tempfile.TemporaryDirectory() as folder, patch.object(probe, "acceptance_manifest", return_value={"runner.py": "hash"}):
            evidence = await probe.run_experiment(runtime, Path(folder))
        self.assertIn("blocked", evidence)
        runtime.invoke.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
