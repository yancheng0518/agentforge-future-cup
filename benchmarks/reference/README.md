# 正确参考控制

这三个 `.py.txt` 是维护者编写的正控制源码，只用于在容器内确认对应独立验收套件本身可通过。它们不是模型生成结果，不是开发任务起始源码，不应放入模型提示词、知识检索或任务候选。

- `bulk_edit.py.txt` 对应 `benchmarks/tasks/bulk_edit/test_todo.py`。
- `priority_query.py.txt` 对应 `benchmarks/tasks/priority_query/test_todo.py`。
- `json_merge.py.txt` 对应 `benchmarks/tasks/json_merge/test_todo.py`。

每套固定 10 项 unittest 测试。控制是否实际通过，以受控 Docker 运行证据为准；解析成功不能当作验收成功。严禁在宿主 Python 中导入或执行这些文件。
