# Web 项目真实验证与复现记录

记录日期：2026-11-09。对应分支 `codex/web-projects-20261109`，实施前声明见 [web-projects-plan.md](web-projects-plan.md)，阶段提交见 [github-web-progress-20261109.md](github-web-progress-20261109.md)。本记录依据本批输出和本机 API 的真实回执；开发终态已由 8003 服务核对，修复后的浏览器验证使用最新服务 `http://127.0.0.1:8004`。不把单元测试、浏览器预览或模型自述当作开发验收；8002 旧服务虽然共读运行记录，不作为新 Web 接口验证依据。

## 当前结论

本批完成一项真实 Flask Web 自然开发及一项独立预设缺陷修正。自然候选第一次代码验收即通过，因此**本批没有自然代码失败后自动修正的证据**。独立预设缺陷实验确实发生了 Docker HTTP 验收失败、模型修正、重新验收通过；该证据仅证明明示缺陷条件下的修正闭环。

真实自然任务从导入的健康页扩展为中文工作项看板，包含创建、列表、完成状态、筛选、本地 HTML/CSS/JavaScript 和输入校验。候选通过 AI 生成的冻结 HTTP 验收，以及维护者事前冻结的另一套 HTTP 用例。首次浏览器尝试遇到平台生命周期和自动化工具失败，修复后另一次真实浏览器验证通过创建、完成切换、筛选、刷新、空标题提示、390px 布局及停止清理；两次记录均保留。浏览器证据单列，不从 HTTP 成功推断 JavaScript 交互通过。

当前仅实测 Flask 3.1.3 的有界 Python Web 项目，不表示任意第三方依赖、任意 Web 工程、生产部署或所有需求已经得到验证。

## 固定条件与预算

自然运行和预设缺陷运行均冻结以下配置：

- 模型 `deepseek-v4.1-flash`，服务地址 `https://aigw.omniedu.com/v1`；`temperature=0.1`，`thinking.type=disabled`。
- 适配器版本 `autonomous-7-web-projects`；每次模型输出最多 8,192 tokens，单次请求最多一次已记录的格式重试，请求间隔至少 4.2 秒，单次超时 120 秒。
- 每个运行最多 40 次模型请求、300,000 tokens、24 个主 Agent 步骤、32 个子任务、3 个并行任务、8 次独立验收、900 秒。
- 事前声明的时间上限为 1,800 秒，实际 UI 在首个付费请求前固定为 900 秒；这一收紧已写入 [natural-ui-intent.json](../output/web-projects-20261109/natural-ui-intent.json)。没有在运行中扩大预算或忽略重试成本。

两条运行使用相同模型和预算上限，但任务和起点不同，不构成同任务对照实验；也不用于替代既有单 Agent、固定串行和动态编排的历史实验。

依赖根清单为 `flask==3.1.3`，完整 7 个 wheel 的版本及 SHA 均冻结。配置指纹为：

```text
dependency_id  = 5fc859cd0e2673e5f6b69410ee3c325ce5e1158034a31e08c1815bf11a92b9aa
lock_sha256    = ca796c951c27f2f4f994917034756adf4859920d2bb01aabcfb2d37794a7ed12
image_id       = sha256:294f32c3bce20f4fe2d863d327a0ac5a782fb55c845851353525eca7f13a185c
runtime_sha256 = 0a67ae95bd0b0878db8c3cf512d5e6f3884ce9af764aad37806ee0f390a0df1a
entrypoint     = app.py
```

描述符见 [flask-dependencies-attempt2.json](../output/web-projects-20261109/flask-dependencies-attempt2.json)。依赖联网准备容器没有挂载候选源码、资料或模型密钥；只从官方 PyPI 下载 wheel。安装镜像使用冻结 wheel 离线构建；候选 Python 服务代码仅在禁网 Docker 中运行，不在主机执行。页面 JavaScript 则在浏览器的受限 iframe 中执行，两者的隔离边界不同。

## 自然开发：首个候选通过

运行 ID：`4af425ca8a1e44beb8b5cb2110e5edb0`。项目 ID：`f9d03a54795e45c48d8d30b5991c08ef`。输入版本为 `4ce0390f57a3449ba5dcfb2797acdd52`；发布版本 2 为 `08cecbce616242119dce1a8fcada76c9`。

运行状态为 `succeeded`。真实模型请求 10 次，131,918 tokens，格式重试 2 次；用量完整，没有未知用量请求。最终候选 SHA-256：`1e2318b774ee750fb80c6660e0a9e5cef893ba78c7ff3e7a9c74b2815881376e`。

实际过程：

1. 初始维护者项目只有 `/health` 与首页；独立健康回归先执行 3 项、3 次 HTTP 请求，全部通过，没有模型调用。
2. 主 Agent 整理新需求并冻结验收后，对导入项目执行 14 项 HTTP 验收：8 个断言失败、2 个执行错误、17 次 HTTP 请求。该轮分类为 `imported_baseline`，反映新功能尚未实现，不能计为自然模型候选失败。
3. 主 Agent 选择 `dispatch`，动态创建 `impl-backend-api` 和 `impl-frontend` 两个任务，两者没有依赖、分别写入 `app.py` 和两个 `static/` 文件。实际开始时间为 UTC 08:16:23.247 与 08:16:23.252，结束时间分别为 08:16:55.861 与 08:16:56.053，执行区间确实重叠。
4. 主 Agent 随后选择 `verify`。首个自然候选执行 14 项 HTTP 验收全部通过，21 次 HTTP 请求，退出码 0，隔离回执确认。契约检查与交付质量检查通过，当前候选和冻结运行配置匹配后保存版本 2，原始导入文件未改写。
5. 维护者冻结的独立 HTTP 套件再执行 12 项、38 次 HTTP 请求，全部通过，0 个模型调用。这是另一套验收证据，不计为模型修正轮次。

本轮的控制器动作仅为 `dispatch → verify → finish`。平台提供 `http_request` 工具，但**主 Agent 本次没有选择 HTTP 工具，也没有选择直接编辑动作**；真实 HTTP 请求来自独立验收执行器。不能描述成“模型先主动浏览接口，再按观察修正”。

两次格式重试分别是：第 1 次请求的 intake 输出未满足“每个 Web 测试必须直接请求并断言”的 schema；第 9 次请求的 final review 输出 JSON 语法无效。原始 `format_error`、原始输出与后续请求保留；二者属于模型输出协议失败，不是应用代码测试失败，成本已经计入上述 10 次请求和 token 总量。

证据：

- [自然运行完整导出](../output/web-projects-20261109/natural-run-final.json)：状态、全部事件、模型请求、决策、任务图、两轮验收、审查和发布信息。
- [原始提交意图](../output/web-projects-20261109/natural-ui-intent.json)、[导入内容](../output/web-projects-20261109/natural-import-intent.json)、[最终候选](../output/web-projects-20261109/natural-candidate.json)、[版本 2](../output/web-projects-20261109/natural-revision.json)。
- [健康基线](../output/web-projects-20261109/http-baseline-attempt1.json)、[独立 HTTP 复核](../output/web-projects-20261109/natural-external-attempt1.json)。
- 本机只读 API：`GET /api/autonomous/runs/4af425ca8a1e44beb8b5cb2110e5edb0`。本次与完整导出交叉核对一致。

## 独立预设缺陷：真实失败后修正

运行 ID：`1ea0bc86900e4113809f82c22f66a9b5`，`provenance.kind=seeded_defect`。该实验从已经通过的自然候选复制出独立副本，维护者明确删除 `done=true/false` 过滤分支，保留 `all` 和其他功能。变更前后 SHA、具体删除文本和实验目的均在付费请求前写入 [seeded-declaration.json](../output/web-projects-20261109/seeded-declaration.json)。

故障输入 SHA-256：`9d4e25418ab504331931a956de24e6e537170a8850d31fdd44085084efb8c18f`。冻结验收 SHA-256：`55a14fe57edc41fcca24d5b3b55928156101e319ddfa6a0e6ac94b35b1800e79`。

实际记录为：

1. 先对故障副本执行 12 项冻结 HTTP 测试，38 次真实 HTTP 请求，1 个断言失败、0 个错误、退出码 1，分类为 `seeded_defect_baseline`。
2. 主 Agent 选择 `dispatch`，委派单个 `fix-done-filter` 任务，仅允许修改 `app.py`；模型根据真实失败恢复两个过滤分支。
3. 主 Agent 选择 `verify`，同一冻结套件再次执行 12 项、38 次 HTTP 请求，全部通过，退出码 0，分类为 `seeded_experiment_candidate`；然后选择 `finish`。

最终状态 `succeeded`，真实模型请求 4 次、72,644 tokens、0 次格式重试，用量完整。最终候选哈希与自然开发通过时一致。该实验没有发布为自然项目的新版本，也没有覆盖自然候选或原始证据。

证据：[实验声明](../output/web-projects-20261109/seeded-declaration.json)、[提交内容](../output/web-projects-20261109/seeded-intent.json)、[创建回执](../output/web-projects-20261109/seeded-created.json)、[终态完整导出](../output/web-projects-20261109/seeded-run-final.json)、[修正后的候选](../output/web-projects-20261109/seeded-candidate.json)、[批次运行 ID](../output/web-projects-20261109/live-ids.json)。最终状态与两轮验收已通过本机只读 API `GET /api/autonomous/runs/1ea0bc86900e4113809f82c22f66a9b5` 核对；创建回执仍保留其最初的 `queued` 状态，不能误当作终态导出。

## 失败和成本分类

- **依赖准备平台失败**：首次尝试 `9a5e2027387549f4aa28cbf7e0d517f3` 因 Windows/WSL 绑定输出目录的 chmod 权限失败。模型调用为 0。保留失败日志后改为容器临时目录下载、向输出目录复制字节；第二次 `c4c52f18dcb241c3b0b3c90bfd77f37c` 成功。不能把这次平台修复算作 Agent 自动修复应用。
- **导入功能缺口**：自然运行首轮 `imported_baseline` 失败，模型尚未实现新功能；独立列账。
- **格式重试**：自然运行 2 次，预设缺陷运行 0 次；属于协议校验，所有请求和 tokens 都进入各自预算。
- **自然候选代码失败**：本批 0 次；自然开发 1 条运行成功，不足以估计一般成功率或修正能力。
- **预设缺陷失败与修正**：独立实验 1 条，真实失败后修复通过；不得合并为自然开发的自修复成功。
- **独立 HTTP 复核**：12/12 通过，无模型调用。它覆盖固定 HTTP 语义，不等于完整浏览器验收或全面安全审计。
- **浏览器预览平台失败**：首次预览容器在空闲期退出，生命周期诊断推断为 WSL 重启；表单交互未发出 POST/PATCH。属于平台预览与 UI 自动化失败，不增加自然模型候选失败次数。修复后另建预览完成真实浏览器交互，成功依据与首次失败分开保存。

两条模型运行总计 14 次真实请求、204,562 tokens。没有为追求成功额外重跑自然任务，所有可见失败和重试均保留。

## 界面用法与浏览器证据

使用顺序为：`项目 → 导入文件夹 → Web 项目 → 选择 .py 入口 → 准备依赖 → 导入为版本 1 → 输入需求继续开发 → 查看过程/HTTP 验收 → 返回项目启动预览`。已准备好的同版本依赖可直接复用。`requirements.txt` 单独进入依赖配置，不进入候选可编辑源码；后续开发版本保留冻结依赖。

导入限制为 32 个 `.py/.html/.css/.js/.json` 源文件、每文件 64 KiB、合计 256 KiB；依赖仅接受普通包的固定 `name==version`，不支持 URL/VCS/editable、源码包构建或 npm 构建。入口应在启动时运行 8080 端口服务。Docker 运行期间没有外网连接。

预览在独立本机 origin 的受限 iframe 内显示，有启动、停止、到期和历史回执。最长 600 秒或 128 次 HTTP 请求；数据仅在本次预览容器中保留。预览启动成功不代表开发验收通过；到期时界面隐藏页面并等待服务端停止回执，不把前端计时推断当作已停止证据。

Docker 使用 `--network none`；浏览器使用 CSP 将网络资产、连接和表单来源限制为同源，并通过 sandbox 限制顶层导航和弹窗。这里不声称实现了完整浏览器网络隔离，也不保证阻止 iframe 自身跳转所有外链；浏览器 sandbox 允许 frame 导航自身，参见 [WHATWG iframe 标准](https://html.spec.whatwg.org/dev/iframe-embed-object.html)。预览适用于当前本机受控验证，不据此宣称任意不可信前端的全面隔离。`/data` 和进程内数据都是预览容器的临时状态，停止、到期或重新启动后不保证保留，不等同于已有普通 Python 程序的持久数据功能。

首次预览 ID 为 `939d7c59167d469b972a7c29f05390aa`，绑定已发布版本 2。初始 HTTP 与静态资源通过，但容器在启动约 96 秒后退出，Docker 观察值为 `ExitCode=255`、`OOMKilled=false`；同期 WSL uptime 和 boot ID 支持“实例在后台容器运行期间重启”的平台生命周期原因推断，不能把它写成 Python 应用自然缺陷。Microsoft 说明 systemd 服务不会维持 WSL 实例存活，这与本次观察相容，但官方说明本身不证明具体退出原因，参见 [WSL systemd 文档](https://learn.microsoft.com/en-us/windows/wsl/systemd)。自动化尝试还记录了 iframe 点击目标失效与键盘焦点失效；虽然曾填写表单，没有发出 POST/PATCH，因此创建、完成与筛选交互均未完成。后来通过界面停止预览，原始事件保留。

证据：[首次浏览器诊断](../output/web-projects-20261109/browser-attempt1-diagnosis.json)、[停止状态截图](../output/web-projects-20261109/browser-attempt1-stopped.png)。本次失败分类为 `platform_preview_lifecycle_failure`。

修复活跃预览的有界 WSL 生命周期 lease 后，独立空闲复核通过：预览 `6a219043317842deb042b787008e400f` 的验证器连续 105 秒不发 Docker、HTTP 或 WSL 命令，前后 boot ID 一致，随后 `/health` 返回 200，停止后 `container_removed=true`、lease 状态 `released`、`client_reaped=true`。这是本次验证器的受控空闲窗口，不声称整台机器没有其他后台活动。记录见 [preview-idle-attempt2.json](../output/web-projects-20261109/preview-idle-attempt2.json)，对应当时实现见 [实现快照](../output/web-projects-20261109/preview-idle-implementation.py.txt)。后续又修正预览代理对 CRLF 源文件的指纹处理，旧空闲报告与快照没有改写。

修复后的真实浏览器验证在 `http://127.0.0.1:8004` 执行，预览 ID 为 `c9b4360f835c4cd09ae5c6f400be3f28`，仍绑定同一项目版本 2 和自然候选 SHA。记录 [browser-attempt2-success.json](../output/web-projects-20261109/browser-attempt2-success.json) 的 `passed=true`，0 次模型调用；实际共 17 次 HTTP 请求（包含初始就绪检查与静态资源），其中两次新增 `POST /api/tasks` 返回 201，一次完成切换 `PATCH /api/tasks/1` 返回 200，全部/已完成/未完成的筛选 GET 均返回 200。

浏览器实际观察到两个不同标题按优先级排序、已完成仅显示第一项、未完成仅显示第二项；空标题显示前端错误且没有发送 POST；重新加载仍保留同一个容器内的数据。390px 视口下 iframe 的 `clientWidth=334`、`scrollWidth=334`，没有横向溢出。通过界面停止后回执确认容器已移除、WSL lease 已释放、客户端已回收。刷新保持状态只证明同一容器内状态，不表示停止重建后持久化。

高层 iframe 点击/焦点工具的 stale target 失败和不支持的键盘 API 尝试均保留；成功操作使用 CUA 文档支持的 CDP 普通鼠标点击，依据新截图定位，没有修改页面 DOM 或直接写入应用状态。它们是 UI 自动化传输问题，不是候选代码测试失败。截图见 [全部工作项](../output/web-projects-20261109/browser-crud-all.png)、[已完成筛选](../output/web-projects-20261109/browser-filter-completed.png)、[未完成筛选](../output/web-projects-20261109/browser-filter-active.png)、[390px 视口](../output/web-projects-20261109/browser-mobile-390.png)、[空标题错误](../output/web-projects-20261109/browser-empty-title.png)、[成功与停止](../output/web-projects-20261109/browser-success.png)。

另一次真实 UI 导入通过文件选择器选择 `app.py` 与 `requirements.txt`，界面自动识别 Web 模式，并选用已经真实准备好的 Flask 依赖版本，成功导入项目 `ad542621ac494031bdaaa4ce47b12ad0` 的版本 1（`9ed7a0605d5b4131b3d28fff5406ad68`）。`requirements.txt` 没有混入可编辑源码，导入版本状态仍是 `unverified`。本次 0 模型调用、0 候选执行、没有新下载 job；它验证了文件选择、依赖复用与导入，**没有实测在浏览器点击“准备依赖”后完成新下载**。记录见 [browser-import-result.json](../output/web-projects-20261109/browser-import-result.json)，截图为 [导入准备](../output/web-projects-20261109/browser-import-ready.png) 与 [导入成功](../output/web-projects-20261109/browser-import-saved.png)。

## 本机复核与待验证范围

早期完整后端回归 799 项及 278 个子测试、Web 专项 198 项通过，原始日志保留。加入生命周期修复后的完整回归为 **822 项测试及 278 个子测试通过**，但这次全量运行发生在最后的 CRLF 指纹小修之前，不能描述为最终代码再次全量通过。最后修改后，CRLF、生命周期、API 与工具相关的 **71 项定向回归通过**；CLI 快照覆盖修复另有 **11 项测试通过**。前端仍为 97 项测试与 TypeScript/Vite build 通过。原始日志见 [完整回归 822](../output/web-projects-20261109/pytest-final.txt)、[最终定向回归 71](../output/web-projects-20261109/preview-lifecycle-final.txt)、[CLI 快照回归 11](../output/web-projects-20261109/cli-snapshot-fix.txt)、[前端测试与构建](../output/web-projects-20261109/frontend-final.txt)，早期日志见 [799 项](../output/web-projects-20261109/pytest-platform-final.txt) 与 [198 项](../output/web-projects-20261109/web-platform-final.txt)。模拟传输单元测试验证的是平台协议，不能冒充真实模型或 Docker 应用执行。

在本机已经具备冻结依赖目录和对应 Docker 镜像的条件下，可重新运行维护者 HTTP 套件；以下操作不调用模型，输出必须使用新文件名，候选不会在主机执行：

```powershell
.\.venv\Scripts\python.exe scripts/verify_web_candidate.py `
  --candidate-json output/web-projects-20261109/natural-candidate.json `
  --web-json output/web-projects-20261109/natural-revision.json `
  --acceptance benchmarks/web-projects/acceptance_external.py.txt `
  --output output/web-projects-20261109/natural-external-recheck-NEW.json
```

这是复核命令说明，不表示已经执行该 `NEW` 尝试。重新调用模型可能产生不同候选和费用，不保证与本次输出逐字相同。每次新尝试必须保留新运行 ID 和输出，不能覆盖本批记录。

既有记录保持原位，初始历史保护检查见 [preservation-start-check.json](../output/web-projects-20261109/preservation-start-check.json)。真实模型运行后的 [preservation-after-live.json](../output/web-projects-20261109/preservation-after-live.json) 已确认原有 1,882 个文件及五套数据库保护检查通过，使用只读输入，0 模型调用、0 Docker 调用。该检查对应其记录时间，不代替后续归档时的最终核验。本批随后已完成新归档、独立核验及本机独立目录恢复；归档与后验报告分开保留，见下文。

最终历史核对：[preservation-final.json](../output/web-projects-20261109/preservation-final.json) 确认原有 1,882 个文件及五套数据库旧行完整。当前 27 条 UI 记录（17 旧主流程 + 10 动态）、4 个项目、7 个版本；新导入检查项目没有调用模型。


## 归档、恢复与封包后复测

新包 `output/agentforge-web-projects-20261109.zip` 实际为 30,231,457 字节，SHA-256 `bc1cf826adf70522dc01c744cdcdf4a32e5e61c3ded5ee64b7c6605f29afeb90`。清单列 1,245 个成员（另有 manifest），含 22 份 SQLite 副本（6 可恢复、16 历史快照）、成功和失败依赖/预览/模型证据、官方 wheel 与四个未改写旧归档。实际密钥字节和递归 ZIP/wheel 内容扫描通过；不包含 `.env`、WSL 磁盘、Docker 镜像或宿主环境。

正式打包、独立核验及恢复第一次均成功。恢复到此前不存在的 `D:\未来杯\artifacts\private\web-restore-20261109`，全部成员和数据库摘要通过，详见 [独立核验](../output/web-projects-packaging-20261109/verify-attempt1.json)、[恢复报告](../output/web-projects-packaging-20261109/restore-attempt1.json) 和 [复现说明](web-projects-restore.md)。恢复脚本本身 0 模型、0 Docker、0 安装、0 候选执行，不恢复旧浏览器会话。

封包后真实 Docker 复测 `external-cd1f6d4041894e13a7bf3f1286dd62ab` 使用恢复出来的候选、Web 配置和冻结维护者验收；执行器和依赖 bundle 来自当前工作区，Docker 使用本机原镜像。12/12 通过、38 次 HTTP 请求、0 模型调用，源码与验收哈希和封包前一致。它验证恢复后的输入可由现有环境重新执行，不是全新机器/从零安装，也不是在恢复目录启动整套工作台。见 [结果](../output/web-projects-20261109/restored-http-attempt1.json) 和 [12 份原始执行文件清单](../output/web-projects-20261109/post-archive-http-recheck/evidence-manifest.json)。这些后验结果与最终文档在 ZIP 之外追加；原 ZIP 哈希保持不变。

另保留维护者预检注记更正：包装脚本曾在确认状态前写出“已取消”注记，实际预检已自然成功完成，进程身份检查拒绝停止，未终止任何进程。原错误注记、完成回执及 [更正依据](../output/web-projects-20261109/archive-preflight-adjudication.json) 并存；不把它算成取消的预检、模型失败或应用自然缺陷。

恢复后最终只读保护核对也已通过：[preservation-after-restore.json](../output/web-projects-20261109/preservation-after-restore.json) 再次确认原有 1,882 个文件未变、五套数据库原行完整；此报告同样在封存包之外。
