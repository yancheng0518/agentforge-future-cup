"""Versioned, code-defined templates. No candidate code is executed here."""
from copy import deepcopy


def node(id, label, capability, operation="agent", depends_on=(), priority=50, **extra):
    return dict(id=id, label=label, capability=capability, operation=operation,
                kind=extra.pop("kind", operation), depends_on=list(depends_on),
                priority=priority, **extra)


VALIDATION = [
    node("test", "独立验收测试", "test", "test", priority=90),
    node("inspect", "需求与差异检查", "review", priority=70),
    node("review", "验证结果审查", "review", depends_on=("test", "inspect"), priority=80),
]


WORKFLOWS = {
    "development": {
        "id": "development", "name": "软件需求实现", "version": 1,
        "description": "规划 → 候选开发 → 并行验证子工作流 → 条件判断 → 最多两轮修正。",
        "max_repairs": 2,
        "nodes": [
            node("prepare", "读取项目与知识", None, "prepare", priority=100),
            node("plan", "任务规划", "plan", depends_on=("prepare",), priority=90),
            node("develop", "候选代码开发", "develop", "edit", ("plan",), 80),
            node("verify", "验证子工作流", None, "subworkflow", ("develop",), 70,
                 children=deepcopy(VALIDATION)),
            node("gate", "验收条件判断", None, "condition", ("verify",), 60),
            node("fix", "反馈修正 · 最多两轮", "fix", "edit", ("gate",), 50,
                 kind="loop", conditional=True),
        ],
    },
    "review": {
        "id": "review", "name": "代码审查", "version": 1,
        "description": "读取项目与知识，规划检查，并行验收与差异分析，汇总审查报告。",
        "max_repairs": 0,
        "nodes": [
            node("prepare", "读取项目与知识", None, "prepare", priority=100),
            node("plan", "审查规划", "plan", depends_on=("prepare",), priority=90),
            node("verify", "审查验证子工作流", None, "subworkflow", ("plan",), 80,
                 children=deepcopy(VALIDATION)),
            node("gate", "审查结果汇总", None, "condition", ("verify",), 70),
        ],
    },
}

AGENTS = [
    {"id": "planner", "name": "规划 Agent", "role": "planner", "capabilities": ["plan"],
     "prompt": "读取项目与引用资料。将需求拆成可验证的开发、测试和审查任务。返回 JSON tasks 和 summary。",
     "tools": ["read_project", "knowledge_search"]},
    {"id": "developer", "name": "开发 Agent", "role": "developer", "capabilities": ["develop", "fix"],
     "prompt": "依据需求和规划生成 todo.py 的完整候选内容，保持兼容。仅返回 JSON files 列表和 summary，不修改验收测试。",
     "tools": ["read_project", "knowledge_search", "candidate_diff"]},
    {"id": "tester", "name": "测试 Agent", "role": "tester", "capabilities": ["test"],
     "prompt": "解读受控容器独立验收测试的真实结果，指出覆盖缺口。不得把 blocked 或模拟结果当作真实通过。",
     "tools": ["run_acceptance", "read_project"]},
    {"id": "reviewer", "name": "审查 Agent", "role": "reviewer", "capabilities": ["review"],
     "prompt": "对照需求、候选差异、资料和已有测试结果审查。返回 JSON passed 布尔值、issues 数组、summary；证据不足则不通过。",
     "tools": ["read_project", "knowledge_search", "candidate_diff"]},
    {"id": "fixer", "name": "修正 Agent", "role": "fixer", "capabilities": ["fix"],
     "prompt": "根据具体验收失败和审查问题修复 todo.py，返回完整 JSON files 列表与 summary。不得修改独立验收测试。",
     "tools": ["read_project", "knowledge_search", "candidate_diff"]},
]
for agent in AGENTS:
    agent.update(enabled=True, model="default", version=1)
