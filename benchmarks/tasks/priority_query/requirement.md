# 任务：优先级与稳定分页查询

在当前 `todo.py` 的 `TodoStore` 上增加任务优先级和组合查询；仅修改该文件，不修改独立验收。初始基础完成状态已实现，优先级功能尚未实现，是维护者准备的开发起点，不是模型生成失败或注入模型候选的故障。

## 基础兼容

`TodoStore()` 无参数且各实例独立。`add(title)` 仍校验非空字符串并去除首尾空白，非法标题抛 `ValueError`；id 从 1 单调递增，新任务 `completed=False`。`complete(task_id, completed=True)` 保持完成/恢复行为及未知 id 的 `KeyError`。`list(completed=None)` 保持原有完成状态筛选和**插入顺序**，不能因优先级排序而改变 list 语义。

本任务每个任务字典新增 `priority` 字段，默认整数 **1**。add、complete、list、set_priority、query 返回的任务字典均含 `id/title/completed/priority`，都是副本。

## 新接口与严格验证

- `set_priority(task_id, priority)`：更新并返回任务副本，不改变完成状态、标题或内部顺序。task_id 必须为正整数且 `type(task_id) is int`，priority 必须恰为整数 0、1、2、3；布尔值、浮点和字符串等一律 `ValueError`。先验证参数格式，再查找 id；格式合法但未知 id 抛 `KeyError`。任何失败都不得修改任务。
- `query(completed=None, min_priority=0, limit=None, offset=0)`：按如下顺序执行：**验证所有参数 → 完成状态与最低优先级过滤 → priority 降序、id 升序排序 → offset/limit 分页 → 返回副本列表**。不得先分页再过滤，也不得改变内部存储顺序。
- completed 仅接受 `None`、精确 `True`、精确 `False`（整数 0/1 无效）。min_priority 仅接受精确整数 0..3。offset 接受精确非负整数。limit 接受 `None` 或精确非负整数；0 表示空结果，None 表示从 offset 起不限数量。上述整数参数均不接受 bool。所有非法参数抛 `ValueError`，即使 store 为空或分页结果本来为空也须验证。
- offset 超过过滤后结果长度返回 `[]`；未提供参数的 query 返回全部任务，按规定优先级排序。

## 独立验收范围

固定 10 个测试覆盖基础默认字段、set_priority 成功/失败、稳定排序、组合过滤、先过滤再分页、所有查询参数边界、返回副本与 list 顺序、实例独立及空库参数验证。验收不会在观察模型结果后追加隐藏条件。
