# Web 项目复现包与恢复边界

本批脚本已实现，**尚未运行真实打包或恢复**。目标新包为 `output/agentforge-web-projects-20261109.zip`；是否已生成、实际成员数和 SHA-256 必须以本批 `archive-manifest.json`、ZIP 边车及独立恢复报告为准。此文不把脚本准备完成写成已验证复现。

本批脚本不调用模型、Docker、安装器或候选代码，不改写源数据库。模型开发、HTTP 验收、平台预览和浏览器操作各有自己的证据；仅恢复历史不重复这些实验，也不产生模型费用。

## 停稳后生成独立归档

等待真实运行全部终态，完成浏览器检查并停止全部预览、依赖准备和新请求后，才可执行：

```powershell
python output/web-projects-20261109/package_reproduction.py
python output/web-projects-20261109/restore_reproduction.py output/agentforge-web-projects-20261109.zip
```

第二条仅核验，不解压。不要用旧导出器的固定大小限制代替本批核验器。新包仍采用 `agentforge-local-reproduction-v1` 清单结构，`purpose` 为 `agentforge-web-projects-reproduction-v1`；增加的 Web 字段不会写入或迁移任何旧 ZIP。

本批 `output/web-projects-20261109/live-ids.json` 是真实运行清单，格式为：

```json
{"schema":"agentforge-web-live-ids-v1","runs":[{"run_id":"实际32位运行ID","data_dir":".agentforge/autonomous","kind":"natural"}]}
```

维护者应把本批全部成功、失败和后续尝试加入清单，预设缺陷使用 `kind:"seeded_defect"`。脚本按清单及只读数据库计算数量，不固定本批成功数；验证改动前 25 条 UI 运行的 ID 与内容完整保留，并要求新增 UI 运行与清单完全一致。没有 Store 运行的维护者基线 HTTP 检查，作为验证目录保留，不伪装成模型运行。

ZIP、边车、归档报告及 `package-database-snapshots` 均独占创建；已存在即拒绝，不覆盖、不删除、不自动重试。失败产生的部分目录或 ZIP 保留且不得当作认证产物。应记录失败原因，再由维护者明确决定后续新批次，不通过删除失败记录达到成功计数。

## 只恢复到尚不存在的目录

```powershell
python output/web-projects-20261109/restore_reproduction.py output/agentforge-web-projects-20261109.zip --extract-to D:\AgentForge-Web-Restore-NEW --report D:\AgentForge-Web-Restore-check-NEW.json
```

目标父目录需要已存在，目标目录与报告路径必须尚不存在。脚本先核验完整成员清单、边车与嵌套 ZIP/wheel，然后逐文件独占写入；核对每份 SQLite 别名、副本、完整性及逐表摘要。输入变动或任何失败均保留部分输出，不能在同一路径静默覆盖重跑。

恢复过程只读 JSON、源码字节和 SQLite。旧 ZIP 与 wheel 保持封存文件，不递归解压到工作区，不导入、不执行、不安装其中内容。不要在宿主机运行候选的 `app.py`、冻结验收、依赖 wheel 或从证据复制出的命令。

`.agentforge/agentforge.sqlite3`、`.agentforge/autonomous/agentforge.sqlite3`、项目的两份数据库及 `.agentforge/web/requests.sqlite3` 是当前状态的一致性副本。清单 `role:historical_snapshot` 的改动前数据库、上一批快照及额外备份只供比较，不应覆盖当前数据库。副本使用 SQLite backup API 从只读事务取得，再仅对新副本设置 DELETE journal；不混拷 live WAL/SHM。打包前后检查数据库内容、文件清单及成员哈希，拒绝中途写入；这依赖任务已停稳，不宣称跨多个数据库具有原子事务。

## Docker 镜像与新机复现

本包**不导出约 180 MB 的 Docker 依赖镜像**，不包含 WSL 磁盘、Python 虚拟环境或 Node 依赖。wheel、完整锁、Dockerfile、下载器、官方来源回执与镜像 ID 作为证据保留。

原机器仍有 manifest 指定的原始 image ID 时，可在独立恢复工作区重新配置服务后检查该镜像；只有实际通过 Docker 验收才算重新运行成功。历史预览 URL、临时访问令牌与端口属于旧会话，解压不会恢复容器或浏览器会话，需要通过平台新建预览并生成新回执。

没有原镜像的机器，即使用同一个 Dockerfile、wheel 和锁重新构建，也不保证得到相同 image ID。应通过平台重新准备依赖，导入为新 Web 项目/新冻结运行配置，再做新开发验收和预览验证。不得修改旧 manifest、project revision 或 run 的 image ID 使它看起来“原样复现”。本批没有验证换机离线恢复，也没有验证原镜像导出/导入。

需要重新运行工作台时，按 `README.md` 重建宿主依赖，在新目录单独配置自己的 `.env`、Docker 与模型。历史浏览不等于调用模型；再次提交需求才产生新请求与可能的费用。新的模型输出和费用不保证与历史相同。

## 保留范围与证据身份

沿用上一批的旧运行、项目、审查与历史证据目录，并增加：

- `.agentforge/web`：成功与失败依赖尝试、下载/构建回执、官方 wheel、锁、依赖 bundle、Web 注册表、完整预览事件与请求。
- `.agentforge/web-validation-20261109`：维护者基线和外部 HTTP 验收，包括失败与成功结果。
- `.agentforge/web-preview-platform-20261109`、`output/web-preview-platform-20261109`：独立平台预览检查。
- `output/web-projects-20261109`：本批复现脚本、模型运行清单、审计、保存检查、UI/浏览器证据及 `browser-attempt1-diagnosis` 等失败诊断。

自然任务的导入基线失败、自然候选失败、AI 生成验收失败、模型格式重试和明示预设缺陷分开读取；HTTP 工具 4xx/5xx 不自动算作自然开发失败。成功 seeded control 需要真实失败基线与修改候选通过，两者仍是预设实验，不是自然修正率。

每次真实模型运行的 `implementation-manifest.json`/`implementation/` 保存它当时使用的宿主实现，包括 `sandbox/web`；候选、冻结验收和 runtime 配置另行保存。打包时的源码可能包含随后修复的预览保活和失败状态处理，不能把新源码冒称为旧模型运行当时的实现；后续平台与浏览器修复验证应查看其对应的新回执。

四个旧包（core、chat、协作图、project-workspaces）及边车原样嵌入，分别按原始清单/schema 与完整成员哈希校验，不改旧包。不要再次解压这些旧包覆盖当前恢复目录。core 包旧 WAL/SHM 与协作图包两份历史日志，仅凭固定整包哈希和成员哈希允许继续封存；新成员没有此例外。

## 扫描与资源限制

新成员排除真实 `.env`、命名凭据文件、WSL/磁盘/安装器、环境和缓存、日志及数据库 sidecar。wheel 是受控包 ZIP：逐层检查路径、重复项、链接、加密标志、大小和全部解压内容；包内合法扩展模块、`secrets.py` 或公开证书不因名称被误认为本机密钥，也不会被安装。所有成员与嵌套内容均拒绝本机配置密钥的 UTF-8/UTF-16 精确字节匹配；不宣称识别未知或编码变换后的所有秘密。

本批上限为单归档成员 512 MiB、总展开 2 GiB、5 万成员、manifest 8 MiB；每个 wheel 仍限定压缩 32 MiB/展开 256 MiB，嵌套检查限定 8 层、累计展开 4 GiB/25 万成员。上限只适配完整官方轮子和多份历史证据，不允许无限解压。SHA-256 检测内容变化，不证明作者身份。
