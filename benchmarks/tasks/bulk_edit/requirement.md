# 任务：原子批量编辑

在当前 `todo.py` 的 `TodoStore` 上新增重命名、删除、原子批量完成；仅修改该文件，不修改测试。初始项目的基础完成状态功能已实现，新功能尚未实现；它是维护者准备的开发起点，不是模型生成失败或向模型候选注入的故障。

## 兼容契约

- `TodoStore()` 无参数，实例不共享状态。
- `add(title)` 接受非空字符串，去除首尾空白，非法值抛 `ValueError`；分配从 1 开始、严格递增且不复用的整数 id，新任务 `completed=False`，返回任务字典副本。
- `complete(task_id, completed=True)` 保留已有完成/恢复行为，未知 id 抛 `KeyError`，返回任务副本。
- `list(completed=None)` 维持插入顺序；`None` 返回全部，`True/False` 分别筛选完成/未完成。所有返回任务均为副本。任务字段保持 `id/title/completed`。

## 新接口

1. `rename(task_id, title)`：去除新标题首尾空白，更新标题，返回更新后任务副本；保留 id、完成状态和原顺序。重复标题允许。
2. `delete(task_id)`：删除并返回被删除任务的副本；其余任务相对顺序不变，删除后再 add 不得复用 id。
3. `batch_complete(task_ids, completed=True)`：按输入 id 顺序返回修改后的任务副本列表，内部任务的插入顺序不变。`completed` 必须恰为 `bool`，不接受整数 0/1 或其他 truthy 值。空列表有效，返回 `[]` 且不改变状态。

三个新接口的 id 必须为正整数，要求 `type(id) is int`，明确拒绝布尔值；格式错误抛 `ValueError`，格式合法但不存在抛 `KeyError`。rename 的标题规则与 add 相同，先检查参数格式，再查找 id。

`task_ids` 必须恰为 Python list，其中每项满足上述 id 规则，且不得有重复 id；类型错误、无效项、重复项或非法 completed 都抛 `ValueError`。全部参数及所有 id 的存在性必须在任何修改之前检查。若批次中任意一个 id 不存在，抛 `KeyError`，**整个批次不做任何更改**；其他验证错误也不得产生部分修改。成功批次允许顺序与内部存储顺序不同。

## 独立验收范围

固定 10 个测试覆盖基础兼容、rename 的空白/副本/格式、delete 的顺序与 id 不复用、批量输入顺序与副本、重复及布尔 id、未知 id 的原子失败、空批次、恢复未完成、实例独立。所有要求已在上文声明；不因模型输出而修改验收。
