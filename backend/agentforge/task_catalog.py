"""Allowlisted task fixtures and immutable contracts; never import candidate code."""
import hashlib
import json
from pathlib import Path

TASKS = {
    "completion": "完成状态与筛选",
    "bulk_edit": "批量编辑与原子更新",
    "priority_query": "优先级与稳定分页查询",
    "json_merge": "JSON 导入、导出与原子合并",
}


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def snapshot_task(root, task_id="completion"):
    if task_id not in TASKS:
        raise ValueError("未知受控任务")
    root = Path(root).resolve()
    if task_id == "completion":
        paths = {"baseline": "examples/todo/todo.py", "requirement": "knowledge/requirements.md",
                 "acceptance": "acceptance/test_todo.py"}
    else:
        base = f"benchmarks/tasks/{task_id}"
        paths = {"baseline": base + "/todo.py", "requirement": base + "/requirement.md",
                 "acceptance": base + "/test_todo.py"}
    result = {"id": task_id, "name": TASKS[task_id], "version": 1, "expected_tests": 10,
              "provenance": "controlled_task_baseline_no_injected_model_defect", "paths": paths}
    for kind, relative in paths.items():
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("任务文件路径超出受控范围")
        content = path.read_text(encoding="utf-8-sig")
        result[kind] = content
        result[kind + "_sha256"] = digest(content)
    result["snapshot_sha256"] = digest(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return result


def public_task(snapshot):
    return {key: value for key, value in snapshot.items() if key not in ("baseline", "acceptance")}


def list_tasks(root):
    return [public_task(snapshot_task(root, task_id)) for task_id in TASKS]
