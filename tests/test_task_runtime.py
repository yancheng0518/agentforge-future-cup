"""Frozen task integration tests; transports are stubbed, no candidate execution."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
from unittest.mock import AsyncMock, patch

import pytest

from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.task_catalog import snapshot_task, list_tasks, digest
from agentforge.engine import Engine
from agentforge.storage import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(tmp_path):
    for name in ("examples", "knowledge", "acceptance", "benchmarks"):
        shutil.copytree(ROOT / name, tmp_path / name)
    rt = Runtime(tmp_path, tmp_path / "data")
    rt.settings.update(api_key="test-not-a-real-key", model="fake", min_request_interval=0)
    return rt


def test_catalog_has_four_explicit_independent_contracts(runtime):
    catalog = list_tasks(runtime.root)
    assert len(catalog) == 4
    assert len({item["acceptance_sha256"] for item in catalog}) == 4
    assert all(item["expected_tests"] == 10 and "baseline" not in item and "acceptance" not in item for item in catalog)
    with pytest.raises(ValueError):
        snapshot_task(runtime.root, "../outside")


def test_task_freezes_before_source_change_and_blocks_test_tamper(runtime):
    async def scenario():
        snap = runtime.snapshot_task("bulk_edit")
        (runtime.root / snap["paths"]["baseline"]).write_text("# changed after snapshot", encoding="utf8")
        manifest = await runtime.prepare("frozen", snap)
        assert (await runtime.read_project("frozen"))["files"][0]["content"] == snap["baseline"]
        assert manifest["task"]["id"] == "bulk_edit"
        with pytest.raises(RuntimeErrorSafe, match="快照"):
            await runtime.prepare("frozen", runtime.snapshot_task("priority_query"))
        trusted = runtime._run_dir("frozen") / "trusted/test_todo.py"
        trusted.write_text("# tampered", encoding="utf8")
        with patch.object(runtime, "health", AsyncMock(return_value={"sandbox": {"available": True}})), patch.object(runtime, "_command", AsyncMock()) as cmd:
            with pytest.raises(RuntimeErrorSafe, match="快照"):
                await runtime.test("frozen")
            cmd.assert_not_called()
    asyncio.run(scenario())


def test_selected_suite_is_separate_readonly_mount_and_contract_retrieved(runtime):
    async def scenario():
        snap = runtime.snapshot_task("priority_query")
        await runtime.prepare("selected", snap)
        evidence = 'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": true}'
        with patch.object(runtime, "health", AsyncMock(return_value={"sandbox": {"available": True}})), patch.object(runtime, "_command", AsyncMock(return_value=(0, evidence, "Ran 10 tests in 0.1s"))) as cmd:
            result = await runtime.test("selected")
        args = cmd.await_args_list[0].args[0]
        assert any("target=/task_acceptance,readonly" in item for item in args)
        assert args[-2:] == ["--suite", "/task_acceptance"]
        assert result["acceptance_sha256"] == snap["acceptance_sha256"]
        knowledge = await runtime.retrieve("query", "selected")
        assert knowledge["matches"][0]["excerpt"] == snap["requirement"]
        assert not any(item["path"] == "knowledge/requirements.md" for item in knowledge["matches"])
        with pytest.raises(RuntimeErrorSafe, match="基线不可应用"):
            await runtime.apply("selected")
    asyncio.run(scenario())


def test_candidate_versions_preserved_without_applying(runtime):
    async def scenario():
        await runtime.prepare("versions", runtime.snapshot_task("json_merge"))
        for text in ("# first\n", "# second\n"):
            await runtime.apply_candidate("versions", [{"path": "todo.py", "content": text}])
            assert (runtime._run_dir("versions") / "candidates" / (digest(text) + ".py.txt")).read_text() == text
    asyncio.run(scenario())


def test_engine_snapshots_model_task_and_rejects_config_drift(runtime):
    async def scenario():
        store = Store(runtime.data_dir / "test.sqlite3")
        engine = Engine(store, runtime)
        run = engine.create_run("declared task", mode="live", task_id="json_merge")
        assert run["task_snapshot"]["id"] == "json_merge"
        assert run["fault_injection"] is False
        runtime.settings["model"] = "different"
        await engine.execute(run["id"])
        assert store.run(run["id"])["status"] == "blocked"
        assert store.run(run["id"])["model_calls"] == 0
        with pytest.raises(ValueError, match="仅支持真实模型"):
            engine.create_run("task", mode="mock", task_id="bulk_edit")
    asyncio.run(scenario())


def test_large_context_keeps_valid_json_and_current_feedback(runtime):
    async def scenario():
        context = {"project": {"files": []}, "feedback": {"test": "current failure"},
                   "task_contract": {"requirement": "atomic"}, "develop": {"files": "x" * 100000},
                   "diff": {"unified_diff": "x" * 100000}}
        response = {"choices": [{"message": {"content": '{"files":[],"summary":"stub"}'}}], "usage": {"total_tokens": 1}}
        with patch.object(runtime, "_post", return_value=response) as post:
            await runtime.invoke({"role": "fixer", "tools": []}, {"mode": "live", "kind": "fix"}, context)
        payload = json.loads(post.call_args.args[0]["messages"][1]["content"])
        assert payload["context"]["feedback"] == context["feedback"]
        assert payload["context"]["task_contract"]["requirement"] == "atomic"
        assert "develop" not in payload["context"]
    asyncio.run(scenario())
