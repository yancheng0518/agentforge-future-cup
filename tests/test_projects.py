"""Immutable project store tests. Source text is never imported or executed."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.autonomous_sandbox import _digest, _json, _sources
from agentforge.projects import ProjectConflictError, ProjectStore, ProjectValidationError


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentforge-project-unit-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "projects.sqlite3"
        self.store = ProjectStore(self.path)
        self.files = {"pkg/__init__.py": "", "pkg/main.py": "# original source text\n"}
        self.acceptance = "# independent test source text, never executed by storage\n"

    def project(self, name="project"):
        return self.store.import_project(name, self.files, self.acceptance)

    def verification(self, files=None, acceptance=None):
        files = self.files if files is None else files
        acceptance = self.acceptance if acceptance is None else acceptance
        fingerprint = _digest(_json(_sources(files)))
        return {"status": "passed", "candidate_sha256": fingerprint, "verified_hash": fingerprint,
                "test": {"status": "passed", "isolated": True, "exit_code": 0, "test_count": 3,
                         "candidate_sha256": fingerprint, "acceptance_sha256": _digest(acceptance),
                         "stdout": "unit evidence only", "container_invoked": True},
                "review": {"passed": True, "candidate_hash": fingerprint, "summary": "unit evidence only"}}

    def promote(self, imported, run_id="run-one", files=None):
        files = self.files if files is None else files
        return self.store.promote(imported["project"]["id"], imported["revision"]["id"], run_id,
                                  files, self.acceptance, self.verification(files))

    def test_import_is_unverified_text_only_and_uses_exact_sandbox_fingerprint(self):
        sentinel = Path(self.temp.name) / "candidate_executed"
        source = f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
        files = {"z.py": source, "pkg/b.py": "# 中文\r\n", "a.py": "print('not executed')\n"}
        with patch("subprocess.Popen", side_effect=AssertionError("Storage must not start processes")):
            imported = self.store.import_project("  Unicode 项目  ", files)
        project, revision = imported["project"], imported["revision"]
        self.assertFalse(sentinel.exists())
        self.assertEqual(project["name"], "Unicode 项目")
        self.assertEqual(project["head_revision_id"], revision["id"])
        self.assertEqual(revision["number"], 1)
        self.assertIsNone(revision["parent_id"])
        self.assertIsNone(revision["run_id"])
        self.assertEqual(revision["verification"], {"status": "unverified"})
        self.assertEqual(revision["acceptance"], "")
        self.assertEqual(revision["source_sha256"], _digest(_json(_sources(files))))
        self.assertEqual(self.store.list_projects(), [project])
        self.assertEqual(ProjectStore(self.path).revision(project["id"], revision["id"]), revision)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        files["z.py"] = "caller change"
        revision["files"]["a.py"] = "return-value change"
        saved = self.store.revision(project["id"], revision["id"])
        self.assertEqual(saved["files"]["z.py"], source)
        self.assertNotEqual(saved["files"]["a.py"], "return-value change")

    def test_rejects_external_paths_case_collisions_and_file_bounds(self):
        invalid = ["https://github.com/example/repo", Path("C:/server/project"), {}, [],
                   {"../x.py": "x"}, {"/x.py": "x"}, {"C:/x.py": "x"}, {"a\\x.py": "x"},
                   {"https://server/x.py": "x"}, {"a.txt": "x"}, {"CON.py": "x"}, {"pkg/COM1.py": "x"},
                   {"a.py": "x", "A.py": "y"}, {"pkg/a.py": "x", "Pkg/b.py": "y"},
                   {"a.py": "\x00"}, {"a.py": "\ud800"}, {"a.py": b"bytes"},
                   {f"a{i}.py": "" for i in range(33)}, {"a.py": "x" * 65537},
                   {f"a{i}.py": "x" * 65536 for i in range(5)},
                   {"/".join(["a"] * 8) + "/x.py": "x"}]
        for files in invalid:
            with self.subTest(files=str(files)[:90]), self.assertRaises(ProjectValidationError):
                self.store.import_project("invalid", files)
        self.assertEqual(self.store.list_projects(), [])
        accepted = self.store.import_project("limit", {f"a{i}.py": "x" * 65536 for i in range(4)})
        self.assertEqual(len(accepted["revision"]["files"]), 4)

    def test_names_and_acceptance_are_bounded_without_execution(self):
        for name in ("", "   ", "x" * 121, None, "a\x00b"):
            with self.subTest(name=name), self.assertRaises(ProjectValidationError):
                self.store.import_project(name, self.files)
        for acceptance in (None, "\x00", "x" * 65537, "\ud800"):
            with self.subTest(acceptance=str(acceptance)[:20]), self.assertRaises(ProjectValidationError):
                self.store.import_project("project", self.files, acceptance)
        self.assertEqual(self.store.import_project("project", self.files, "x" * 65536)["revision"]["verification"]["status"], "unverified")

    def test_promotion_is_append_only_and_old_versions_remain_unchanged(self):
        initial = self.project()
        original = deepcopy(initial["revision"])
        changed = {"pkg/__init__.py": "", "pkg/main.py": "# next source\n"}
        promoted = self.promote(initial, files=changed)
        self.assertEqual(promoted["revision"]["number"], 2)
        self.assertEqual(promoted["revision"]["parent_id"], original["id"])
        self.assertEqual(promoted["revision"]["run_id"], "run-one")
        self.assertFalse(promoted["idempotent"])
        self.assertEqual(self.store.revision(initial["project"]["id"], original["id"]), original)
        self.assertEqual(self.store.get_project(initial["project"]["id"])["head_revision_id"], promoted["revision"]["id"])
        self.assertEqual([r["number"] for r in self.store.revisions(initial["project"]["id"])], [1, 2])
        changed["pkg/main.py"] = "caller mutated"
        promoted["revision"]["verification"]["review"]["passed"] = False
        saved = self.store.revision(initial["project"]["id"], promoted["revision"]["id"])
        self.assertTrue(saved["verification"]["review"]["passed"])
        self.assertEqual(saved["files"]["pkg/main.py"], "# next source\n")

    def test_promotion_rejects_failed_forged_or_stale_verification(self):
        initial = self.project()
        mutations = [("status", "failed"), ("candidate_sha256", "0" * 64), ("verified_hash", None),
                     ("test.status", "failed"), ("test.isolated", False), ("test.exit_code", False),
                     ("test.exit_code", 1), ("test.test_count", 0), ("test.test_count", True),
                     ("test.candidate_sha256", "0" * 64), ("test.acceptance_sha256", "0" * 64),
                     ("review.passed", 1), ("review.passed", False), ("review.candidate_hash", "0" * 64),
                     ("test", None), ("review", None)]
        for path, value in mutations:
            with self.subTest(path=path, value=value):
                verification = self.verification()
                parts = path.split(".")
                target = verification if len(parts) == 1 else verification[parts[0]]
                target[parts[-1]] = value
                with self.assertRaises(ProjectValidationError):
                    self.store.promote(initial["project"]["id"], initial["revision"]["id"], "run", self.files, self.acceptance, verification)
        with self.assertRaises(ProjectValidationError):
            self.store.promote(initial["project"]["id"], initial["revision"]["id"], "run", {"x.py": "new"}, self.acceptance, self.verification())
        self.assertEqual(self.store.revisions(initial["project"]["id"]), [initial["revision"]])
        self.assertEqual(self.store.get_project(initial["project"]["id"]), initial["project"])

    def test_stale_and_cross_project_bases_cannot_advance_head(self):
        first, other = self.project("first"), self.project("other")
        promoted = self.promote(first)
        with self.assertRaises(ProjectConflictError):
            self.promote(first, "later-run")
        with self.assertRaises(KeyError):
            self.store.promote(other["project"]["id"], first["revision"]["id"], "cross-run", self.files, self.acceptance, self.verification())
        self.assertIsNone(self.store.revision(other["project"]["id"], promoted["revision"]["id"]))
        self.assertEqual(self.store.get_project(other["project"]["id"]), other["project"])

    def test_duplicate_run_is_idempotent_and_conflicting_reuse_is_rejected(self):
        initial = self.project()
        first = self.promote(initial)
        replay = self.promote(initial)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["revision"], first["revision"])
        later = self.promote(first, "run-two")
        old_replay = self.promote(initial)
        self.assertTrue(old_replay["idempotent"])
        self.assertEqual(old_replay["revision"], first["revision"])
        self.assertEqual(old_replay["project"]["head_revision_id"], later["revision"]["id"])
        with self.assertRaises(ProjectConflictError):
            self.promote(initial, files={"different.py": "# different\n"})
        with self.assertRaises(ProjectConflictError):
            self.promote(self.project("other"))
        self.assertEqual(len(self.store.revisions(initial["project"]["id"])), 3)

    def test_concurrent_head_cas_allows_exactly_one_distinct_run(self):
        initial = self.project()
        stores = [ProjectStore(self.path), ProjectStore(self.path)]
        barrier = threading.Barrier(2)

        def submit(index):
            barrier.wait(timeout=5)
            try:
                return stores[index].promote(initial["project"]["id"], initial["revision"]["id"], f"parallel-{index}", self.files, self.acceptance, self.verification())
            except ProjectConflictError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, ProjectConflictError) for result in results), 1)
        self.assertEqual(len(self.store.revisions(initial["project"]["id"])), 2)

    def test_concurrent_duplicate_run_creates_one_revision(self):
        initial = self.project()
        stores = [ProjectStore(self.path), ProjectStore(self.path)]
        barrier = threading.Barrier(2)

        def submit(index):
            barrier.wait(timeout=5)
            return stores[index].promote(initial["project"]["id"], initial["revision"]["id"], "same-run", self.files, self.acceptance, self.verification())

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))
        self.assertEqual({r["revision"]["id"] for r in results}, {results[0]["revision"]["id"]})
        self.assertEqual(sorted(r["idempotent"] for r in results), [False, True])
        self.assertEqual(len(self.store.revisions(initial["project"]["id"])), 2)

    def test_failed_head_commit_rolls_back_inserted_revision(self):
        initial = self.project()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_commit BEFORE UPDATE ON projects BEGIN SELECT RAISE(ABORT,'simulated head failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.promote(initial)
        self.assertEqual(self.store.revisions(initial["project"]["id"]), [initial["revision"]])
        self.assertEqual(self.store.get_project(initial["project"]["id"]), initial["project"])

    def test_free_profiles_append_versions_and_only_inherit_model_configuration(self):
        defaults = self.store.list_profiles()
        self.assertEqual(len(defaults), 1)
        body = {"name": "领域专家", "description": "自由角色", "prompt": "关注组合约束", "capabilities": ["业务建模", "Python"], "enabled": True}
        first = self.store.save_profile(body)
        updated = self.store.save_profile({**body, "prompt": "新提示词", "enabled": False}, first["id"])
        self.assertEqual(updated["version"], 2)
        self.assertEqual(self.store.profile(first["id"], 1), first)
        self.assertEqual(self.store.profile(first["id"]), updated)
        self.assertEqual(first["sha256"], _digest(_json(body)))
        self.assertFalse(next(p for p in self.store.list_profiles() if p["id"] == first["id"])["enabled"])
        first["prompt"] = "caller mutation"
        self.assertEqual(self.store.profile(first["id"], 1)["prompt"], body["prompt"])
        for extra in ({"model": "other-model"}, {"tools": ["shell"]}, {"id": "forged"}, {"enabled": 1}, {"capabilities": ["Python", "python"]}):
            with self.subTest(extra=extra), self.assertRaises(ProjectValidationError):
                self.store.save_profile({**body, **extra})
        with self.assertRaises(KeyError):
            self.store.save_profile(body, "missing-profile")
        self.assertIsNone(self.store.profile(first["id"], 999))

    def test_knowledge_versions_are_immutable_and_scoped_to_project(self):
        first, other = self.project("one"), self.project("two")
        project_id = first["project"]["id"]
        doc1 = self.store.save_knowledge(project_id, "  约束  ", "旧内容")
        doc2 = self.store.save_knowledge(project_id, "约束", "新内容", doc1["id"])
        self.assertEqual(self.store.knowledge(project_id), [doc2])
        self.assertEqual(self.store.get_knowledge(project_id, doc1["id"], 1), doc1)
        self.assertEqual(doc1["sha256"], _digest(_json({"title": "约束", "content": "旧内容"})))
        self.assertEqual(doc1["content_sha256"], _digest("旧内容"))
        selected = self.store.select_knowledge(project_id, [{"id": doc1["id"], "version": 1}])
        self.assertEqual(selected, [doc1])
        selected[0]["content"] = "caller mutation"
        self.assertEqual(self.store.get_knowledge(project_id, doc1["id"], 1), doc1)
        self.assertEqual(self.store.knowledge(other["project"]["id"]), [])
        self.assertIsNone(self.store.get_knowledge(other["project"]["id"], doc1["id"], 1))
        with self.assertRaises(KeyError):
            self.store.save_knowledge(other["project"]["id"], "forged", "content", doc1["id"])
        with self.assertRaises(KeyError):
            self.store.select_knowledge(other["project"]["id"], [{"id": doc1["id"], "version": 1}])
        with self.assertRaises(KeyError):
            self.store.select_knowledge(project_id, [{"id": doc1["id"], "version": 99}])

    def test_knowledge_limits_use_utf8_bytes_and_selected_frozen_versions(self):
        project_id = self.project()["project"]["id"]
        for content in ("x" * 65537, "中" * 21846, "", "\x00", None):
            with self.subTest(content=str(content)[:20]), self.assertRaises(ProjectValidationError):
                self.store.save_knowledge(project_id, "invalid", content)
        documents = [self.store.save_knowledge(project_id, f"doc-{i}", "x" * 65536) for i in range(3)]
        self.assertEqual(len(self.store.knowledge(project_id)), 3, "Repository size is distinct from selected context size")
        selectors = [{"id": d["id"], "version": 1} for d in documents]
        self.assertEqual(len(self.store.select_knowledge(project_id, selectors[:2])), 2)
        with self.assertRaises(ProjectValidationError):
            self.store.select_knowledge(project_id, selectors)
        for selection in ([selectors[0], selectors[0]], [{"id": documents[0]["id"], "version": True}], [{"id": documents[0]["id"]}]):
            with self.subTest(selection=selection), self.assertRaises(ProjectValidationError):
                self.store.select_knowledge(project_id, selection)

    def test_execution_records_are_append_only_detached_and_cross_project_isolated(self):
        first, other = self.project("one"), self.project("two")
        project_id = first["project"]["id"]
        evidence = {"run_id": "run", "result": {"status": "failed", "stdout": "unit fixture"}}
        first_record = self.store.save_execution(project_id, evidence)
        second_record = self.store.save_execution(project_id, {**evidence, "result": {"status": "passed"}})
        self.assertNotEqual(first_record["id"], second_record["id"])
        evidence["result"]["status"] = "changed"
        first_record["record"]["result"]["status"] = "also changed"
        saved = self.store.executions(project_id)
        self.assertEqual([e["record"]["result"]["status"] for e in saved], ["failed", "passed"])
        self.assertEqual(self.store.executions(other["project"]["id"]), [])
        with self.assertRaises(KeyError):
            self.store.save_execution("missing-project", {})

    def test_non_json_unbounded_and_recursive_evidence_is_rejected(self):
        project_id = self.project()["project"]["id"]
        cycle = {}
        cycle["cycle"] = cycle
        for record in ([], {1: "non-string key"}, {"n": float("nan")}, {"n": float("inf")}, {"bytes": b"no"},
                       {"tuple": (1, 2)}, {"text": "x" * (2 * 1024 * 1024)}, cycle):
            with self.subTest(kind=type(record).__name__), self.assertRaises(ProjectValidationError):
                self.store.save_execution(project_id, record)
        self.assertEqual(self.store.executions(project_id), [])

    def test_application_state_and_execution_commit_together_and_are_detached(self):
        project_id = self.project()["project"]["id"]
        empty = self.store.application_state(project_id)
        self.assertEqual(empty, {"state": {}, "sha256": _digest(_json({})), "version": 0})
        state = {"items.json": "WzFd"}
        record = {"status": "succeeded", "data_saved": True, "data_before_sha256": empty["sha256"],
                  "data_after_sha256": _digest(_json(state))}
        saved = self.store.commit_execution(project_id, record, state, empty["sha256"])
        self.assertEqual(self.store.executions(project_id), [saved])
        self.assertEqual(self.store.application_state(project_id), {"state": state, "sha256": _digest(_json(state)), "version": 1})
        state["items.json"] = "caller mutation"
        record["status"] = "caller mutation"
        saved["record"]["status"] = "return value mutation"
        current = ProjectStore(self.path).application_state(project_id)
        self.assertEqual(current["state"], {"items.json": "WzFd"})
        current["state"]["items.json"] = "read value mutation"
        self.assertEqual(self.store.application_state(project_id)["state"], {"items.json": "WzFd"})
        self.assertEqual(self.store.executions(project_id)[0]["record"]["status"], "succeeded")

    def test_evidence_only_execution_never_changes_application_state(self):
        project_id = self.project()["project"]["id"]
        before = self.store.application_state(project_id)
        self.store.save_execution(project_id, {"status": "failed"})
        self.assertEqual(self.store.application_state(project_id), before)
        self.store.commit_execution(project_id, {"status": "succeeded"}, {"saved": "YQ=="}, before["sha256"])
        stable = self.store.application_state(project_id)
        self.store.commit_execution(project_id, {"status": "blocked", "completion_unknown": True},
                                    expected_state_sha256="ignored-without-state")
        self.assertEqual(self.store.application_state(project_id), stable)
        self.assertEqual(len(self.store.executions(project_id)), 3)

    def test_application_state_cas_and_project_ownership_prevent_stale_overwrite(self):
        first, other = self.project("one")["project"]["id"], self.project("two")["project"]["id"]
        original = self.store.application_state(first)
        self.store.commit_execution(first, {"id": "first"}, {"a": "YQ=="}, original["sha256"])
        stable = self.store.application_state(first)
        with self.assertRaises(ProjectConflictError):
            self.store.commit_execution(first, {"id": "stale"}, {"b": "Yg=="}, original["sha256"])
        self.assertEqual(self.store.application_state(first), stable)
        self.assertEqual(len(self.store.executions(first)), 1)
        self.assertEqual(self.store.application_state(other), original)
        self.assertEqual(self.store.executions(other), [])
        with self.assertRaises(ProjectConflictError):
            self.store.commit_execution(other, {}, {"b": "Yg=="}, stable["sha256"])
        with self.assertRaises(KeyError):
            self.store.application_state("missing-project")
        with self.assertRaises(KeyError):
            self.store.commit_execution("missing-project", {}, {}, original["sha256"])

    def test_failed_execution_insert_rolls_back_both_initial_and_updated_state(self):
        first, second = self.project("empty")["project"]["id"], self.project("existing")["project"]["id"]
        empty = self.store.application_state(second)
        self.store.commit_execution(second, {"id": "original"}, {"old": "e30="}, empty["sha256"])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_execution BEFORE INSERT ON executions BEGIN SELECT RAISE(ABORT,'simulated evidence failure'); END")
        for project_id in (first, second):
            before = self.store.application_state(project_id)
            records = self.store.executions(project_id)
            with self.subTest(project_id=project_id), self.assertRaises(sqlite3.IntegrityError):
                self.store.commit_execution(project_id, {"id": "not-committed"}, {"new": "WzJd"}, before["sha256"])
            self.assertEqual(self.store.application_state(project_id), before)
            self.assertEqual(self.store.executions(project_id), records)

    def test_application_state_concurrent_cas_commits_exactly_one_record(self):
        project_id = self.project()["project"]["id"]
        initial = self.store.application_state(project_id)
        stores = [ProjectStore(self.path), ProjectStore(self.path)]
        barrier = threading.Barrier(2)

        def submit(index):
            barrier.wait(timeout=5)
            try:
                return stores[index].commit_execution(project_id, {"index": index}, {str(index): "e30="}, initial["sha256"])
            except ProjectConflictError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))
        winners = [result for result in results if isinstance(result, dict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(isinstance(result, ProjectConflictError) for result in results), 1)
        self.assertEqual(self.store.executions(project_id), winners)
        current = self.store.application_state(project_id)
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["state"], {str(winners[0]["record"]["index"]): "e30="})

    def test_application_state_requires_bounded_json_and_explicit_hash_for_commit(self):
        project_id = self.project()["project"]["id"]
        before = self.store.application_state(project_id)
        for state in ([], {"large": "x" * 131072}, {"invalid": float("nan")}, {1: "invalid"}):
            with self.subTest(state_type=type(state).__name__), self.assertRaises(ProjectValidationError):
                self.store.commit_execution(project_id, {}, state, before["sha256"])
        for expected in (None, "", "x" * 64, True):
            with self.subTest(expected=expected), self.assertRaises(ProjectValidationError):
                self.store.commit_execution(project_id, {}, {}, expected)
        self.assertEqual(self.store.application_state(project_id), before)
        self.assertEqual(self.store.executions(project_id), [])

    def test_sqlite_rejects_mutation_of_immutable_tables(self):
        imported = self.project()
        project_id = imported["project"]["id"]
        self.store.save_knowledge(project_id, "title", "content")
        self.store.save_execution(project_id, {"status": "unit"})
        for table in ("revisions", "profiles", "knowledge_versions", "executions"):
            for action in (f"UPDATE {table} SET body='{{}}'", f"DELETE FROM {table}"):
                with self.subTest(table=table, action=action), closing(sqlite3.connect(self.path)) as db, db, self.assertRaises(sqlite3.IntegrityError):
                    db.execute(action)
        self.assertEqual(self.store.revision(project_id, imported["revision"]["id"]), imported["revision"])


if __name__ == "__main__":
    unittest.main()
