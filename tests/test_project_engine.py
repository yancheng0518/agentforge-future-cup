"""Project-iteration unit simulations. No paid model or candidate execution."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.autonomous import AutonomousEngine, DecisionRejected
from agentforge.autonomous_model import (canonical_sha256, freeze_configuration, validate_intake,
                                         acceptance_parts, merge_acceptance)
from agentforge.autonomous_sandbox import validate_bundle
from agentforge.runtime import Runtime
from agentforge.storage import Store
from test_autonomous_engine import FakeModel, FakeSandbox, action, task


SOURCE = '''import argparse
def total(values):
    return sum(values)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("values", nargs="*", type=int)
    print(total(parser.parse_args().values))
if __name__ == "__main__":
    main()
'''


def suite(function="total", label="Collision"):
    return f'''import unittest
from bridge import call
class {label}(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(call("report", "{function}", [1, 2]), 3)
    def test_empty(self):
        self.assertEqual(call("report", "{function}", []), 0)
    def test_negative(self):
        self.assertEqual(call("report", "{function}", [-1, 2]), 1)
'''


def revision(files=None, acceptance="", number=1):
    files = {"report.py": SOURCE} if files is None else files
    return {"id": f"revision-{number}", "project_id": "project-unit", "number": number,
            "files": deepcopy(files), "source_sha256": canonical_sha256(files), "acceptance": acceptance,
            "metadata": {"origin": "unit-simulation"}}


def profile(enabled=True):
    content = {"name": "报告开发", "description": "报表模块与CLI实现", "prompt": "保留报表CLI并检查汇总行为。",
               "capabilities": ["reporting", "python"], "enabled": enabled}
    return {"id": "report-specialist", "version": 2, **content, "sha256": canonical_sha256(content)}


def document():
    content = {"title": "报表需求", "content": "报告CLI保留旧参数，汇总为total；新增stats接口。持久化文件只写/data。"}
    return {"id": "report-reference", "project_id": "project-unit", "version": 3, **content, "sha256": canonical_sha256(content)}


def proposed(function="stats"):
    return {"status": "ready", "summary": "在现有报表代码新增接口", "specification": f"在report.py添加{function}(values)，保留total和CLI。",
            "assumptions": ["CLI由独立交付检查验证"], "files": {}, "acceptance": suite(function)}


class ProjectSandbox(FakeSandbox):
    validate_bundle = staticmethod(validate_bundle)

    def __init__(self, statuses):
        super().__init__(statuses)
        self.bundles = {}
        self.force_count = None

    async def prepare(self, run_id, bundle):
        if run_id not in self.files:
            self.bundles[run_id] = deepcopy(bundle)
            await super().prepare(run_id, bundle)

    async def test(self, run_id):
        result = await super().test(run_id)
        result["test_count"] = (self.force_count if self.force_count is not None else
                                sum(p["count"] for p in acceptance_parts(self.bundles[run_id]["acceptance"])))
        return result


class ProjectModel(FakeModel):
    def __init__(self, proposal, decisions, outputs=None):
        super().__init__(decisions, outputs)
        self.proposal = proposal
        self.intake_run = None

    async def prepare(self, run, feedback=None):
        self.intake_run = deepcopy(run)
        return deepcopy(self.proposal)

    async def review_contract(self, run, value):
        return {"passed": True, "issues": [], "summary": "UNIT simulated contract review"}

    async def review_final(self, run, context):
        return {"passed": True, "issues": [], "summary": "UNIT simulated final review"}


def environment(tmp_path, proposal=None, decisions=None, outputs=None, statuses=None):
    runtime = Runtime(ROOT, tmp_path / "data")
    runtime._post = Mock(side_effect=AssertionError("No real model in unit tests"))
    runtime._command = Mock(side_effect=AssertionError("No candidate or Docker execution in unit tests"))
    store = Store(tmp_path / "state.sqlite3")
    model = ProjectModel(proposal or proposed(), decisions or [action("stop")], outputs)
    sandbox = ProjectSandbox(statuses or ["passed"])
    return AutonomousEngine(store, runtime, model, sandbox)


def test_project_revision_configuration_are_frozen_before_requests(tmp_path):
    engine = environment(tmp_path)
    base, profiles, knowledge = revision(), [profile()], [document()]
    run = engine.create_run_from_project("增加stats保留CLI", base, profiles=profiles, knowledge=knowledge)
    base["files"]["report.py"] = "changed"
    profiles[0]["prompt"] = "changed"
    knowledge[0]["content"] = "changed"
    saved = engine.store.run(run["id"])
    assert saved["project_context"]["files"]["report.py"] == SOURCE
    assert saved["profile_snapshot"][0]["prompt"] == profile()["prompt"]
    assert saved["knowledge_snapshot"][0]["content"] == document()["content"]
    assert saved["base_revision_id"] == "revision-1"
    assert saved["candidate_origin"] == "imported_baseline"
    assert saved["bundle"] is None
    engine.runtime._post.assert_not_called()


def test_project_intake_preserves_source_and_allows_existing_module_new_functions():
    assert validate_intake(proposed(), {"report.py": SOURCE})["files"] == {}
    replacement = proposed()
    replacement["files"] = {"report.py": "def stats(values):\n    raise NotImplementedError\n"}
    with pytest.raises(ValueError, match="cannot replace"):
        validate_intake(replacement, {"report.py": SOURCE})
    replacement["files"] = {"REPORT.py": "def stats(values):\n    raise NotImplementedError\n"}
    with pytest.raises(ValueError, match="cannot replace"):
        validate_intake(replacement, {"report.py": SOURCE})


@pytest.mark.parametrize("baseline_status", ["passed", "failed"])
def test_imported_baseline_may_pass_or_fail_without_natural_repair_claim(tmp_path, baseline_status):
    engine = environment(tmp_path, statuses=[baseline_status])
    run = engine.create_run_from_project("增加stats保留CLI", revision(acceptance=suite()))
    asyncio.run(engine.execute(run["id"]))
    saved = engine.store.run(run["id"])
    assert saved["status"] == "needs_human"  # Explicit scripted controller stop.
    assert saved["intake"]["status"] == "completed"
    assert saved["bundle"]["files"]["report.py"] == SOURCE
    assert saved["verifications"][0]["classification"] == "imported_baseline"
    assert saved["verifications"][0]["status"] == baseline_status
    assert saved["verifications"][0]["test_count"] == 6
    assert saved["task_graph"] == []


def test_two_project_iterations_keep_source_regressions_and_new_candidates(tmp_path):
    async def scenario():
        first_source = SOURCE + "\ndef stats(values):\n    return sum(values)\n"
        engine = environment(tmp_path, decisions=[action("dispatch", tasks=[task("stats", ["report.py"])]), action("verify"), action("finish")],
                             outputs={"stats": {"summary": "UNIT candidate", "files": {"report.py": first_source}}}, statuses=["failed", "passed"])
        first = engine.create_run_from_project("新增stats并保留CLI", revision(acceptance=suite()))
        await engine.execute(first["id"])
        saved = engine.store.run(first["id"])
        assert saved["status"] == "succeeded"
        assert [v["classification"] for v in saved["verifications"]] == ["imported_baseline", "natural_candidate"]
        next_revision = revision({"report.py": first_source}, saved["bundle"]["acceptance"], number=2)
        second_source = first_source + "\ndef metrics(values):\n    return sum(values)\n"
        engine.model = ProjectModel(proposed("metrics"), [action("dispatch", tasks=[task("metrics", ["report.py"])]), action("verify"), action("finish")],
                                    {"metrics": {"summary": "UNIT second candidate", "files": {"report.py": second_source}}})
        engine.sandbox = ProjectSandbox(["failed", "passed"])
        second = engine.create_run_from_project("新增metrics", next_revision)
        await engine.execute(second["id"])
        final = engine.store.run(second["id"])
        assert final["status"] == "succeeded"
        assert final["bundle"]["files"]["report.py"] == first_source
        assert final["acceptance_manifest"]["regression_test_count"] == 6
        assert final["acceptance_manifest"]["new_test_count"] == 3
        assert final["verifications"][-1]["test_count"] == 9
        assert len(acceptance_parts(final["bundle"]["acceptance"])) == 3
        assert all("_AGENTFORGE_SUITE_PARTS" not in p["source"] for p in acceptance_parts(final["bundle"]["acceptance"]))
    asyncio.run(scenario())


def test_loader_discovers_same_named_classes_in_all_revisions():
    # These are authored, trusted platform-test snippets, never candidate/model code.
    def trusted_suite(name):
        return "import unittest\nclass Collision(unittest.TestCase):\n    def test_same(self):\n        self.assertEqual(" + repr(name) + ", " + repr(name) + ")\n"
    once, _ = merge_acceptance(trusted_suite("old"), trusted_suite("new"))
    twice, manifest = merge_acceptance(once, trusted_suite("next"))
    module = types.ModuleType("unit_test_host_composition_only")
    exec(compile(twice, "<trusted-unit-suite-composition>", "exec"), module.__dict__)
    collected = unittest.defaultTestLoader.loadTestsFromModule(module)
    assert collected.countTestCases() == manifest["total_test_count"] == 3
    result = unittest.TestResult()
    collected.run(result)
    assert result.testsRun == 3 and result.wasSuccessful()


def test_omitted_regression_tests_cannot_pass_imported_baseline(tmp_path):
    engine = environment(tmp_path)
    engine.sandbox.force_count = 3  # Simulated runner omitted the three old tests.
    run = engine.create_run_from_project("新增stats", revision(acceptance=suite()))
    asyncio.run(engine.execute(run["id"]))
    saved = engine.store.run(run["id"])
    assert saved["status"] == "needs_human"
    assert saved["verifications"][0]["discovery_mismatch"] is True
    assert saved["verified_hash"] is None and saved["controller_decisions"] == []


def test_unknown_disabled_profile_rejected_and_legacy_task_allowed(tmp_path):
    engine = environment(tmp_path)
    run = engine.create_run_from_project("task", revision(), profiles=[profile(False)])
    for id in ("missing", "report-specialist"):
        with pytest.raises(DecisionRejected, match="Unknown or disabled"):
            engine._dispatch(run, action("dispatch", tasks=[{**task("work"), "profile_id": id}]))
    engine._dispatch(run, action("dispatch", tasks=[task("legacy-five-fields")]))
    assert engine.store.run(run["id"])["task_graph"][0]["id"] == "legacy-five-fields"


def test_publication_callback_precedes_terminal_export_and_failure_preserves_candidate(tmp_path):
    async def scenario():
        engine = environment(tmp_path)
        run = engine.create_run_from_project("task", revision())
        async def publish(run_id):
            assert not any(e["type"] == "terminal" for e in engine.store.events(run_id))
            assert await engine.sandbox.read(run_id) == {"report.py": SOURCE}
            raise ValueError("UNIT publication conflict")
        engine.completion_callback = publish
        await engine.execute(run["id"])
        evidence = json.loads((engine.runtime._run_dir(run["id"]) / "evidence.json").read_text(encoding="utf-8"))
        assert evidence["run"]["publication"]["status"] == "failed"
        assert evidence["run"]["publication"]["candidate_preserved"] is True
        kinds = [e["type"] for e in evidence["events"]]
        assert kinds.index("publication_failed") < kinds.index("terminal")
        assert await engine.sandbox.read(run["id"]) == {"report.py": SOURCE}
    asyncio.run(scenario())


def test_invalid_revision_or_configuration_does_not_create_run(tmp_path):
    engine = environment(tmp_path)
    broken = revision()
    broken["files"]["report.py"] = "modified after hash"
    with pytest.raises(ValueError, match="revision hash"):
        engine.create_run_from_project("task", broken)
    broken_profile = profile()
    broken_profile["prompt"] = "tampered"
    with pytest.raises(ValueError, match="profile content"):
        engine.create_run_from_project("task", revision(), profiles=[broken_profile])
    assert engine.store.runs() == []
