"""Evidence parsing only; never executes candidate code or Docker."""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("probe_task_suites", Path(__file__).resolve().parents[1] / "scripts/probe_task_suites.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class TaskControlEvidenceTests(unittest.TestCase):
    def result(self, passed=True):
        return {"status": "passed" if passed else "failed", "exit_code": 0 if passed else 1, "isolated": True,
                "stdout": 'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": ' + str(passed).lower() + '}\n',
                "stderr": "Ran 10 tests in 0.010s\n" + ("OK\n" if passed else "FAIL: test_filter (test_todo.C.test_filter)\nFAILED (failures=1)\n")}

    def test_complete_reference_and_declared_assertion_failure(self):
        self.assertTrue(probe.assess_result(self.result())["expected_outcome_verified"])
        self.assertTrue(probe.assess_result(self.result(False), {"test_filter"})["expected_outcome_verified"])

    def test_infrastructure_or_incomplete_suite_not_a_control_failure(self):
        for changes in ({"exit_code": None}, {"exit_code": 125}, {"isolated": False}, {"status": "blocked"},
                        {"stdout": ""}, {"stderr": "RPC timeout"}, {"exit_code": True}):
            with self.subTest(changes=changes):
                result = {**self.result(False), **changes}
                self.assertFalse(probe.assess_result(result, {"test_filter"})["expected_outcome_verified"])

    def test_unexpected_failure_error_or_duplicate_attestation_rejected(self):
        for suffix in ("FAIL: test_unexpected (test_todo.C.test_unexpected)\n",
                       "ERROR: test_unexpected (test_todo.C.test_unexpected)\n"):
            result = self.result(False)
            result["stderr"] += suffix
            self.assertFalse(probe.assess_result(result, {"test_filter"})["expected_outcome_verified"])
        result = self.result(False)
        result["stdout"] *= 2
        self.assertFalse(probe.assess_result(result, {"test_filter"})["expected_outcome_verified"])

    def test_exact_count_and_boolean_attestation_types_required(self):
        for attestation in ('{"tests": 9, "passed": false}', '{"tests": 10, "passed": 0}', '{"tests": "10", "passed": false}'):
            result = self.result(False)
            result["stdout"] = "AGENTFORGE_ACCEPTANCE=" + attestation + "\n"
            self.assertFalse(probe.assess_result(result, {"test_filter"})["expected_outcome_verified"])
