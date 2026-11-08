# 环境检查与模型验证记录

检查日期：2026-11-08（Asia/Shanghai）。此记录区分本机观察、外部 API 实测与尚未验证项，不把模拟适配器或 mock 测试视为比赛真实闭环。

**最新更新：** 下文保留初次检查历史。WSL 3.0.3、`AgentForge` Ubuntu 22.04 与 Docker Engine 29.9.3 已真实安装，容器边界和验收探针通过。用户充值后模型列表已返回 8 条，原 Pro 渠道返回 503，改用列表中的 `deepseek-v4.1-flash` 后完成真实工具往返、JSON、开发首轮通过，以及明示合成缺陷的真实修正/再验收。见 [安装记录](sandbox-setup.md)、[运行证据](runtime-evidence.md) 和 [最新状态](implementation-status.md)。此前“未安装”“新 Key 仍 403”均为历史快照，不是当前结论。

## 已观察到的本机条件

- 工作区最初仅有《项目策划案.md》《交接提示词.md》和赛事说明会 PPT，没有应用源码或项目级 AGENTS.md。
- Python 命令位于 `C:\Python313\python.exe`，Node 位于 `H:\nodejs\node.exe`，npm 位于 `H:\nodejs\npm.ps1`，Git 位于 `C:\Program Files\Git\cmd\git.exe`。具体版本、依赖安装与构建结果以 README 和后续实际运行记录为准。
- Windows 11 家庭中文版，版本 `10.0.22000`，build `22000`，64 位。
- `docker` 和 `podman` 不在 PATH；检查过的 Docker/Podman 标准系统安装位置、用户安装位置以及 `D:\Docker`、`H:\Docker` 均不存在。
- `wsl --status` 返回默认版本 2，并明确提示“未找到 WSL 2 内核文件”；`wsl --list --verbose` 返回没有已安装的发行版。
- `LxssManager` 与 `vmcompute` 服务运行中，`HypervisorPresent=true`。CPU 查询的虚拟化字段为 false；在 hypervisor 已存在时，不据此断言 BIOS 虚拟化已关闭。
- 具名检查的 `DOCKER_HOST`、`DOCKER_CONTEXT` 均未配置。

因此，本次检查没有发现可实际运行候选代码的受控容器环境。应用必须显式阻塞自动验收；不回退为在 Windows 宿主机执行模型生成代码。当前 Docker 文档对 Windows 11 列出的最低 build 为 22631，高于本机 build 22000，直接安装最新版 Docker Desktop 的兼容性尚未成立。未安装或启动任何新系统组件，未修改系统更新设置。

参考：[Docker Desktop Windows 安装要求](https://docs.docker.com/desktop/setup/install/windows-install/)、[容器执行与资源限制](https://docs.docker.com/engine/containers/run/)。

## 模型配置检查与历史真实请求

检查只输出相关环境变量是否存在，不输出值。当前进程未继承模型密钥；Windows User 级 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_MODEL` 存在。具名检查的 `OPENAI_API_KEY`、`OMNIEDU_API_KEY`、`ANTHROPIC_API_KEY`、`AGENTFORGE_API_KEY`、`LLM_API_KEY` 未配置；这不代表扫描或确认了所有软件的凭据。

根据用户要求核实模型接入，使用既有 User 级密钥在内存中执行了一次小额上限请求：

- 请求地址：`https://api.deepseek.com/chat/completions`。
- 配置模型：`deepseek-ai/DeepSeek-R1-0528-Qwen3-8B`。
- 目的：最多 160 输出 token，强制请求只读 `read_project` 工具，成功后才计划发送工具结果并验证短 JSON 响应。
- 实测结果：HTTP `401`，`Authentication Fails`，耗时约 `0.551` 秒。
- 实际请求次数：**1**。由于鉴权失败，没有执行第二次请求，没有返回工具调用、JSON 内容或有效 token 用量。
- 没有确认实际扣费；没有将凭据写入项目、日志或文档；没有猜测或轮询第三方网关。

该模型名与当前 DeepSeek 官方文档列出的 `deepseek-flash`、`deepseek-v4-pro` 不一致。不能把已有变量名当作服务商确认依据，也不能自动将它绑定到 DeepSeek 官方服务。

**用户随后明确选择在比赛平台新建 API Key。** 当前项目已移除对既有 `DEEPSEEK_*` 变量的自动回退，只接受本项目明确配置的 `AGENTFORGE_LLM_*` 变量；默认 Base URL 为智云，模型 ID 保持空白，需填写已开通的准确 ID。以上 401 是先前备用接入检查的历史证据，不代表新建比赛平台密钥的结果。服务端 `.env` 已加入忽略规则，密钥不得进入前端、版本库或本记录。

参考：[DeepSeek 官方 API 入口及模型名](https://api-docs.deepseek.com/)、[工具调用协议](https://api-docs.deepseek.com/guides/tool_calls/)、[JSON 输出协议](https://api-docs.deepseek.com/guides/json_mode/)。协议文档可用不等于本地凭据可用。

## 智云接入核查

重新读取官方文档，确认 Base URL 为 `https://aigw.omniedu.com/v1`，聊天端点为 `/chat/completions`，需要 API Key 并开通对应模型服务。ChatCompletions 参数定义包含 `tools`、`tool_choice`、`tool_calls` 和 `response_format`；部分 schema 字段类型与说明不一致，应用接入仍需实测。

本次没有登录或操作账户开通页面，没有核实账户额度、费用、限流、实际模型权限，没有创建智云 API Key。上一份策划案记录的“未开通、资源包为 0”是前次观察，不能视为本次实时账户状态。

参考：[智云快速入门](https://api-doc.omniedu.com/)、[ChatCompletions 接口](https://api-doc.omniedu.com/425983562e0)、[官方参数 Markdown](https://api-doc.omniedu.com/425983562e0.md)。

### 用户配置比赛密钥后的补充实测

用户随后表示已在本地 `.env` 填写比赛平台密钥，且已开通平台模型，授权继续使用比赛模型验证。2026-11-08 14:20 左右（Asia/Shanghai）执行如下最小检查：

- `GET https://aigw.omniedu.com/v1/models` 两次均返回 HTTP 200，但未返回可用模型 ID。第二次仅用于核查响应结构，其顶层字段为 `data`、`object`、`success`。
- 按[官方 Claude Code 集成示例](https://api-doc.omniedu.com/8450710m0)选择 `deepseek-v3-2-251201`，发送一次最多 256 输出 token 的只读工具探测。结果 HTTP 403，约 146 ms，未产生工具调用。
- 按[官方 LangChain 集成示例](https://api-doc.omniedu.com/8450733m0)选择 `doubao-seed-code-preview-251028`，同类探测结果 HTTP 403，约 149 ms，错误文本仅在内存分类为 `credential_or_authorization`，未输出原始响应或密钥。
- 累计 **4 次 API 请求**：2 次模型列表 GET、2 次聊天 POST。由于均被拒绝，没有进行工具结果回传或 JSON 响应第二步；停止进一步尝试。
- 未核实价格、额度和实际扣费；没有成功可调用模型证据。已开通模型的用户陈述不能替代该 API Key 的调用权限验证，具体拒绝原因仍需在平台核查。

以上两个旧文档 ID 的脱敏证据已归档至忽略目录 `.agentforge/model-probe-history-20261108T062406Z.json`，含两次失败与累计请求计数。旧文档示例 ID 不能替代平台当前模型列表。

### 平台确认准确模型 ID 后复核

主代理随后在用户已登录的平台模型页面核实：10 个模型显示已开通，DeepSeek-V4-Pro 的准确 ID 为 `deepseek-v4-pro`，页面标示结构化输出和工具调用能力。平台模型说明显示 TPM 60,000、RPM 15；资源包为 0 及页面价格为 0 不能作为免费调用承诺。这些页面信息来自本次 UI 核查，不是从旧 API 示例推测。

据此于 2026-11-08 14:24 左右，将 `.env` 的模型 ID 行改为 `deepseek-v4-pro`，保持密钥不变，再做 **1 次**最多 256 输出 token 的工具探测。依[智云思考模式文档](https://api-doc.omniedu.com/8557885m0)显式关闭思考模式，以控制探测预算。

结果仍为 **HTTP 403**，约 **147 ms**。本次在内存解析到服务端错误含义：**当前令牌无权访问该模型**；服务端错误 `code` 为空。原始消息中的模型名、请求标识和所有可能的密钥片段已脱敏，未输出或保存密钥。由于此证据明确指向令牌访问权限，未执行第二步，不再尝试其他模型。具体应调整哪项平台配置仍需核查，不能仅凭此响应推断计费或模型开通失败。

最新证据为 `.agentforge/model-probe.json`，先前历史保留在上述归档中。比赛平台累计 **5 次 API 请求**：2 次模型列表 GET、3 次聊天 POST；所有聊天请求均未进入成功生成。当前 `.env` 选择 `deepseek-v4-pro`，模型 ID 已由平台确认，**该密钥的模型调用仍未验证通过**。

## 验证层次与后续复现条件

- **平台单元测试**：`tests/test_runtime.py` 在临时目录复制受控示例、知识资料和验收测试，使用 fake transport 检查文件边界、差异、模型协议、错误脱敏、容器命令约束和基线冲突。不会连接模型或启动 Docker，也不会导入/执行候选 `todo.py`。
- **模拟模式**：确定性模型适配器的首轮筛选缺陷是明确标注的预设故障，不能作为真实 LLM 自我修正证据。
- **真实 API**：有历史备用服务 HTTP 401，以及比赛平台三次 HTTP 403 的失败证据；其中平台当前准确模型 ID 已确认，但令牌无访问权限。尚无成功工具调用或多 Agent 模型闭环。
- **真实容器验收**：尚未运行。需先完成可用 Linux 容器引擎、验收镜像及隔离预检，再执行独立验收测试。
- **独立验收边界**：验收文件放在候选目录外并以只读方式挂载；随后实现已将可信测试驱动与候选 Python 分到父子进程，通过受限 JSON RPC 检查行为，候选 stdout 不直接作为测试结果。父子进程仍在同容器同 UID，不能对任意恶意代码宣称强对抗隔离。协议单元测试通过，真实容器验证待完成。

复现真实闭环至少需要：明确服务商、Base URL、模型 ID 与有效密钥；通过一次真实只读工具往返及 JSON 输出校验；通过容器隔离预检；保留一次真实验收失败、反馈修正及再次验收的记录。缺少任何一项，都应继续标注待验证或阻塞。

## 本轮平台测试结果

2026-11-08 使用项目虚拟环境执行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_runtime.py -v
```

结果：**25 项通过**，测试报告耗时约 2.603 秒。首次运行曾发现候选应用缺少符号链接检查、模型工具参数校验不完整；修复后复测通过。新增回归项也确认没有自动读取既有服务商凭据、Agent 工具权限受限。以上是平台单元测试结果，不代表 Docker 验收或真实 LLM 工作流通过。

最终集成检查：`python -m pytest -q` 为 **45 passed, 22 subtests passed**（10.51 秒）；包括 25 项运行时、14 项引擎/API、6 项验收代理协议测试。前端 TypeScript + Vite 构建通过，浏览器交互及桌面/398px 布局完成检查。详见 `implementation-status.md`。

随后只读查看平台 API Key 创建/编辑界面：仅提供名称字段，未见可配置的模型访问授权字段。未创建、修改或删除平台密钥，没有调整账户或模型服务状态。403 的下一步应由平台检查令牌后端授权，不能把问题直接解释为用户忘记开通模型。
