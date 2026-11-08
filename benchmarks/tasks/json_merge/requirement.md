# 任务：版本化 JSON 导入与原子合并

在当前 `todo.py` 的 `TodoStore` 上增加 JSON 导出/导入合并。仅修改该文件，不改独立验收。初始基础完成状态已实现，导入导出功能尚未实现；它是维护者提供的开发起点，不是自然模型错误或向模型候选注入的缺陷。

## 基础兼容

`TodoStore()` 无参数，实例独立。保留 add、complete、list：非空字符串标题去除首尾空白，非法标题抛 ValueError；id 从 1 严格递增且不复用；新任务 completed=False；complete 支持完成/恢复，未知 id 抛 KeyError；list(None/True/False) 按插入顺序返回全部/完成/未完成的任务副本。任务字段恰为 id、title、completed，不增加其他字段。

## 导出

`export_json()` 返回 JSON **字符串**，解码后恰为 `{"version": 1, "tasks": [...]}`。tasks 按当前 list() 顺序排列，每个任务恰有 id/title/completed 三个字段；中文、换行、引号等标题内容保持正确。不要求 JSON 的缩进、空格、Unicode 转义方式或字段排列形式。导出不能改变 store。

## 导入

`import_json(payload, collision="error")` 接受 JSON 字符串，导入到当前 store，返回 `{"inserted": n, "updated": n, "skipped": n}` 三个整数计数。

完整验证规则如下；任意非法输入都抛 **ValueError**，而不是将错误吞掉或部分接受：

- collision 必须为字符串 `error`、`replace`、`skip` 之一。
- payload 必须为 str 且能解析成 JSON；顶层必须恰含 version、tasks 两个键，拒绝多余或缺失字段。
- version 必须为精确整数 1，`true` 不等同于版本 1。tasks 必须为 list。
- 每项必须为恰含 id/title/completed 的字典。id 必须为正的精确整数，拒绝 bool、浮点、字符串。title 必须为非空字符串，导入时去除首尾空白，结果不得为空。completed 必须为精确 bool，拒绝 0/1、null 或字符串。
- 同一输入中的重复 id 总是非法，包括 collision=skip/replace；不能静默取最后一项。
- **先验证整个文档和所有任务，再做任何修改**。skip 模式下，即使对应 id 已存在，也必须验证那项内容。出现任何错误时，任务内容、顺序与下一次 add 的 id 都必须保持不变。

碰撞规则针对与 store 中现有 id 的冲突：

- error：只要一个输入 id 已存在，就抛 ValueError，整个导入不变。
- replace：替换冲突任务的 title/completed，保留该任务在现有列表中的位置，updated 加 1。
- skip：保留冲突任务完全不变，skipped 加 1。
- 三种模式下，不冲突的新任务保留输入 id，并按**输入顺序**追加在现有任务之后，inserted 加 1。
- 成功导入后，下一次 add 的 id 必须不小于导入前下一个 id，并且大于 store 中所有 id。不能重编号导入任务。空 tasks 有效，计数均为 0，不改变 store 或 id 分配。

返回的 list/complete/add 字典保持副本语义；导出字符串被外部解析和改写不影响 store。两个实例之间不能共享导入状态。

## 独立验收范围

固定 10 个测试覆盖基础兼容、空库与中文转义导出、稀疏 id 导入分配、error 碰撞的原子性、replace 保持位置、skip 全文验证、非法 JSON/schema/type/重复 id 的原子失败、往返、空导入与实例隔离。全部要求在模型运行前确定。
