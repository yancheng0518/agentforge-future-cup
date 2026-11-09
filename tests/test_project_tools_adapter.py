"""Real adapter/SQLite, fake application transport; no Docker/model/code execution."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.project_runner import ProjectRunner, digest
from agentforge.project_tools import ProjectTools
from agentforge.projects import ProjectStore
from agentforge.autonomous_sandbox import SandboxIntegrityError


class FakeSandbox:
    def __init__(self):
        self.files = {"main.py": "# unit text only, version one\n"}

    async def read(self, run_id):
        return deepcopy(self.files)

    async def fingerprint(self, run_id):
        return digest(self.files)


class ProjectToolsAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentforge-adapter-unit-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.real_store = ProjectStore(self.directory / "user-projects/projects.sqlite3")
        self.real_project = self.real_store.import_project("User project", {"main.py": "# never execute\n"})
        self.user_id = self.real_project["project"]["id"]
        self.real_store.commit_execution(self.user_id, {"kind": "unit seed"},
                                         state={"private.json": "VVNFUi1SRUFMLVNUQVRF"}, expected_state_sha256=digest({}))
        self.original_user_state = self.real_store.application_state(self.user_id)
        self.original_user_executions = self.real_store.executions(self.user_id)
        self.sandbox = FakeSandbox()
        self.runs = {name: {"id": name, "project_id": self.user_id,
                            "project_context": deepcopy(self.real_project["revision"])} for name in ("run-one", "run-two")}
        self.runtime = SimpleNamespace(root=ROOT, data_dir=self.directory / "autonomous",
                                       _run_dir=lambda run_id: self.directory / "autonomous/runs" / run_id)
        self.engine = SimpleNamespace(runtime=self.runtime, sandbox=self.sandbox,
                                      store=SimpleNamespace(run=lambda run_id: deepcopy(self.runs.get(run_id))))
        self.adapter = ProjectTools(self.engine)
        self.calls, self.effects = [], []

        async def fake_execute(runner, project_id, revision, entrypoint, arguments, files):
            before = runner.store.application_state(project_id)
            self.calls.append({"project_id": project_id, "revision": deepcopy(revision), "files": deepcopy(files),
                               "state": deepcopy(before), "directory": runner.directory, "entrypoint": entrypoint,
                               "args": deepcopy(arguments)})
            effect = self.effects.pop(0) if self.effects else {}
            status = effect.get("status", "succeeded")
            updated = deepcopy(effect.get("state", before["state"]))
            record = {"kind": "unit_fake_application", "status": status, "source_sha256": digest(files),
                      "exit_code": 0 if status == "succeeded" else 1, "data_saved": status == "succeeded",
                      "data_before_sha256": before["sha256"], "data_after_sha256": digest(updated) if status == "succeeded" else before["sha256"],
                      "test_claim": False, "stdout": "Explicit fake transport; no execution"}
            if effect.get("raise"):
                runner.store.save_execution(project_id, {**record, "status": "blocked", "data_saved": False, "completion_unknown": True})
                raise effect["raise"]
            if status == "succeeded":
                runner.store.commit_execution(project_id, record, updated, before["sha256"])
            else:
                runner.store.save_execution(project_id, record)
            if "mutate_files" in effect:
                self.sandbox.files = deepcopy(effect["mutate_files"])
            return {**record, **effect.get("returned", {})}

        for guard in (patch.object(ProjectRunner, "_execute", new=fake_execute),
                      patch("subprocess.Popen", side_effect=AssertionError("No candidate processes in adapter tests")),
                      patch("asyncio.create_subprocess_exec", side_effect=AssertionError("No Docker in adapter tests")),
                      patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network in adapter tests"))):
            guard.start()
            self.addCleanup(guard.stop)

    async def application(self, run_id="run-one", **arguments):
        return await self.adapter(run_id, "run_application", {"entrypoint": "main.py", "args": [], **arguments})

    def scratch(self, run_id="run-one"):
        base = self.runtime._run_dir(run_id) / "application-tools"
        store = ProjectStore(base / "scratch.sqlite3")
        marker = json.loads((base / "last-project.json").read_text(encoding="utf-8"))
        return store, marker["project_id"]

    def assert_user_state_untouched(self):
        self.assertEqual(self.real_store.application_state(self.user_id), self.original_user_state)
        self.assertEqual(self.real_store.executions(self.user_id), self.original_user_executions)
        self.assertEqual(self.real_store.revisions(self.user_id), [self.real_project["revision"]])

    async def test_read_file_is_candidate_text_only_with_bounded_lines_and_content(self):
        self.sandbox.files = {"main.py": "\n".join(f"line {i}" for i in range(1, 501)), "large.py": "中" * 20000}
        full = await self.adapter("run-one", "read_file", {"path": "main.py"})
        self.assertEqual(full["content"].splitlines()[0], "line 1")
        self.assertEqual(full["content"].splitlines()[-1], "line 400")
        self.assertTrue(full["truncated"])
        tail = await self.adapter("run-one", "read_file", {"path": "main.py", "start_line": 401, "end_line": 500})
        self.assertEqual(tail["content"].splitlines(), [f"line {i}" for i in range(401, 501)])
        self.assertFalse(tail["truncated"])
        large = await self.adapter("run-one", "read_file", {"path": "large.py"})
        self.assertEqual(len(large["content"]), 16000)
        self.assertTrue(large["truncated"])
        for result in (full, tail, large):
            self.assertFalse(result["executed_code"])
            self.assertEqual(result["candidate_sha256"], digest(self.sandbox.files))
        self.assertFalse(self.calls)
        self.assertFalse((self.runtime._run_dir("run-one") / "application-tools").exists())
        self.assert_user_state_untouched()

    async def test_read_file_rejects_escape_extra_fields_and_invalid_ranges(self):
        for args in ({"path": "../main.py"}, {"path": "C:/server.py"}, {"path": "missing.py"},
                     {"path": "main.py", "command": "shell"}, {"path": "main.py", "start_line": 0},
                     {"path": "main.py", "start_line": True}, {"path": "main.py", "end_line": 401},
                     {"path": "main.py", "start_line": 3, "end_line": 2},
                     {"path": "main.py", "start_line": 1.0}, {"path": "main.py", "end_line": None}):
            with self.subTest(arguments=args), self.assertRaises(ValueError):
                await self.adapter("run-one", "read_file", args)
        self.assertFalse(self.calls)

    async def test_search_is_literal_casefolded_and_result_bounded(self):
        self.sandbox.files = {"main.py": "\n".join("Needle " + "x" * 400 for _ in range(40)), "other.py": "literal .*\nStraße\n"}
        result = await self.adapter("run-one", "search_project", {"query": "nEEdLE"})
        self.assertEqual(len(result["matches"]), 30)
        self.assertTrue(result["truncated"])
        self.assertTrue(all(len(m["text"]) == 320 and m["path"] == "main.py" for m in result["matches"]))
        self.assertEqual(result["candidate_sha256"], digest(self.sandbox.files))
        self.assertFalse(result["executed_code"])
        literal = await self.adapter("run-one", "search_project", {"query": ".*"})
        self.assertEqual(literal["matches"], [{"path": "other.py", "line": 1, "text": "literal .*"}])
        unicode = await self.adapter("run-one", "search_project", {"query": "STRASSE"})
        self.assertEqual(unicode["matches"][0]["line"], 2)
        for args in ({"query": ""}, {"query": "x" * 201}, {"query": None}, {"query": "x", "path": "elsewhere"}):
            with self.subTest(arguments=args), self.assertRaises(ValueError):
                await self.adapter("run-one", "search_project", args)
        self.assertFalse(self.calls)

    async def test_run_application_never_reads_user_project_state(self):
        original = ProjectStore.application_state
        expected_database = self.runtime._run_dir("run-one") / "application-tools/scratch.sqlite3"

        def guarded(store, project_id):
            self.assertEqual(store.path, expected_database)
            self.assertNotEqual(project_id, self.user_id)
            return original(store, project_id)

        self.effects = [{"state": {"scratch.json": "WzFd"}}]
        with patch.object(ProjectStore, "application_state", new=guarded):
            result = await self.application()
        self.assertEqual(self.calls[0]["state"]["state"], {})
        self.assertEqual(result["data_scope"], "per_run_scratch_only")
        self.assertEqual(result["candidate_sha256"], digest(self.sandbox.files))
        self.assertFalse(result["test_claim"])
        self.assert_user_state_untouched()

    async def test_successive_calls_inherit_only_scratch_and_freeze_each_new_source(self):
        state1, state2 = {"counter": "MQ=="}, {"counter": "Mg=="}
        self.effects = [{"state": state1}, {"status": "failed", "state": {"poison": "e30="}}, {"state": state2}]
        source_snapshots = []
        for version in range(1, 4):
            self.sandbox.files = {"main.py": f"# source version {version}\n"}
            source_snapshots.append(deepcopy(self.sandbox.files))
            result = await self.application()
            self.assertEqual(result["candidate_sha256"], digest(source_snapshots[-1]))
            store, project_id = self.scratch()
            self.assertEqual(store.application_state(project_id)["state"], state1 if version < 3 else state2)
        self.assertEqual([call["state"]["state"] for call in self.calls], [{}, state1, state1])
        self.assertEqual([call["revision"]["files"] for call in self.calls], source_snapshots)
        self.assertEqual(len({call["project_id"] for call in self.calls}), 3)
        self.assertTrue(all(call["revision"]["number"] == 1 for call in self.calls))
        self.assert_user_state_untouched()

    async def test_separate_model_runs_never_share_scratch_even_for_same_user_project(self):
        self.effects = [{"state": {"run-one-only": "e30="}}, {"state": {"run-two-only": "W10="}}]
        await self.application("run-one")
        await self.application("run-two")
        self.assertEqual([call["state"]["state"] for call in self.calls], [{}, {}])
        for run_id, expected in (("run-one", {"run-one-only": "e30="}), ("run-two", {"run-two-only": "W10="})):
            store, project_id = self.scratch(run_id)
            self.assertEqual(store.application_state(project_id)["state"], expected)
        self.assert_user_state_untouched()

    async def test_timeout_or_cancellation_never_overwrites_previous_scratch(self):
        self.effects = [{"state": {"stable": "e30="}}, {"status": "blocked", "returned": {"timed_out": True}},
                        {"raise": asyncio.CancelledError()}]
        await self.application()
        with self.assertRaises(TimeoutError):
            await self.application()
        store, timeout_project = self.scratch()
        self.assertEqual(store.application_state(timeout_project)["state"], {"stable": "e30="})
        with self.assertRaises(asyncio.CancelledError):
            await self.application()
        store, after_cancel = self.scratch()
        self.assertEqual(after_cancel, timeout_project)
        self.assertEqual(store.application_state(after_cancel)["state"], {"stable": "e30="})
        self.assert_user_state_untouched()

    async def test_candidate_change_during_tool_execution_never_returns_stale_hash(self):
        self.effects = [{"mutate_files": {"main.py": "# external edit during fake execution\n"}}]
        with self.assertRaises(SandboxIntegrityError):
            await self.application()
        self.assertNotEqual(self.calls[0]["revision"]["source_sha256"], digest(self.sandbox.files))
        self.assert_user_state_untouched()

    async def test_runner_returned_source_or_candidate_hash_mismatch_is_not_relabelled(self):
        self.effects = [{"state": {"stable": "e30="}}]
        await self.application()
        store, stable_project = self.scratch()
        stable_state = store.application_state(stable_project)
        for key in ("source_sha256", "candidate_sha256"):
            with self.subTest(key=key):
                self.effects = [{"returned": {key: "0" * 64}}]
                call_count = len(self.calls)
                with self.assertRaises(SandboxIntegrityError):
                    await self.application()
                self.assertEqual(len(self.calls), call_count + 1)
                store, current_project = self.scratch()
                self.assertEqual(current_project, stable_project)
                self.assertEqual(store.application_state(current_project), stable_state)
                self.assertNotEqual(self.calls[-1]["project_id"], stable_project)
                self.assertTrue(store.executions(self.calls[-1]["project_id"]))
        self.assert_user_state_untouched()

    async def test_unknown_state_or_evidence_commit_stops_without_advancing_scratch(self):
        for key, run_id in (("state_commit_unknown", "run-one"),
                            ("evidence_commit_unknown", "run-two")):
            with self.subTest(unknown=key):
                self.effects = [{"state": {"stable": "e30="}}]
                await self.application(run_id)
                store, stable_project = self.scratch(run_id)
                stable_state = store.application_state(stable_project)
                self.effects = [{"status": "blocked", "returned": {key: True}}]
                call_count = len(self.calls)
                with self.assertRaises(SandboxIntegrityError):
                    await self.application(run_id)
                self.assertEqual(len(self.calls), call_count + 1, "Unknown completion must not be retried by the adapter")
                store, current_project = self.scratch(run_id)
                self.assertEqual(current_project, stable_project)
                self.assertEqual(store.application_state(current_project), stable_state)
                blocked_project = self.calls[-1]["project_id"]
                self.assertNotEqual(blocked_project, stable_project)
                self.assertEqual(store.application_state(blocked_project)["state"], stable_state["state"])
                records = store.executions(blocked_project)
                self.assertEqual(records[-1]["record"]["status"], "blocked")
                self.assertFalse(records[-1]["record"]["data_saved"])
        self.assert_user_state_untouched()

    async def test_first_unknown_commit_does_not_create_a_scratch_continuation_marker(self):
        for key, run_id in (("state_commit_unknown", "run-one"),
                            ("evidence_commit_unknown", "run-two")):
            with self.subTest(unknown=key):
                self.effects = [{"status": "blocked", "returned": {key: True}}]
                call_count = len(self.calls)
                with self.assertRaises(SandboxIntegrityError):
                    await self.application(run_id)
                self.assertEqual(len(self.calls), call_count + 1)
                base = self.runtime._run_dir(run_id) / "application-tools"
                self.assertFalse((base / "last-project.json").exists())
                store = ProjectStore(base / "scratch.sqlite3")
                self.assertTrue(store.executions(self.calls[-1]["project_id"]))
        self.assert_user_state_untouched()

    async def test_unknown_tools_nonproject_runs_and_invalid_application_args_are_rejected(self):
        self.runs["not-project"] = {"id": "not-project"}
        for run_id, tool, args in (("missing", "read_file", {"path": "main.py"}),
                                   ("not-project", "read_file", {"path": "main.py"}),
                                   ("run-one", "shell", {"command": "anything"}),
                                   ("run-one", "run_application", {"entrypoint": "main.py"}),
                                   ("run-one", "run_application", {"entrypoint": "../main.py", "args": []}),
                                   ("run-one", "run_application", {"entrypoint": "main.py", "args": ["\x00"]}),
                                   ("run-one", "run_application", {"entrypoint": "main.py", "args": ["x"] * 33}),
                                   ("run-one", "read_file", [])):
            with self.subTest(run_id=run_id, tool=tool), self.assertRaises(ValueError):
                await self.adapter(run_id, tool, args)
        self.assertFalse(self.calls)
        self.assert_user_state_untouched()


if __name__ == "__main__":
    unittest.main()
