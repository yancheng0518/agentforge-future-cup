# 项目工作区复现包：在全新目录恢复

本轮打包脚本是 `output/project-workspaces-20261109/package_reproduction.py`，目标是新建 `output/agentforge-project-workspaces-20261109.zip`。脚本准备完成不代表压缩包已经生成；以同目录的 `.zip.sha256` 和本轮 `archive-manifest.json` 的核验结果为准。它不调用模型、不运行 Docker、不执行候选、不上传文件，也不改写当前数据库。

本轮归档合同固定为旧工作台 17 条运行、动态 API 8 条运行，其中本轮同项目迭代 2 条。项目、版本、配置、资料和程序执行数量从打包时数据库的一致性快照取得，以 manifest 为准；额外导入但未调用模型的检查项目不会增加模型运行数。固定运行数量变化、仍有进行中的运行或待落库回执都会报错，需先核实变化，不能通过删除旧记录满足数量要求。

## 生成与核对

等待所有开发、文档、独立验收和证据整理完成，并停止产生新任务、程序执行或配置编辑后，在项目根目录运行：

```powershell
python output/project-workspaces-20261109/package_reproduction.py
python scripts/export_reproduction.py --verify output/agentforge-project-workspaces-20261109.zip
```

脚本独占创建归档、SHA-256 文件、归档报告和 `package-database-snapshots` 目录；任何目标已存在都会拒绝覆盖。失败时可能留下未认证的部分文件，脚本不会自动删除或复用它们。应先查明失败原因，保留失败记录，再另行决定新的输出批次。

`REPRODUCTION_MANIFEST.json` 列出每个成员的大小、SHA-256、类别，各数据库的逐表行数和内容摘要，以及实际运行的实现源码快照位置。核验命令只读 ZIP，不解压和执行其中的代码。SHA-256 用于检测内容变化，不证明作者身份。

## 只在新空目录恢复

1. 创建一个此前从未运行过 AgentForge 的空目录，将整个 ZIP 解压到该目录。不要覆盖当前 `D:\未来杯`，也不要将包内数据库复制回仍在使用的服务。
2. 根目录下的 `.agentforge/agentforge.sqlite3`、`.agentforge/autonomous/agentforge.sqlite3`、`.agentforge/projects/projects.sqlite3`、`.agentforge/projects/requests.sqlite3` 已映射为本次只读 SQLite backup API 获得的一致性快照。所有被选中的 `application-tools/scratch.sqlite3` 也同样处理。无需再复制数据库、清空历史或运行迁移恢复脚本。
3. 在新目录按 `README.md` 使用 `backend/requirements-lock.txt` 和 `frontend/package-lock.json` 重建依赖。压缩包不包含虚拟环境、Node 依赖、构建缓存、WSL 磁盘、运行日志或真实 `.env`。若旧服务还占用端口，给新服务使用独立端口；不要同时指向原工作目录。
4. 仅核对 JSON、源码、数据库或 ZIP 不需要模型凭据。启动后查看历史也不发送新模型请求；再次提交需求才需要另在新目录配置自己的模型与 Docker，且会产生新的调用和费用。模型重新运行不保证得到相同结果。
5. 候选代码只能经项目提供的 Docker 入口执行，不要在宿主机直接导入或运行归档里的候选、验收代码或应用入口。程序退出码 0 与开发验收通过分开记录。

完整换机安装及从空环境启动，只有在另有实测记录时才能称为已验证。当前包的成员校验和原机真实运行证据不能替代换机验收。

## 当前数据库与历史快照的区别

清单中的 `database_snapshots` 为每个数据库记录 `source_database`、`archive_member`、`backup_member`、`role`、`row_counts` 和逐表摘要。

- `restorable_database`：以原相对路径恢复的当前应用数据库，包括项目版本、配置、知识、幂等请求、程序回执、应用数据及本轮工具临时数据库。原路径只是 ZIP 内的映射，不会写回当前项目目录。
- `historical_snapshot`：`preservation-before.json` 引用的 `legacy-before.sqlite3`、`autonomous-before.sqlite3`、`validation-before.sqlite3`。它们保留在本轮 `output` 目录，用来核对改动前数据，不能作为当前运行数据库覆盖使用。脚本在备份后按原 preservation manifest 核对全部表名、行数及内容摘要；列名也记录在新 manifest 中。新 SQLite 文件的物理字节布局可与旧快照不同。
- `package-database-snapshots`：上述所有数据库的额外只读备份成员。其角色由 manifest 标明；不是另一套需要启动的服务。

所有 SQLite 都通过只读连接备份，生成的副本转换为独立 DELETE journal 文件；不会把 live DB/WAL/SHM 原样混拷。打包前后还会检查源数据库的逐表内容是否改变，并核对项目头版本、版本链、源码/应用状态哈希、run 发布记录、幂等回执及工具临时项目指针。各数据库分别有一致性事务快照，并且以无活动请求、无中途写入作为跨库归档前提；这不是跨库事务备份系统。

项目应用数据是本次维护者构造的虚构记账样本，随项目数据库与程序执行输入/回执保留；仍经过与其他内容相同的凭据扫描。正式项目数据与 `application-tools` 的每轮临时数据独立，恢复后不可将后者合并回项目正式数据。

## 证据与源码范围

当前源码按现有导出器的明确目录清单选取，包含后端、前端、测试、脚本、文档、验收、知识和示例等；本轮全部证据来自以下明确目录，不扫描整个机器：

- `.agentforge/runs`
- `.agentforge/autonomous/runs`
- `.agentforge/autonomous-chat-posthoc-20261108`
- `.agentforge/collaboration-graph-checks-20261108`
- `.agentforge/projects`
- `output/project-workspaces-20261109`

真实请求、响应、候选、冻结验收、程序执行输入、实现快照和成功/失败回执按原路径保留。运行状态、格式重试、决定被拒、导入基线失败、候选失败、预设缺陷及事后否定结论仍分别阅读，不能只统计最终成功记录。

本轮两条真实模型运行是 `ce012be5a56e452c86805463b3ba65e6` 和 `c77677a111d641ebb743b7678d5546e4`。各自 `implementation-manifest.json` 和 `implementation/` 保存该次运行实际使用的宿主实现；打包时源码是工作区最终字节，不应冒称与两次运行时版本完全相同。候选实现、测试和代码配置快照另存于该运行目录及证据中。

三个旧包 `agentforge-autonomous-core-20261108.zip`、`agentforge-autonomous-chat-20261108.zip`、`agentforge-collaboration-graph-20261108.zip` 与其 SHA-256 文件原样嵌入 `output/`，用于保留更早实验、对照和旧验证数据库，不会在本轮重写。不要把三个旧包再解压覆盖当前恢复目录；审阅时单独核验或解压到不同空目录。

旧 core 包原本含一个 32 KiB SHM 和一个空 WAL，旧协作图包含两份前端测试/构建日志。为不改写封存历史，这四个成员仅在原旧包内保留；脚本同时固定整包 SHA-256、成员路径和成员 SHA-256，作为 `sealed_legacy_archive_exceptions` 单列。它们不能作为当前数据库恢复来源，仍接受凭据、路径和大小扫描；本轮没有复制任何 live WAL/SHM。除此以外的新成员、未知归档或变化后的旧归档不享有此例外。

打包同时扫描所有选中成员及嵌套 ZIP 解压后的内容，拒绝已配置密钥在 UTF-8/UTF-16 中的精确字节匹配；本轮新成员排除 `.env`、命名凭据文件、缓存、日志和数据库 sidecar。检测到敏感内容会拒绝打包，不会篡改证据。该检查不宣称识别未知或编码变换后的全部秘密。
