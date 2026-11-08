"""Evidence and precondition tests; no server, model, or candidate is executed."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location("live_demo", Path(__file__).resolve().parents[1] / "scripts/run_live_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class LiveDemoTests(unittest.TestCase):
    def successful_detail(self, repair=False, review_failure=False):
        steps = []
        events = [{"type": "agent", "node_id": "develop", "data": {"model_mode": "live", "model_calls": 1, "output": {"summary": "candidate"}}}]

        def gate(attempt, passed, source, test_passed):
            scope = {"id": f"round-{attempt}/verify", "attempt": attempt,
                     "candidate_hash": demo.candidate_fingerprint({"files": [{"path": "todo.py", "after": source}]})}
            raw_test = {"status": "passed" if test_passed else "failed", "isolated": True, "exit_code": 0 if test_passed else 1,
                        "stdout": 'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": ' + str(test_passed).lower() + '}\n'}
            test = {**raw_test, "evidence_scope": scope}
            review = {"passed": passed, "issues": [], "evidence_scope": scope}
            steps.extend([{"id": scope["id"], "status": "succeeded", "output": {"test": test, "review": review, "validation_scope": scope}},
                          {"id": scope["id"] + "/test", "status": "succeeded", "output": {"test": test}}])
            events.extend([{"type": "tool", "node_id": scope["id"] + "/test", "data": {"tool": "run_acceptance", "source": "runtime", "output": raw_test}},
                           {"type": "validation", "node_id": f"round-{attempt}/gate", "data": {"passed": passed, "test": test, "review": review, "attempt": attempt}}])
            return test

        if repair:
            failed_test = gate(0, False, "old candidate", review_failure)
            events.append({"type": "feedback", "node_id": "round-0/gate", "data": {"next_attempt": 1, "test": failed_test}})
            fix_output = {"files": [{"path": "todo.py", "content": "new candidate"}]}
            steps.append({"id": "round-1/fix", "status": "succeeded", "output": fix_output})
            events.extend([{"type": "agent", "node_id": "round-1/fix", "data": {"model_mode": "live", "model_calls": 1, "output": fix_output}},
                           {"type": "tool", "node_id": "round-1/fix", "data": {"tool": "candidate_diff", "source": "runtime", "status": "completed",
                            "output": {"files": [{"path": "todo.py", "after": "new candidate"}]}}}])
        gate(int(repair), True, "new candidate", True)
        for i, event in enumerate(events, 1):
            event["id"] = i
        diff = {"files": [{"path": "todo.py", "after": "new candidate"}]}
        return {"id": "a" * 32, "mode": "live", "status": "succeeded", "attempt": int(repair), "events": events,
                "steps": steps, "diff": diff, "verified_hash": demo.candidate_fingerprint(diff)}

    def test_first_pass_success_is_not_self_correction_evidence(self):
        result = demo.extract_outcome(self.successful_detail())
        self.assertTrue(result["workflow_succeeded"])
        self.assertFalse(result["repair_observed"])

    def test_actual_validation_feedback_and_round_are_required(self):
        detail = self.successful_detail(True)
        self.assertTrue(demo.extract_outcome(detail)["repair_observed"])
        self.assertTrue(demo.extract_outcome(detail)["repair_after_test_failure_observed"])
        detail["events"] = [event for event in detail["events"] if event["type"] != "feedback"]
        self.assertFalse(demo.extract_outcome(detail)["repair_observed"])

    def test_review_failure_is_not_labeled_as_test_failure(self):
        detail = self.successful_detail(True, review_failure=True)
        result = demo.extract_outcome(detail)
        self.assertTrue(result["repair_observed"])
        self.assertFalse(result["real_isolated_test_failure_observed"])
        self.assertFalse(result["repair_after_test_failure_observed"])

    def test_wrong_event_order_cannot_claim_repair(self):
        detail = self.successful_detail(True)
        feedback = next(e for e in detail["events"] if e["type"] == "feedback")
        detail["events"].remove(feedback)
        detail["events"].append(feedback)
        # Keep increasing IDs so this also tests the actual causal order.
        for i, event in enumerate(detail["events"], 1):
            event["id"] = i
        self.assertFalse(demo.extract_outcome(detail)["repair_observed"])

    def test_missing_real_fix_output_or_completed_fix_is_not_repair(self):
        for mode in ("missing", "mock", "failed_step", "missing_write"):
            detail = self.successful_detail(True)
            if mode == "missing":
                detail["events"] = [e for e in detail["events"] if not (e["type"] == "agent" and e.get("node_id") == "round-1/fix")]
            elif mode == "mock":
                next(e for e in detail["events"] if e["type"] == "agent" and e.get("node_id") == "round-1/fix")["data"]["model_mode"] = "mock"
            elif mode == "missing_write":
                detail["events"] = [e for e in detail["events"] if e.get("data", {}).get("tool") != "candidate_diff"]
            else:
                next(s for s in detail["steps"] if s["id"] == "round-1/fix")["status"] = "failed"
            self.assertFalse(demo.extract_outcome(detail)["repair_observed"], mode)

    def test_invalidated_or_stale_gate_cannot_claim_current_success(self):
        detail = self.successful_detail()
        detail["events"].append({"id": 100, "type": "validation", "node_id": "round-0/verify", "data": {}})
        self.assertFalse(demo.extract_outcome(detail)["workflow_succeeded"])
        self.assertFalse(demo.extract_outcome(detail)["real_isolated_test_passed"])
        detail = self.successful_detail()
        detail["diff"]["files"][0]["after"] = "candidate changed since test"
        self.assertFalse(demo.extract_outcome(detail)["workflow_succeeded"])

    def test_unscoped_or_model_reported_test_cannot_claim_real_test(self):
        detail = self.successful_detail()
        next(e for e in detail["events"] if e["type"] == "tool")["data"]["source"] = "model"
        self.assertFalse(demo.extract_outcome(detail)["real_isolated_test_passed"])

    def test_failed_workflow_note_does_not_claim_first_pass_success(self):
        detail = self.successful_detail()
        detail["status"] = "needs_human"
        outcome = demo.extract_outcome(detail)
        self.assertFalse(outcome["workflow_succeeded"])
        self.assertNotIn("first-pass success", outcome["repair_note"])

    def test_missing_isolation_prevents_submission(self):
        health = {"model": {"configured": True}, "sandbox": {"available": False}}
        with patch.object(demo, "api_request", return_value=health) as request, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(demo.main([]), 2)
        request.assert_called_once_with("http://127.0.0.1:8000/api", "/health")

    def test_cannot_resume_mock_as_live(self):
        health = {"model": {"configured": True}, "sandbox": {"available": True}}
        with patch.object(demo, "api_request", side_effect=[health, {"mode": "mock", "status": "blocked"}]) as request, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(demo.main(["--resume", "a" * 32]), 2)
        self.assertEqual(request.call_count, 2)

    def test_require_repair_returns_distinct_status_without_fabricating_failure(self):
        health = {"model": {"configured": True}, "sandbox": {"available": True}}
        detail = self.successful_detail()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            fake_runtime = MagicMock(settings={"api_key": "local-test-secret"})
            with patch.object(demo, "api_request", side_effect=[health, {"id": "a" * 32}, detail]), patch.object(demo, "Runtime", return_value=fake_runtime), contextlib.redirect_stdout(io.StringIO()):
                code = demo.main(["--require-repair", "--output", str(output)])
            self.assertEqual(code, 3)
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(evidence["outcome"]["workflow_succeeded"])
            self.assertFalse(evidence["outcome"]["repair_observed"])
            self.assertEqual(evidence["run"]["status"], "succeeded")

    def test_evidence_secret_redaction(self):
        result = demo.redact({"api_key": "unknown", "events": ["value local-test-secret", "sk-longgeneratedsecret123"]}, "local-test-secret")
        serialized = json.dumps(result)
        self.assertNotIn("local-test-secret", serialized)
        self.assertNotIn("sk-longgeneratedsecret123", serialized)
        self.assertEqual(result["api_key"], "[REDACTED]")
