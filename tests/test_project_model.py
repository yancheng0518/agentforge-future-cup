"""Real adapter payload/evidence tests with fake HTTP; no live model or code execution."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from test_project_engine import environment, revision, profile, document, proposed, SOURCE
from agentforge.autonomous_model import (AutonomousModel, retrieve_frozen_knowledge, canonical_sha256,
                                         validate_decision, validate_intake, ModelProtocolError)
from agentforge.runtime import RuntimeErrorSafe


def response(output):
    return {"choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 20}}


def setup_adapter(tmp_path, profiles=None, knowledge=None):
    engine = environment(tmp_path)
    engine.runtime.settings.update(api_key="UNIT-INVALID-SECRET", model="unit-only-model", base_url="https://example.invalid/v1", min_request_interval=0)
    engine.model = AutonomousModel(engine.runtime, engine.store)
    run = engine.create_run_from_project("报表CLI新增stats，持久化到/data", revision(),
                                          budget={"max_tokens": 300000}, profiles=profiles or [profile()], knowledge=knowledge or [document()])
    return engine, run


def evidence(engine, run):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((engine.runtime._run_dir(run["id"]) / "model_requests").glob("*.json"))]


def sent_context(engine):
    return json.loads(engine.runtime._post.call_args.args[0]["messages"][1]["content"])["context"]


def test_project_intake_receives_real_imported_cli_source_and_frozen_references(tmp_path):
    async def scenario():
        engine, run = setup_adapter(tmp_path)
        engine.runtime._post = Mock(return_value=response(proposed()))
        result = await engine.model.prepare(run)
        assert result["files"] == {}
        context = sent_context(engine)
        assert context["project"]["report.py"] == SOURCE
        assert context["project_revision"]["id"] == "revision-1"
        assert context["configuration"]["profiles"][0]["sha256"] == profile()["sha256"]
        assert context["configuration"]["knowledge"][0]["sha256"] == document()["sha256"]
        assert "todo" not in json.dumps(context).lower()
        prompt = engine.runtime._post.call_args.args[0]["messages"][0]["content"]
        assert "Never return replacement content" in prompt
        assert "CLI execution and /data persistence require separate" in prompt
        assert "The baseline scaffold MUST fail" not in prompt
        assert evidence(engine, run)[0]["knowledge_references"][0]["id"] == document()["id"]
        engine.runtime._command.assert_not_called()
    asyncio.run(scenario())


def test_profile_is_in_actual_worker_payload_and_each_http_request_evidence(tmp_path):
    async def scenario():
        configured = profile()
        engine, run = setup_adapter(tmp_path, profiles=[configured])
        configured["prompt"] = "AFTER-FREEZE-MUTATION"
        assigned = {"id": "stats", "title": "报表汇总", "instructions": "保留报表CLI，实现stats", "depends_on": [],
                    "write_paths": ["report.py"], "profile_id": "report-specialist"}
        engine.runtime._post = Mock(side_effect=[response("invalid worker object"), response({"summary": "UNIT worker", "files": {}})])
        await engine.model.work(run, assigned, {"project": {"report.py": SOURCE}})
        context = sent_context(engine)
        actual = context["configuration"]["selected_profile"]
        assert actual["prompt"] == profile()["prompt"]
        assert actual["description"] == profile()["description"]
        assert "AFTER-FREEZE-MUTATION" not in json.dumps(context)
        records = evidence(engine, run)
        assert len(records) == 2  # Explicit format retry, both carry same identity.
        for record in records:
            assert record["profile"] == {k: profile()[k] for k in ("id", "version", "sha256")}
            assert record["profile_model_policy"] == "inherits_run_model"
            assert record["request"]["model"] == "unit-only-model"
            assert record["configuration_sha256"] == run["configuration_sha256"]
            assert record["knowledge_references"][0]["sha256"] == document()["sha256"]
        events = [e for e in engine.store.events(run["id"]) if e["type"] == "knowledge_retrieved"]
        assert events[0]["node_id"] == "stats" and events[0]["data"]["matches"][0]["excerpt"]
    asyncio.run(scenario())


def test_main_receives_catalog_and_can_choose_optional_profile(tmp_path):
    async def scenario():
        engine, run = setup_adapter(tmp_path)
        decision = {"action": "dispatch", "summary": "use reporting profile", "tasks": [{"id": "work", "title": "report", "instructions": "report", "depends_on": [],
                    "write_paths": ["report.py"], "profile_id": "report-specialist"}]}
        engine.runtime._post = Mock(return_value=response(decision))
        returned = await engine.model.decide(run, {"project": {"report.py": SOURCE}})
        assert returned == decision
        context = sent_context(engine)
        assert context["configuration"]["profiles"][0]["id"] == "report-specialist"
        engine._dispatch(run, returned)
        assigned = engine.store.run(run["id"])["task_graph"][0]
        assert assigned["profile_version"] == profile()["version"]
        assert assigned["profile_sha256"] == profile()["sha256"]
        assert evidence(engine, run)[0]["profile"] is None  # Controller itself inherits run model.
    asyncio.run(scenario())


def test_unknown_profile_stops_before_any_model_request(tmp_path):
    async def scenario():
        engine, run = setup_adapter(tmp_path)
        assigned = {"id": "work", "instructions": "report", "profile_id": "not-frozen"}
        with pytest.raises(RuntimeErrorSafe, match="Unknown or disabled"):
            await engine.model.work(run, assigned, {})
        engine.runtime._post.assert_not_called()
    asyncio.run(scenario())


def test_retrieval_is_bounded_relevant_and_uses_frozen_hashes():
    docs = []
    for index in range(8):
        content = {"title": "报表参考 " + str(index), "content": "noise " * 900 + "报表CLI必需保留\n" + "details " * 900}
        docs.append({"id": f"ref-{index}", "version": 1, **content, "sha256": canonical_sha256(content)})
    matches = retrieve_frozen_knowledge(docs, "报表CLI")
    assert len(matches) == 4
    assert all(len(m["excerpt"]) <= 2400 and "CLI" in m["excerpt"] for m in matches)
    assert all(m["sha256"] == docs[int(m["id"].split("-")[-1])]["sha256"] for m in matches)
    assert retrieve_frozen_knowledge(docs, "unrelatedword") == []


def test_project_intake_limits_new_tests_to_twelve_and_does_not_implement_new_module():
    value = proposed()
    value["acceptance"] += "\nclass More(unittest.TestCase):\n" + "".join(
        f"    def test_{i}(self):\n        self.assertEqual(call('report','stats',[1]),1)\n" for i in range(10))
    with pytest.raises(ValueError, match="3 to 12"):
        validate_intake(value, {"report.py": SOURCE})
    value = proposed()
    value["files"] = {"extra.py": "def fn(x):\n    return x\n"}
    with pytest.raises(ValueError, match="NotImplementedError"):
        validate_intake(value, {"report.py": SOURCE})
