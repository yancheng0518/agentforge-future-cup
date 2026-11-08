# 模型 403 定向诊断 · 2026-11-08

## 最新：充值后已接通可用模型

用户报告已充值后，于 15:52–16:00 重新验证。以下结果覆盖后文“仍阻塞”的历史状态：

1. 原 `deepseek-v4-pro` 不再返回 403，改为 HTTP 503，服务端含义是该模型在 `default` 分组没有可用通道。
2. `/v1/models` 从空列表变为 8 条：`bge-m3`、`deepseek-v4.1-flash`、`deepseek-v4.1-flash-omniedu-384`、`glm-5.2`、`glm-5.3`、`glm-5.3-flash-omniedu-384`、`kimi-k2.6`、`qwen-image`。原 Pro 不在可访问列表中。
3. 仅修改本地模型 ID 为列表中的 `deepseek-v4.1-flash`，保持同一 API Key 和 Base URL。两次真实请求完成工具调用往返与 JSON 校验，合计 564 tokens、5.329 秒。
4. 真实开发工作流首轮通过，真实审查模板正确发现原始项目缺陷；另外的明示合成缺陷实验由真实 fixer 修复并通过 Docker 重测与真实 review。详见 [运行证据](runtime-evidence.md)。

证据为 `.agentforge/model-probe-after-recharge.json`（原 Pro 503）、`model-list-after-recharge.json`（可访问列表）、`model-probe-flash.json`（兼容探测通过）。充值与权限可见状态的变化在本次观察中一致；未获平台后台说明，不能进一步推断唯一的内部授权机制。此前新建 Key 并未消除 403；充值后的同一 Key 配合可访问模型已成功。

当前不需要继续创建 Key 或重装 Docker。默认 Pro 模型本次渠道不可用的事实仍成立，不能把 Flash 的成功算成 Pro 成功。错误诊断已区分授权拒绝、渠道不可用与普通 5xx；提示使用固定安全文本，不输出原始服务端响应或密钥。

## 历史：新密钥复测 · 15:37–15:41

以下保留充值前结果。用户明确同意后在平台创建了 `AgentForge-dev`，用户自行将其替换到本地 `.env`。只在内存比较新密钥与平台可见遮罩的前后缀，结果匹配；没有输出密钥。创建新 Key 是排查“旧 Key 的模型授权未同步”的假设，并非认定旧 Key 已失效。

- 15:37:41（Asia/Shanghai），对 `deepseek-v4-pro` 发出一次最多 256 输出 token 的只读工具探测，HTTP **403**，耗时 149 ms，含义仍是 `This token has no access to model`。未进入工具执行或第二步 JSON 验证。
- 15:40:50，同一新 Key 请求 `/v1/models`，HTTP **200**，`success=true`，`data=[]`，耗时 151 ms。
- 本轮共 **2 次真实 API 请求**（1 次聊天、1 次模型列表）；没有成功生成或真实模型修正。证据分别为 `.agentforge/model-probe-new-key.json` 与 `.agentforge/model-list-new-key.json`。
- 平台 UI 再次确认 DeepSeek-V4-Pro 已开通，准确 ID 为 `deepseek-v4-pro`；其“API接入示例”直接打开官方 `https://api-doc.omniedu.com/`。官方快速入门、[Cherry Studio](https://api-doc.omniedu.com/8450704m0)、[OpenCode](https://api-doc.omniedu.com/8450711m0) 均指向当前 Base URL，无另一套个人工作台网关、租户前缀或模型命名空间要求。
- UI 显示资源包剩余/总量 0/0；资源包页可购买套餐。但在已查官方文档中未找到“必须购买才获得模型权限”的说明，因此不能据此断定充值就能解决 403，也没有购买或变更计费设置。

**结论：** 换 Key 没有修复问题；不能宣称原 Key 无效，也不能宣称新 Key 可用。更可能是平台账户开通状态与网关 token 权限/分组关联或竞赛权益问题，这是推断，尚非平台确认。没有证据支持继续修改本地请求协议或重复建 Key。

### 供平台核查的说明（已准备，未发送）

个人工作台显示模型 `deepseek-v4-pro` 已开通。使用官方 Base URL `https://aigw.omniedu.com/v1`，旧 Key 和新建的 `AgentForge-dev` 都被拒绝。2026-11-08 15:37:41（北京时间）新 Key 调用 `/chat/completions` 返回 HTTP 403，错误为 `This token has no access to model`；15:40:50 新 Key 请求 `/models` 返回 200、`success=true`，但 `data=[]`。本地格式、模型 ID、地址、环境变量覆盖已核对。请协助检查账户模型开通状态是否同步到 API 网关 token 的模型权限/分组，并确认竞赛权益是否已发放、资源包 0/0 是否影响 API 访问。无需在普通聊天或日志中提供完整 Key。

### 恢复验证

平台确认授权修复后，先运行 `.\.venv\Scripts\python.exe scripts\probe_model.py`。退出码 0 且工具往返、JSON 两项都通过后，再启动后端并运行 `scripts/run_live_demo.py --require-repair`。Docker 目前已通过真实隔离与模拟反馈闭环验证，无需重装；真实模式不自动退回 mock。

## 历史：旧密钥定向诊断

本次继续处理用户要求的真实模型闭环，仅调查比赛平台项目配置；未改变 `.env`、密钥或平台权限，没有尝试其他服务商。

## 本地配置排除项

直接在内存比较运行时配置和项目 `.env`，只输出布尔结论：

- `AGENTFORGE_LLM_API_KEY` 在当前进程环境中不存在，Windows User 级同名变量也不存在。
- 运行时使用的密钥与项目 `.env` 相同，没有被旧环境变量覆盖。
- `.env` 没有 BOM、重复密钥行；密钥没有空白、引号、Bearer 前缀、非 ASCII 字符或 Unicode 控制/格式字符。
- urllib 没有启用代理。
- 实际地址为 `https://aigw.omniedu.com/v1`，实际模型为 `deepseek-v4-pro`。

以上只证明程序读取了本地配置，不能单独证明该配置对应当前平台账户的哪一条密钥。没有输出密钥内容、指纹或尾号。

## 两次定向真实请求

1. `GET /v1/models`：HTTP 200，`success=true`，`data=[]`，耗时约121ms。该次接口没有向该令牌返回任何可访问模型。
2. `POST /v1/chat/completions`：使用已确认模型 ID，发送极简“Reply OK.”请求，最大输出8 tokens，显式关闭思考，不包含 tools 或 response_format。仍返回HTTP403，耗时约144ms；响应匹配“当前令牌无权访问该模型”的含义。没有额度或IP白名单限制关键词，但不能仅凭关键词排除平台其他后台配置问题。

请求累计2次。没有成功生成，也没有继续重试或更换模型。原始错误体只在内存分类，没有记录可能回显凭据的内容。

## 当前结论

403 并非仅由本项目的工具调用或 JSON 参数引起，因为不带这些参数的最小请求同样被拒绝。本地密钥格式、进程变量覆盖和代理因素也已排除。

结合“令牌可访问模型列表为空”和“token 无权访问目标模型”，更可能是平台令牌模型授权、账户关联或模型开通同步问题；这是根据响应作出的推断，尚未查到平台内部状态，不能声称已经修复。

主代理继续核查已登录平台 UI。应先确认本地密钥对应当前账户的有效密钥，再检查平台可见的授权/同步机制。若平台没有授权设置，需平台方处理该令牌与已开通模型的关联。不要通过猜测模型 ID、改变网关或持续重试掩盖此问题。

## 官方协议核对

官方快速入门仍要求创建 API Key、开通对应模型，并向上述 Base URL 发送 Bearer 鉴权请求：[快速入门](https://api-doc.omniedu.com/)。聊天端点定义仍为 `/v1/chat/completions`：[接口说明](https://api-doc.omniedu.com/425983562e0)。DeepSeek-V4-Pro 可显式关闭默认思考模式：[思考模式说明](https://api-doc.omniedu.com/8557885m0)。这些文档未提供本次403对应的账户后台修复接口，不能据此臆造授权API。
