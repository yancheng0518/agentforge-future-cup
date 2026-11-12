# 交付体验与收尾预算改进

2026-11-10，继续 AgentForge 软件开发工作台方向。用户授权继续实现、参考 `tt-main` 的处理逻辑并留存 GitHub 记录。本批从 `d7a0109` 创建 `codex/delivery-readiness-20261110`，远端为 `yancheng0518/agentforge-future-cup`。

## 本批范围

1. 项目页先展示选中版本的交付概况、验收依据、开发过程入口和下载，然后展示 Web 预览，再提供继续开发。导入版本、历史版本、证据读取失败与事后否决要分别表达；预览运行不等于验收通过。
2. 参考 `tt-main` 分阶段预留预算的思想，在 AgentForge 现有请求、token、时间及验收总上限内，为最后验证和语义审查保留资源。实现仍使用本项目的 Docker、源码哈希和冻结验收门控，不移植数学求解器、数学语料或本机执行工具。
3. 新策略只适用于新建运行，历史记录保持原样。本批不将旧模型结果作为新策略效果证据，也不把模拟传输测试作为真实模型效果评测。

## 验证计划

- 平台回归：覆盖正常收尾、验收失败、审查否决、待执行任务、额度不足、并发预留、格式重试与旧运行兼容。
- 前端：覆盖所选版本证据、加载态、未验收、事后否决及预览生命周期，执行全部前端测试与构建。
- 浏览器：以已有真实项目核查交付顺序、版本切换、查看过程、预览启动/停止与窄屏布局。使用真实应用与服务数据，新增结果单独留存。
- 历史保护：使用已有只读核查器，检查 1,882 份冻结文件及五套数据库旧记录。原 Web 复现包不覆盖。
- GitHub：按后端、界面、验证记录分阶段提交，推送后核对远端 SHA。`tt-main/` 为用户添加的独立参考目录，不进入本批源码提交。

## 执行记录

准备阶段：本地与上一阶段远端分支一致；GitHub CLI 已认证。预检确认 Docker 与既有验收镜像可用，模型配置存在；预检没有发送模型请求。首次控制台输出存在编码问题，原记录 `preflight-before.txt` 保留，另存 UTF-8 复核 `preflight-utf8.txt`。

实施前历史核查通过，记录为 `output/delivery-readiness-20261110/preservation-before.json`。

## 已完成：原总预算内的收尾保护

新运行冻结 `finishing-budget-v1` 策略：最多保留 2 次模型调用、总 token 的 20%（最多 65,536）、1 次验收、1 个决策步骤和总时间的 20%（最多 60 秒）。调用预留不超过总调用数的四分之一；总验收不足 2 次、总步骤不足 4 次时不预留对应额度。全部来自原总预算，未增加上限。没有此策略字段的旧运行继续使用原路径，旧结果不会回填新策略。

开发请求在现有并发锁内检查额度，包含在途 token 预留、未知 usage 和格式重试。主 Agent 上下文可看到冻结的预留策略。资源接近阈值或新的开发请求会侵占预留时，宿主记录事件并进入收尾，停止继续委派、修改文件和调用应用工具。已有未完成委派不能被静默跳过。

收尾复用原完成门控：当前源码、冻结 Web 运行环境、独立 Docker 验收与需要的语义审查必须一致。验收失败、审查否决、候选变化或预算不足都保留为待处理；不通过扩大总额度或略过检查获得成功。过程页和协作图时间线可查看预算预留、开始收尾、收尾未完成及完成事件。

时间阈值只在安全动作边界检查，长调用仍可能达到原总时限；停止本地等待不代表远端请求已取消。固定 token 预留也不保证任何大小的最终审查提示都能装下，不足时不会发送额外请求。本批没有新增真实模型调用，不能由这些平台测试推导模型成功率或费用改善。

## 已完成：版本交付页与真实预览

所选版本的交付概况置顶，展示来源、源码数量、验收状态、下载和开发过程入口。Web 预览位于下一步需求之前，完整需求、源码、冻结依赖与协作设置按需展开。

成功状态要求所选版本的源码指纹、保存回执、冻结验收、来源运行及最新审查对应。导入版本不会因为存在源码或预览可用而显示验收通过；后续复核否决、读取错误、版本错配也会撤下通过结论。开发来源运行定期重新读取。同版本刷新保持预览组件及其本地状态，换版时仍按版本 ID 隔离。

浏览器检查在 `http://127.0.0.1:8005` 进行，使用已有 Flask 项目 `f9d03a54795e45c48d8d30b5991c08ef` 的版本 2。版本 1 正确显示导入未验收，版本 2 显示 14 项冻结验收及模型审查通过；继续改进按钮保持项目路由并聚焦输入框，开发过程入口打开对应历史运行。

新增预览 `9b5c48a88a57421d84e2987cda0aad93` 在项目刷新后保持同一地址且可停止。通过实际页面新增“交付页刷新后仍可操作”，应用日志确认 `POST /api/tasks` 返回 201，页面显示对应工作项。总计 11 次实际 HTTP 请求。界面停止后 iframe 消失，后端回执确认 `container_removed=true`、WSL lease 已释放且客户端已回收。工作项属于预览临时数据，没有修改已保存源码或验收结果。

390px 视口下页面 `clientWidth/scrollWidth` 均为 390，iframe 均为 334；桌面页面宽度和 scrollWidth 均为 1280。临时视口覆盖已恢复，最终浏览器检查未发现控制台 error。浏览器检查修复了实施期间发现的路由跳转和同版本刷新预览状态丢失问题；自动化本身的一次选择器不匹配及 iframe 点击失效记录在结果文件，未当作应用失败或模型失败。

证据：[浏览器结果](../output/delivery-readiness-20261110/browser-verification.json)、[真实预览回执](../output/delivery-readiness-20261110/preview-receipt.json)、[交付页](../output/delivery-readiness-20261110/delivery-desktop-final.jpg)、[实际页面](../output/delivery-readiness-20261110/preview-after-refresh.jpg)、[手机交付页](../output/delivery-readiness-20261110/mobile-delivery-390.jpg)、[手机预览](../output/delivery-readiness-20261110/mobile-preview-390.jpg)。

## 最终验证与复现

- 后端完整回归：**872 passed，278 subtests passed，121.04 秒**，见 [原始日志](../output/delivery-readiness-20261110/pytest-final.txt)。新增预算用例覆盖收尾成功、验收失败、语义否决、未完成任务、并发调用与 token 预留、未知 usage、格式重试、硬时限及旧记录兼容。
- 前端：**110 项测试通过**，TypeScript/Vite 构建通过，见 [测试](../output/delivery-readiness-20261110/frontend-final.txt) 与 [构建](../output/delivery-readiness-20261110/frontend-final-build.txt)。其中新增 11 项交付证据测试及 2 项收尾状态展示测试。
- 独立源码审查完成，确认的同版本刷新问题已修复，并用真实预览刷新与停止验证。
- 最终历史核查通过：**1,882 个冻结文件及五套数据库原记录保持完整**，见 [核查结果](../output/delivery-readiness-20261110/preservation-final.json)。本批新增预览记录单独保存，原 Web 归档不覆盖。
- 本批模型调用增量为 **0**。平台测试采用明确的假传输和测试替身；浏览器使用既有真实候选及新 Docker 预览。没有新模型效果评测，也没有全新电脑复现声明。

平台测试命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

在 `frontend` 目录运行 `npm test` 和 `npm run build`。启动本批界面可运行 `powershell -ExecutionPolicy Bypass -File .\scripts\workbench.ps1 -Port 8005`，已有服务占用该端口时直接访问，不重复启动。打开“项目”，选择已有版本；启动预览会新建有界 Docker 容器，查看历史过程和切换版本不会调用模型。

## GitHub 记录

已分别提交并推送：

- [`c6db226`](https://github.com/yancheng0518/agentforge-future-cup/commit/c6db226)：原预算内的收尾策略、门控与后端测试。
- [`9cebfc9`](https://github.com/yancheng0518/agentforge-future-cup/commit/9cebfc9)：交付概况、预览前置、证据判定、收尾事件展示及前端测试。

最终验证与截图在后续 `docs(delivery): record regression and real preview evidence` 提交留存。每次推送前检查暂存差异和本地配置凭据字节，最终推送后核对远端分支 SHA。分支：[codex/delivery-readiness-20261110](https://github.com/yancheng0518/agentforge-future-cup/tree/codex/delivery-readiness-20261110)。
