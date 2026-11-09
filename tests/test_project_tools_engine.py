"""Project direct-action regressions; fake tools/models, never candidate execution."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from test_project_engine import environment, revision, SOURCE
from test_project_model import setup_adapter, response, sent_context
from test_autonomous_engine import make_engine, action, task
from agentforge.autonomous import DecisionRejected
from agentforge.autonomous_model import validate_decision, validate_project_tool
from agentforge.autonomous_sandbox import SandboxIntegrityError
from agentforge.runtime import RuntimeErrorSafe


def edit(source=SOURCE + "\n# UNIT direct change\n"):
    return {"action": "edit", "summary": "UNIT direct main edit", "files": {"report.py": source}}


def tool(name="read_file", arguments=None):
    return {"action": "tool", "summary": "UNIT observation", "tool": name,
            "arguments": {"path": "report.py"} if arguments is None else arguments}


async def prepared(engine):
    run = engine.create_run_from_project("报表CLI新增stats", revision())
    engine.store.update_run(run["id"], status="running")
    assert await engine._prepare_requirement(run["id"])
    return engine.store.run(run["id"])


def test_model_read_file_schema_accepts_bounded_followup_pages():
    args = {'path':'report.py', 'start_line':401, 'end_line':800}
    assert validate_project_tool('read_file', args) == args
    assert validate_project_tool('read_file', {'path':'report.py','start_line':401})['start_line'] == 401
    for start, end in [(0,1),(1,401),(3,2),(True,4),(1,False)]:
        with pytest.raises(ValueError):
            validate_project_tool('read_file', {'path':'report.py','start_line':start,'end_line':end})


def test_main_direct_edit_invalidates_finish_and_preserves_history(tmp_path):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        old_hash = run["verified_hash"]
        assert old_hash
        decision = edit()
        await engine._action(run["id"], decision)
        decision["files"]["report.py"] = "caller mutated its dictionary"
        saved = engine.store.run(run["id"])
        assert saved["candidate_origin"] == "natural_candidate" and saved["verified_hash"] is None
        assert saved["task_graph"] == []
        assert saved["bundle"]["files"]["report.py"] == SOURCE
        assert (await engine.sandbox.read(run["id"]))["report.py"].endswith("# UNIT direct change\n")
        event = next(e for e in engine.store.events(run["id"]) if e["type"] == "main_edit")
        assert event["data"]["before_sha256"] == old_hash
        assert event["data"]["after_sha256"] != old_hash
        assert event["data"]["changed_paths"] == ["report.py"]
        with pytest.raises(DecisionRejected, match="independent Docker"):
            await engine._action(run["id"], action("finish"))
    asyncio.run(scenario())


@pytest.mark.parametrize("files", [
    {"../outside.py": ""}, {"/tmp/outside.py": ""}, {"pkg\\outside.py": ""},
    {"report.py": "x", "REPORT.py": "y"}, {"CON.py": "x"}, {},
])
def test_direct_edit_rejects_path_escape_aliases_and_empty_edits(tmp_path, files):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        with pytest.raises(DecisionRejected):
            await engine._action(run["id"], {"action": "edit", "summary": "bad", "files": files})
        assert await engine.sandbox.read(run["id"]) == {"report.py": SOURCE}
    asyncio.run(scenario())


def test_existing_path_cannot_be_reintroduced_with_case_alias(tmp_path):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        with pytest.raises(DecisionRejected, match="大小写"):
            await engine._action(run["id"], {"action": "edit", "summary": "bad alias", "files": {"REPORT.py": "changed"}})
        assert engine.sandbox.applied == []
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["pending", "running"])
def test_direct_edit_waits_for_delegated_tasks(tmp_path, status):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        engine.store.update_run(run["id"], task_graph=[{**task("unfinished", ["other.py"]), "status": status}])
        with pytest.raises(DecisionRejected, match="active delegated"):
            await engine._action(run["id"], edit())
        assert engine.sandbox.applied == []
    asyncio.run(scenario())


def test_legacy_runs_cannot_enable_direct_actions_even_with_tool_callback(tmp_path):
    async def scenario():
        engine, run, _, _ = make_engine(tmp_path, [])
        called = []
        async def callback(*args):
            called.append(args)
            return {}
        engine.project_tool_callback = callback
        for decision in (edit(), tool()):
            with pytest.raises(DecisionRejected):
                await engine._action(run["id"], decision)
        assert called == []
    asyncio.run(scenario())


def test_tools_require_callback_and_parameters_are_host_checked(tmp_path):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        with pytest.raises(DecisionRejected, match="callback"):
            await engine._action(run["id"], tool())
        async def callback(*args):
            raise AssertionError("Invalid tool must not reach callback")
        engine.project_tool_callback = callback
        for decision in (tool("shell", {"command": "arbitrary"}), tool(arguments={"path": "../escape.py"}),
                         tool("run_application", {"entrypoint": "report.py", "args": "--help"}),
                         tool("read_file", {"path": "report.py", "alias": "outside"})):
            with pytest.raises(DecisionRejected):
                await engine._action(run["id"], decision)
    asyncio.run(scenario())


def test_tool_result_is_detached_feedback_and_not_verification(tmp_path):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        await engine._action(run["id"], edit())
        returned = {"exit_code": 0, "stdout": "UNIT tool output", "status": "passed", "verified_hash": "forged"}
        async def callback(run_id, name, arguments):
            assert name == "run_application"
            arguments["args"].append("callback mutated args")
            return returned
        engine.project_tool_callback = callback
        request = tool("run_application", {"entrypoint": "report.py", "args": ["--help"]})
        await engine._action(run["id"], request)
        returned["stdout"] = "mutated after callback"
        assert request["arguments"]["args"] == ["--help"]
        saved = engine.store.run(run["id"])
        assert saved["feedback"]["result"]["stdout"] == "UNIT tool output"
        assert saved["feedback"]["arguments"]["args"] == ["--help"]
        assert saved["feedback"]["verification_evidence"] is False
        assert saved["verified_hash"] is None
        assert len(saved["verifications"]) == 1  # Imported baseline only.
        with pytest.raises(DecisionRejected):
            await engine._action(run["id"], action("finish"))
    asyncio.run(scenario())


def test_callback_error_is_feedback_for_next_decision_and_delegation_remains_available(tmp_path):
    async def scenario():
        engine = environment(tmp_path, decisions=[tool(), action("dispatch", tasks=[task("inspect")]), action("stop")])
        async def callback(*args):
            raise FileNotFoundError("UNIT missing file")
        engine.project_tool_callback = callback
        run = engine.create_run_from_project("report task", revision())
        await engine.execute(run["id"])
        saved = engine.store.run(run["id"])
        assert saved["status"] == "needs_human"
        assert engine.model.contexts[1]["feedback"]["status"] == "failed"
        assert engine.model.contexts[1]["feedback"]["recoverable"] is True
        assert engine.model.work_ids == ["inspect"]
        assert saved["task_graph"][0]["status"] == "succeeded"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [TimeoutError("UNIT deadline"), asyncio.CancelledError("UNIT cancelled")])
def test_timeout_and_cancel_stop_loop_without_consuming_following_decision(tmp_path, failure):
    async def scenario():
        engine = environment(tmp_path, decisions=[tool(), action("stop")])
        async def callback(*args):
            raise failure
        engine.project_tool_callback = callback
        run = engine.create_run_from_project("report task", revision())
        if isinstance(failure, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await engine.execute(run["id"])
        else:
            await engine.execute(run["id"])
        saved = engine.store.run(run["id"])
        assert saved["status"] == ("blocked" if isinstance(failure, asyncio.CancelledError) else "needs_human")
        assert len(engine.model.decisions) == 1
        assert saved["verified_hash"] is None
        events = engine.store.events(run["id"])
        assert any(e["type"] == "tool_interrupted" for e in events)
        assert not any(e["type"] == "tool_result" for e in events)
    asyncio.run(scenario())


def test_tool_mutation_invalidates_verification_and_stops(tmp_path):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        async def callback(*args):
            engine.sandbox.files[run["id"]]["report.py"] = "unexpected source mutation"
            return {"exit_code": 0}
        engine.project_tool_callback = callback
        with pytest.raises(SandboxIntegrityError):
            await engine._action(run["id"], tool())
        assert engine.store.run(run["id"])["verified_hash"] is None
    asyncio.run(scenario())


def test_tool_steps_respect_existing_controller_budget(tmp_path):
    async def scenario():
        engine = environment(tmp_path, decisions=[tool(), tool(), action("stop")])
        calls = []
        async def callback(*args):
            calls.append(args)
            return {"content": "UNIT text"}
        engine.project_tool_callback = callback
        run = engine.create_run_from_project("report task", revision(), budget={"max_steps": 2})
        await engine.execute(run["id"])
        assert len(calls) == 2 and len(engine.model.decisions) == 1
        assert engine.store.run(run["id"])["status"] == "needs_human"
        assert "decision budget" in engine.store.run(run["id"])["summary"]
    asyncio.run(scenario())


def test_adapter_offers_direct_project_actions_in_actual_payload_without_changing_v6_config(tmp_path):
    async def scenario():
        engine, run = setup_adapter(tmp_path)
        engine.runtime._post = Mock(return_value=response(edit()))
        output = await engine.model.decide(run, {"project_actions": {"edit": True, "tools": ["read_file"]}})
        assert output["action"] == "edit"
        sent = engine.runtime._post.call_args.args[0]
        prompt = sent["messages"][0]["content"]
        assert "do not delegate every small change" in prompt
        assert '"action":"edit"' in prompt and '"action":"tool"' in prompt
        assert "CLI exit code 0 DOES NOT permit finish" in prompt
        assert run["model_config"]["adapter_revision"] == "autonomous-6-project-revisions"
        assert validate_decision(tool()) == tool()
    asyncio.run(scenario())


def test_direct_observe_edit_verify_loop_uses_current_feedback_without_delegation(tmp_path):
    async def scenario():
        engine = environment(tmp_path, decisions=[tool(), edit(), tool("run_application", {"entrypoint": "report.py", "args": ["--help"]}), action("verify"), action("finish")],
                             statuses=["failed", "passed"])
        seen = []
        async def callback(run_id, name, args):
            current = await engine.sandbox.read(run_id)
            seen.append((name, deepcopy(current)))
            return {"content": current["report.py"]} if name == "read_file" else {"status": "succeeded", "exit_code": 0, "stdout": "UNIT help"}
        engine.project_tool_callback = callback
        run = engine.create_run_from_project("report task", revision())
        await engine.execute(run["id"])
        saved = engine.store.run(run["id"])
        assert saved["status"] == "succeeded" and saved["task_graph"] == []
        assert engine.model.work_ids == []
        assert seen[0][1]["report.py"] == SOURCE
        assert seen[1][1]["report.py"] != SOURCE
        assert engine.model.contexts[1]["feedback"]["kind"] == "tool_result"
        assert engine.model.contexts[2]["feedback"]["kind"] == "main_edit"
        assert len(saved["verifications"]) == 2  # Only independent verify changes this.
    asyncio.run(scenario())


def test_reported_timeout_is_not_a_recoverable_tool_result(tmp_path):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        async def callback(*args):
            return {"status": "blocked", "timed_out": True}
        engine.project_tool_callback = callback
        with pytest.raises(TimeoutError):
            await engine._action(run["id"], tool())
        assert engine.store.run(run["id"])["verified_hash"] is None
    asyncio.run(scenario())


def test_blocked_tool_and_bounded_output_are_explicit_feedback(tmp_path):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        async def unavailable(*args):
            return {"status": "blocked", "container_invoked": False, "isolated": False}
        engine.project_tool_callback = unavailable
        await engine._action(run["id"], tool("run_application", {"entrypoint": "report.py", "args": []}))
        assert engine.store.run(run["id"])["feedback"]["status"] == "blocked"
        async def too_large(*args):
            return {"content": "x" * 65536}
        engine.project_tool_callback = too_large
        await engine._action(run["id"], tool())
        feedback = engine.store.run(run["id"])["feedback"]
        assert feedback["status"] == "failed" and "64 KiB" in feedback["error"]
    asyncio.run(scenario())


def test_search_query_contract_matches_host_limit():
    assert validate_project_tool("search_project", {"query": "x" * 200}) == {"query": "x" * 200}
    with pytest.raises(ValueError):
        validate_project_tool("search_project", {"query": "x" * 201})


def test_stale_tool_receipt_is_not_presented_as_current_evidence(tmp_path):
    async def scenario():
        engine = environment(tmp_path)
        run = await prepared(engine)
        async def callback(*args):
            return {"candidate_sha256": "stale-candidate", "exit_code": 0, "stdout": "old output"}
        engine.project_tool_callback = callback
        await engine._action(run["id"], tool())
        feedback = engine.store.run(run["id"])["feedback"]
        assert feedback["status"] == "failed"
        assert "different candidate hash" in feedback["error"]
        assert "result" not in feedback
    asyncio.run(scenario())
