# 模型接入文档（评审自备 API Key）

> 本项目默认把大语言模型作为 Agent 推理核心，通过 **OpenAI 兼容接口**调用云端模型。
> 评审/复现者需要**自备可用的模型 API Key**；本仓库与提交压缩包**不包含任何真实密钥**。
> 没有 Key 时仍可浏览随包演示数据（历史运行、协作图、项目版本），见《部署与复现指南》。

## 1. 在评测平台获取 API Key 与模型 ID

本项目实测平台为智云产教科创平台（未来杯官方平台）：

1. 登录平台：<https://www.omniedu.com/ai-hub>，进入个人工作台 / 模型服务。
2. 确认目标模型**已开通、有额度或资源包**。平台模型页、服务开通状态与资源包数量以实际账户为准；未开通或余额不足时接口可能返回 403/空模型列表。
3. 在 API Key / 密钥管理页创建密钥。**密钥通常只显示一次**，请立即保存到密码管理器；不要发到聊天、截图、Issue 或仓库。
4. 在平台「模型说明」中复制**准确的模型 ID**（不是界面展示名称）。本项目实测可用的模型 ID 为：

```text
deepseek-v4.1-flash
```

> 历史记录（`docs/model-diagnosis.md`）：旧账号曾遇到 401/403 与空模型列表；充值后可访问的模型列表返回 8 条，原 `deepseek-v4-pro` 不在列表内、调用返回 503「没有可用模型渠道」。因此**请以你自己账户实际可用的模型 ID 为准**，并先用第 3 节探针验证。

5. 官方接口信息（以平台文档为准）：

| 项 | 值 |
|---|---|
| Base URL | `https://aigw.omniedu.com/v1` |
| 聊天接口 | `POST /chat/completions`（OpenAI 兼容） |
| 官方 API 文档 | <https://api-doc.omniedu.com/> |
| 本项目默认模型 | `deepseek-v4.1-flash` |

## 2. 写入本地 `.env`

在项目根目录复制 `.env.example` 为 `.env`（`.env` 已被 `.gitignore` 排除，不会进入仓库或压缩包）：

```powershell
Copy-Item .env.example .env
notepad .env
```

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `AGENTFORGE_LLM_BASE_URL` | 是 | `https://aigw.omniedu.com/v1` | 模型服务地址；须为 OpenAI 兼容接口 |
| `AGENTFORGE_LLM_MODEL` | 是 | 空 | 平台「模型说明」中的准确模型 ID |
| `AGENTFORGE_LLM_API_KEY` | 是 | 空 | 你自备的模型密钥；只保存在本地 |
| `AGENTFORGE_LLM_TIMEOUT` | 否 | `60` | 单次请求超时（秒），允许 5–120 |
| `AGENTFORGE_LLM_MIN_INTERVAL` | 否 | `4.2` | 两次模型请求的最小间隔（秒），用于降低 429 概率 |
| `AGENTFORGE_DOCKER_BACKEND` | 否 | `wsl` | `wsl`：WSL2 专用发行版内 Docker；Linux/已装 Docker 的机器改为 `native` |
| `AGENTFORGE_WSL_DISTRO` | 否 | `AgentForge` | 专用 WSL 发行版名（`wsl` 模式） |
| `AGENTFORGE_TEST_IMAGE` | 否 | `agentforge-test:py313` | 隔离验收镜像标签 |

**安全要求**：`.env` 只放在本地；不要提交 Git、不要打进提交包、不要贴到评测平台或群聊；共享机器使用后请删除 Key 并在平台吊销。

## 3. 三步验证（建议按顺序）

### 3.1 预检（不发送模型请求）

```powershell
.\.venv\Scripts\python.exe scripts\preflight.py
```

- 退出码 **0**：模型配置存在且隔离环境（Docker）可用；
- 退出码 **2**：缺少 Key 或 Docker 不可用；
- 退出码 **1**：运行时异常。

### 3.2 模型协议探针（会真实调用，最多 2 次小请求，可能计费）

```powershell
.\.venv\Scripts\python.exe scripts\probe_model.py
```

探针依次验证：

1. 模型是否返回**只读工具调用**（Function Calling 形态）且参数符合契约；
2. 是否支持 **JSON object 输出**并满足 `{"file":"todo.py","ok":true}` 契约。

- 退出码 **0**：协议兼容，可跑真实闭环；
- 退出码 **2**：不兼容或调用失败（见下表错误分类）；
- 证据写入 `.agentforge/model-probe.json`（已脱敏，不含密钥）。

### 3.3 真实运行（可选，验证完整闭环）

```powershell
.\.venv\Scripts\python.exe scripts\run_live_demo.py --workflow development
```

或直接在界面输入需求并运行。真实运行会消耗 tokens、可能计费；失败与重试全部计入运行记录。

## 4. 错误分类与处理

`probe_model.py` 与运行日志不会记录密钥或供应商原文，只给出类别：

| 现象 / HTTP | 类别 | 含义 | 处理建议 |
|---|---|---|---|
| 401 | `credential_or_authorization` | 密钥无效或未授权 | 重新创建 Key，确认无多余空格/换行 |
| 403 | `permission_denied` / `model_not_enabled` | 账户无权限或模型未开通 | 平台开通模型、确认账户分组 |
| 404 | — | Base URL/路径错误 | 确认以 `/v1` 结尾、接口为 `/chat/completions` |
| 429 | `quota_or_balance` / 限流 | 额度不足或触发限流 | 充值/降低并发/增大 `AGENTFORGE_LLM_MIN_INTERVAL` |
| 503 | `model_channel_unavailable` | 平台没有可用服务渠道 | 换用账户可访问的模型 ID，或等渠道恢复 |
| 超时/连接失败 | — | 网络或代理问题 | 检查网络/代理；必要时调大超时（≤120 秒） |
| JSON/工具校验失败 | — | 模型不满足工具或 JSON 契约 | 换支持 Function Calling + JSON mode 的模型；本项目自动做 1 次格式重试 |

历史真实失败（403/503/429、格式重试、用量未知）均保留在 `docs/model-diagnosis.md` 与各批次验证文档中，未做美化。

## 5. 替换为其他模型服务

项目通过 OpenAI 兼容 `POST /chat/completions` 调用模型，原则上可替换为任意兼容服务（云端或本地开源模型网关）：

1. 修改 `.env` 中 `AGENTFORGE_LLM_BASE_URL` 与 `AGENTFORGE_LLM_MODEL`；
2. 必须支持 **JSON object 输出** 与 **Function Calling（工具调用）**；不支持时探针会失败，平台会明确报错而不是静默降级；
3. 用 `scripts/probe_model.py` 实测通过后再跑真实任务；
4. 本项目预算与请求间隔按平台实测参数设置，其他服务若限流更严，请调大 `AGENTFORGE_LLM_MIN_INTERVAL` 并降低并发。

## 6. 费用、限流与隐私

- **费用**：模型调用由平台按 token 计费；平台测试（pytest/前端测试）不调用模型、不产生费用。
- **限流**：默认请求间隔 4.2 秒；历史对照实验出现过 4 次 HTTP 429，均保留记录。
- **隐私**：需求、候选源码、验收测试摘要与失败输出会发送到你配置的模型服务。不要用真实敏感数据；代码与验收不含密钥。
- **密钥边界**：密钥只经 `Authorization` 头发送；`probe_model.py` 与运行证据对错误信息做脱敏；界面与 API 不回显密钥。

## 7. 评审最小验证路径

| 场景 | 步骤 |
|---|---|
| 无 Key（只体验界面） | 解压提交包 → `一键启动.ps1` → 浏览演示数据中的运行、协作图、项目版本 |
| 有 Key（验证真实闭环） | `部署.ps1` 或手动部署 → 填 `.env` → `preflight.py` 退出码 0 → `probe_model.py` 退出码 0 → 工作台新建/打开一次需求 |
