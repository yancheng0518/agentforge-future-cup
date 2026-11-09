"""Static intake/model tests only; never call a real model or execute candidates."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.autonomous import AutonomousEngine
from agentforge.autonomous_model import validate_intake, AutonomousModel
from agentforge.runtime import Runtime
from agentforge.storage import Store


def proposal():
    return {"status": "ready", "summary": "生成加法接口和边界验收",
            "specification": "numbers.py 的 add(a,b) 返回两个整数之和，非整数抛 ValueError。",
            "assumptions": ["输入不接受布尔值"],
            "files": {"numbers.py": "def add(a, b):\n    raise NotImplementedError\n"},
            "acceptance": """import unittest
from bridge import call
class TestAddition(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(call('numbers','add',2,3),5)
    def test_zero(self):
        self.assertEqual(call('numbers','add',0,0),0)
    def test_invalid(self):
        with self.assertRaises(ValueError):
            call('numbers','add','2',3)
"""}


def environment(tmp_path):
    runtime = Runtime(ROOT, tmp_path / "data")
    runtime.settings.update(api_key="FAKE-KEY-NOT-REAL", model="fake-only", base_url="https://example.invalid/v1", min_request_interval=0)
    runtime._post = Mock(side_effect=AssertionError("No live model allowed in unit tests"))
    runtime._command = Mock(side_effect=AssertionError("No command execution allowed in unit tests"))
    store = Store(tmp_path / "state.sqlite3")
    return runtime, store, AutonomousEngine(store, runtime)


def response(value, tokens=20):
    return {"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}], "usage": {"total_tokens": tokens}}


def test_valid_pure_function_contract_does_not_import_candidate():
    value = proposal()
    assert validate_intake(value) == value


@pytest.mark.parametrize("edit", [
    lambda x: x.update(files={"numbers.py": "def add(a,b): return a+b"}),
    lambda x: x.update(files={"numbers.py": "def add(:"}),
    lambda x: x.update(files={"../escape.py": "def add(a,b): raise NotImplementedError"}),
    lambda x: x.update(acceptance="print('pretend passed')"),
    lambda x: x.update(acceptance=x["acceptance"].replace("from bridge import call", "from numbers import add")),
    lambda x: x.update(acceptance=x["acceptance"].replace("'add'", "'missing'")),
    lambda x: x.update(acceptance=x["acceptance"].replace("self.assertRaises(ValueError)", "self.assertRaises(Exception)")),
    lambda x: x.update(acceptance=x["acceptance"].replace("self.assertRaises(ValueError)", "self.assertRaises(NotImplementedError)")),
    lambda x: x.update(acceptance=x["acceptance"].replace("self.assertEqual(call('numbers','add',0,0),0)", "self.assertTrue(True)")),
    lambda x: x.update(acceptance=x["acceptance"].replace("def test_zero", "def helper_zero")),
    lambda x: x.update(acceptance=x["acceptance"].replace("def test_zero", "def test_positive")),
    lambda x: x.update(acceptance=x["acceptance"] + "\nclass TestAddition(unittest.TestCase):\n    pass\n"),
])
def test_rejects_implemented_scaffolds_and_obviously_invalid_acceptance(edit):
    value = proposal()
    edit(value)
    with pytest.raises(ValueError):
        validate_intake(value)


def test_needs_input_is_explicit_and_bounded():
    value = {"status": "needs_input", "summary": "当前不支持浏览器游戏", "questions": ["是否改为可测试的 Python 规则函数？"]}
    assert validate_intake(value) == value
    with pytest.raises(ValueError):
        validate_intake({**value, "questions": []})


def test_request_is_saved_without_fixed_task_bundle_or_paid_calls(tmp_path):
    runtime, store, engine = environment(tmp_path)
    run = engine.create_run_from_requirement("统计传感器温度的均值和异常值", {"max_tokens": 300000})
    assert run["requirement"] == "统计传感器温度的均值和异常值"
    assert run["bundle"] is None and run["bundle_sha256"] is None
    assert run["task_graph"] == [] and run["controller_decisions"] == []
    assert run["intake"] == {"status": "pending", "acceptance_origin": "model_generated"}
    assert (runtime._run_dir(run["id"]) / "implementation-manifest.json").is_file()
    manifest = json.loads((runtime._run_dir(run["id"]) / "implementation-manifest.json").read_text())
    assert "backend/agentforge/autonomous_model.py" in manifest
    runtime._post.assert_not_called()
    runtime._command.assert_not_called()


@pytest.mark.parametrize("requirement", ["", "   ", "a" * 8001, None])
def test_invalid_requirement_creates_no_run(tmp_path, requirement):
    _, store, engine = environment(tmp_path)
    with pytest.raises(ValueError):
        engine.create_run_from_requirement(requirement)
    assert store.runs() == []


def test_intake_real_adapter_records_format_retry_and_shared_usage(tmp_path):
    async def scenario():
        runtime, store, engine = environment(tmp_path)
        run = engine.create_run_from_requirement("计算整数相加", {"max_tokens": 300000})
        invalid = proposal()
        invalid["files"]["numbers.py"] = "def add(a,b): return a+b"
        runtime._post = Mock(side_effect=[response(invalid), response(proposal(), 30)])
        result = await engine.model.prepare(run)
        assert result["status"] == "ready"
        metrics = store.run(run["id"])["model_metrics"]
        assert metrics["model_calls"] == 2 and metrics["tokens"] == 50 and metrics["contract_retries"] == 1
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((runtime._run_dir(run["id"]) / "model_requests").glob("*.json"))]
        assert [record["role"] for record in records] == ["intake", "intake"]
        assert [record["status"] for record in records] == ["protocol_failed", "accepted"]
        assert "FAKE-KEY-NOT-REAL" not in json.dumps(records)
        runtime._command.assert_not_called()
    asyncio.run(scenario())
