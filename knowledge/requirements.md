# Todo 演示需求与验收契约

主演示任务：增加任务完成状态，并支持按照完成状态筛选任务。

受控目标只有 `todo.py`，类名为 `TodoStore`。
- `add(title)` 创建任务，id 单调递增，title 去除首尾空格，空标题抛出 ValueError。
- 新任务 `completed` 默认为 False。
- `complete(task_id, completed=True)` 标记完成/恢复未完成并返回任务副本，未知 id 抛出 KeyError。
- `list(completed=None)` 返回所有任务；True/False 分别只返回完成/未完成任务。
- 外部修改返回字典不能改写内部任务，多个实例不能共享任务。

新增任意功能需求可交给真实模型，但第一版独立验收仅覆盖上述契约，其他需求不得据此宣称验收通过。
