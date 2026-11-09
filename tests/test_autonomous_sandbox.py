"""Generic sandbox contract tests; model/container transports are mocked here."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.autonomous_sandbox import AutonomousSandbox, SandboxIntegrityError, validate_bundle
from agentforge.runtime import Runtime, RuntimeErrorSafe


BUNDLE = {
    "requirement": "Return the sum of two integers from arithmetic.add.",
    "files": {"arithmetic.py": "def add(a, b):\n    return a + b\n"},
    "acceptance": "import unittest\nfrom bridge import call\nclass Tests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(call('arithmetic', 'add', 2, 3), 5)\n",
    "provenance": {"kind": "natural", "description": "Unit fixture, not real execution evidence"},
}


class BundleTests(unittest.TestCase):
    def test_detached_bundle_and_arbitrary_module_names(self):
        value = {**BUNDLE, "files": {"analytics/__init__.py": "", "analytics/csv_summary.py": "# source"}}
        frozen = validate_bundle(value)
        frozen["provenance"]["kind"] = "seeded_defect"
        self.assertEqual(value["provenance"]["kind"], "natural")
        self.assertEqual(frozen["files"], value["files"])

    def test_invalid_paths_rejected(self):
        for path in ("../outside.py", "/outside.py", "C:/outside.py", "pkg\\a.py", "a.py/../b.py", "a.txt", "CON.py", "pkg/NUL.py", "a//b.py", "a-b.py"):
            with self.subTest(path=path), self.assertRaises(RuntimeErrorSafe):
                validate_bundle({**BUNDLE, "files": {path: "x"}})

    def test_case_collisions_and_size_limits_rejected(self):
        for files in ({"a.py": "", "A.py": ""}, {"pkg/a.py": "", "PKG/b.py": ""}, {"a.py": "x" * 65537}, {}, {f"a{i}.py": "" for i in range(33)}):
            with self.subTest(files=list(files)), self.assertRaises(RuntimeErrorSafe):
                validate_bundle({**BUNDLE, "files": files})

    def test_provenance_and_extra_fields_not_silently_inferred(self):
        for value in ({**BUNDLE, "provenance": {}}, {**BUNDLE, "extra": 1}, {**BUNDLE, "acceptance": ""}):
            with self.assertRaises(RuntimeErrorSafe):
                validate_bundle(value)


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentforge-autonomous-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "sandbox" / "autonomous", self.root / "sandbox" / "autonomous")
        settings = {"image": "unit:never-run", "docker_backend": "native", "api_key": "", "model": "", "base_url": "https://example.invalid/v1", "timeout": 5, "key_source": "none"}
        with patch("agentforge.runtime._settings", return_value=settings):
            self.runtime = Runtime(self.root, self.root / "data")
        self.runtime.health = AsyncMock(return_value={"sandbox": {"available": True, "reason": "test transport"}})
        self.sandbox = AutonomousSandbox(self.runtime)
        self.command_calls = []

        async def command(args, timeout=8):
            self.command_calls.append((args, timeout))
            if args[1:3] == ["image", "inspect"]:
                return 0, "sha256:" + "a" * 64, ""
            if args[1] == "run":
                attestation = {"nonce": args[-1], "tests": 1, "discovered": 1, "passed": True}
                return 0, "AGENTFORGE_AUTONOMOUS=" + json.dumps(attestation) + "\n", "test_add ... ok"
            return 0, "", ""

        self.runtime._command = AsyncMock(side_effect=command)

    async def test_prepare_freezes_sources_suite_and_runner(self):
        manifest = await self.sandbox.prepare("run", BUNDLE)
        self.assertEqual(await self.sandbox.read("run"), BUNDLE["files"])
        self.assertEqual(await self.sandbox.prepare("run", BUNDLE), manifest)
        with self.assertRaises(RuntimeErrorSafe):
            await self.sandbox.prepare("run", {**BUNDLE, "requirement": "Changed"})
        base = self.runtime._run_dir("run")
        self.assertEqual(len(list((base / "evidence").glob("*.json"))), 1)
        self.runtime._command.assert_not_called()

    async def test_apply_validates_whole_batch_before_editing(self):
        await self.sandbox.prepare("run", BUNDLE)
        with self.assertRaises(RuntimeErrorSafe):
            await self.sandbox.apply("run", {"arithmetic.py": "changed", "escape.py": "bad"}, ["arithmetic.py"])
        self.assertEqual(await self.sandbox.read("run"), BUNDLE["files"])
        record = await self.sandbox.apply("run", {"arithmetic.py": "# changed", "helpers.py": "# new"}, ["arithmetic.py", "helpers.py"])
        self.assertEqual(record["candidate_sha256"], await self.sandbox.fingerprint("run"))
        evidence = json.loads((self.runtime._run_dir("run") / record["snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["changed_paths"], ["arithmetic.py", "helpers.py"])
        self.assertEqual(evidence["files"]["helpers.py"], "# new")

    async def test_candidate_is_not_executed_on_host(self):
        sentinel = self.root / "UNSAFE_HOST_EXECUTION"
        source = "from pathlib import Path\nPath(" + repr(str(sentinel)) + ").touch()\n"
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("Host execution prohibited")):
            await self.sandbox.prepare("run", {**BUNDLE, "files": {"arithmetic.py": source}})
            await self.sandbox.read("run")
            await self.sandbox.fingerprint("run")
            await self.sandbox.apply("run", {"other.py": source}, ["other.py"])
        self.assertFalse(sentinel.exists())

    async def test_multi_file_io_error_restores_previous_candidate(self):
        await self.sandbox.prepare("run", BUNDLE)
        original = self.sandbox._write
        failed = False
        def write(path, text, base):
            nonlocal failed
            if path.name == "helpers.py" and not failed:
                failed = True
                raise OSError("Synthetic disk write failure")
            original(path, text, base)
        with patch.object(self.sandbox, "_write", side_effect=write):
            with self.assertRaisesRegex(RuntimeErrorSafe, "已恢复"):
                await self.sandbox.apply("run", {"arithmetic.py": "# overwritten", "helpers.py": "# new"}, ["arithmetic.py", "helpers.py"])
        self.assertEqual(await self.sandbox.read("run"), BUNDLE["files"])
        failures = list((self.runtime._run_dir("run") / "evidence").glob("*-apply-failed-*.json"))
        self.assertEqual(len(failures), 1)
        self.assertTrue(json.loads(failures[0].read_text(encoding="utf-8"))["rollback_completed"])

    async def test_rollback_failure_raises_terminal_integrity_error(self):
        await self.sandbox.prepare("run", BUNDLE)
        original = self.sandbox._write
        failed = False
        def write(path, text, base):
            nonlocal failed
            if path.name == "helpers.py":
                failed = True
                raise OSError("Synthetic write failure")
            if failed and path.name == "arithmetic.py":
                raise OSError("Synthetic rollback failure")
            original(path, text, base)
        with patch.object(self.sandbox, "_write", side_effect=write):
            with self.assertRaises(SandboxIntegrityError):
                await self.sandbox.apply("run", {"arithmetic.py": "# overwritten", "helpers.py": "# new"}, ["arithmetic.py", "helpers.py"])
        failures = list((self.runtime._run_dir("run") / "evidence").glob("*-apply-failed-*.json"))
        self.assertEqual(len(failures), 1)
        self.assertFalse(json.loads(failures[0].read_text(encoding="utf-8"))["rollback_completed"])

    async def test_immutable_inputs_cannot_be_changed(self):
        for relative in ("bundle.json", "trusted/test_acceptance.py", "runner/worker.py"):
            run = "run-" + relative.replace("/", "-").replace(".", "-")
            await self.sandbox.prepare(run, BUNDLE)
            target = self.runtime._run_dir(run) / relative
            target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
            # Semantic bundle JSON changes are detected, whitespace alone is not.
            if relative == "bundle.json":
                frozen = json.loads(target.read_text(encoding="utf-8"))
                frozen["requirement"] = "tampered"
                target.write_text(json.dumps(frozen), encoding="utf-8")
            with self.subTest(file=relative), self.assertRaises(RuntimeErrorSafe):
                await self.sandbox.test(run)
        self.runtime._command.assert_not_called()

    async def test_actual_docker_command_is_pinned_and_hardened(self):
        await self.sandbox.prepare("run", BUNDLE)
        result = await self.sandbox.test("run")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["test_count"], 1)
        self.assertTrue(result["isolated"])
        command = next(args for args, _ in self.command_calls if args[1] == "run")
        for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "no-new-privileges", "--pids-limit", "--memory", "65534:65534"):
            self.assertIn(flag, command)
        self.assertIn("sha256:" + "a" * 64, command)
        self.assertEqual(command.count("--mount"), 3)
        self.assertTrue(all("readonly" in command[index + 1] for index, value in enumerate(command) if value == "--mount"))
        self.assertEqual(self.command_calls[-1][0][1:3], ["rm", "-f"])
        self.assertTrue((self.runtime._run_dir("run") / result["result_record"]).exists())

    async def test_docker_unavailable_does_not_fallback(self):
        await self.sandbox.prepare("run", BUNDLE)
        self.runtime.health.return_value = {"sandbox": {"available": False, "reason": "offline"}}
        result = await self.sandbox.test("run")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["isolated"])
        self.runtime._command.assert_not_called()

    async def test_empty_forged_incomplete_or_missing_attestation_cannot_pass(self):
        await self.sandbox.prepare("run", BUNDLE)
        original = self.runtime._command.side_effect
        invalid = (None, {"nonce": "wrong", "tests": 1, "discovered": 1, "passed": True},
                   {"tests": 0, "discovered": 0, "passed": True}, {"tests": 1, "discovered": 2, "passed": True})
        for value in invalid:
            async def command(args, timeout=8):
                if args[1] == "run":
                    result = dict(value) if value is not None else None
                    if result is not None:
                        result.setdefault("nonce", args[-1])
                    return 0, "" if result is None else "AGENTFORGE_AUTONOMOUS=" + json.dumps(result), ""
                return await original(args, timeout)
            self.runtime._command.side_effect = command
            with self.subTest(attestation=value):
                self.assertEqual((await self.sandbox.test("run"))["status"], "blocked")

    async def test_candidate_edit_during_test_invalidates_pass(self):
        await self.sandbox.prepare("run", BUNDLE)
        original = self.runtime._command.side_effect
        async def command(args, timeout=8):
            result = await original(args, timeout)
            if args[1] == "run":
                await self.sandbox.apply("run", {"arithmetic.py": "# external change"}, ["arithmetic.py"])
            return result
        self.runtime._command.side_effect = command
        result = await self.sandbox.test("run")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("变化", result["reason"])

    async def test_timeout_cleans_up_container_and_keeps_evidence(self):
        await self.sandbox.prepare("run", BUNDLE)
        original = self.runtime._command.side_effect
        async def command(args, timeout=8):
            if args[1] == "run":
                raise TimeoutError()
            return await original(args, timeout)
        self.runtime._command.side_effect = command
        result = await self.sandbox.test("run")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["isolated"])
        self.assertTrue(result["container_invoked"])
        self.assertEqual(self.command_calls[-1][0][1:3], ["rm", "-f"])
        self.assertTrue((self.runtime._run_dir("run") / result["result_record"]).exists())

    async def test_cancelled_docker_retains_unknown_result_and_cleans_up(self):
        await self.sandbox.prepare("run", BUNDLE)
        original = self.runtime._command.side_effect
        async def command(args, timeout=8):
            if args[1] == "run":
                raise asyncio.CancelledError()
            return await original(args, timeout)
        self.runtime._command.side_effect = command
        with self.assertRaises(asyncio.CancelledError):
            await self.sandbox.test("run")
        self.assertEqual(self.command_calls[-1][0][1:3], ["rm", "-f"])
        records = list((self.runtime._run_dir("run") / "evidence").glob("*-validation-result-*.json"))
        self.assertEqual(len(records), 1)
        result = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertTrue(result["completion_unknown"])
        self.assertFalse(result["isolated"])


if __name__ == "__main__":
    unittest.main()
