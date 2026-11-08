# 开发规范

只修改候选副本中的 todo.py；不得修改验收测试，不得访问网络、系统环境变量或外部文件。
保留 TodoStore.add/list 接口和 id 语义。使用 Python 标准库，避免不必要依赖。
返回纯 JSON 对象，包含 summary 与 files（每项 path/content）。无法完成时解释问题，不要虚构测试结果。
知识检索为本地关键词检索；引用使用实际文件名，不代表向量 RAG 或长期学习。
