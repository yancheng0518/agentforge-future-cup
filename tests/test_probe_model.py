"""Probe error evidence tests; no real credentials or model network requests."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error


spec = importlib.util.spec_from_file_location("probe_model", Path(__file__).resolve().parents[1] / "scripts/probe_model.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def http_error(status, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return urllib.error.HTTPError("https://example.invalid/v1/chat/completions", status, "provider error", {}, io.BytesIO(raw))


class ProbeDiagnosticsTests(unittest.TestCase):
    def test_channel_failure_is_distinct_from_token_authorization(self):
        for status, message, expected in (
            (503, "No available channel for model token-model under group default (distributor)", "model_channel_unavailable"),
            (403, "This token has no access to model fake-model", "credential_or_authorization"),
            (403, "Insufficient balance", "quota_or_balance"),
            (403, "Model not enabled", "model_not_enabled"),
        ):
            with self.subTest(status=status, message=message):
                result = probe.http_diagnostics(http_error(status, {"error": {"message": message}}))
                self.assertEqual(result["error_category"], expected)
                self.assertEqual(result["http_status"], status)
                self.assertNotIn(message, result["error"])

    def test_only_explicit_provider_message_establishes_channel_failure(self):
        for body in (b"<html>upstream failed</html>", [], {"error": {"message": None}},
                     {"error": {"message": "upstream unavailable"}, "debug": "token quota no available channel for model"}):
            with self.subTest(body=body):
                result = probe.http_diagnostics(http_error(503, body))
                self.assertEqual(result["error_category"], "provider_unavailable")

    def test_provider_response_fields_are_never_retained(self):
        secret = "test-secret-must-not-be-written"
        body = {"error": {"message": "No available channel for model " + secret, "code": secret}, "debug": secret}
        result = probe.http_diagnostics(http_error(503, body))
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn("provider_error_message", result)
        self.assertNotIn("provider_error_code", result)


class ProbeEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_failure_writes_safe_evidence_without_claiming_protocol_success(self):
        secret = "test-secret-must-not-be-written"
        runtime = MagicMock(settings={"api_key": secret, "base_url": "https://example.invalid/v1", "model": "fake-model"})

        def fail(_payload):
            try:
                raise http_error(503, {"error": {"message": "No available channel for model fake-model under group default (distributor) " + secret}})
            except urllib.error.HTTPError:
                raise probe.RuntimeErrorSafe("模型 HTTP 503；检查服务开通、模型 ID、额度和权限。") from None

        runtime._post.side_effect = fail
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "probe.json"
            stdout = io.StringIO()
            with patch.object(probe, "Runtime", return_value=runtime), patch.object(probe.sys, "argv", ["probe_model.py", "--output", str(output)]), patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network prohibited")), contextlib.redirect_stdout(stdout):
                self.assertEqual(await probe.main(), 2)
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(evidence["requests"], 1)
            self.assertEqual(evidence["error_category"], "model_channel_unavailable")
            self.assertIn("服务渠道", evidence["error"])
            self.assertFalse(evidence["passed"])
            self.assertFalse(evidence["tool_calls_verified"])
            self.assertFalse(evidence["json_verified"])
            self.assertNotIn(secret, output.read_text(encoding="utf-8") + stdout.getvalue())
            runtime._post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
