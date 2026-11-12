# 第三方组件与自定义实现

版本来自本机实际安装与锁文件；正式提交应随源码保留依赖许可证，审查传递依赖。这里不代表法律意见或完整 SBOM。

- React / React DOM 19.3.1：MIT；工作台渲染与状态管理。
- Vite 8.3.6、@vitejs/plugin-react 6.2.1：MIT；本地构建和开发代理。
- TypeScript 7.0.2：Apache-2.0；前端类型检查。
- FastAPI 0.162.2：MIT；服务端 HTTP API。
- Uvicorn 0.54.0：BSD-3-Clause；本地 ASGI 服务。
- HTTPX 0.28.1：BSD-3-Clause；API 测试客户端依赖。
- pytest 9.2.0：MIT；平台回归测试。
- python-dotenv 1.2.4：BSD-3-Clause；当前环境依赖，配置运行时目前使用小型标准库读取器。
- Python 标准库 sqlite3：本地状态保存；SQLite 项目为公共领域。
- Docker/Python 基础镜像：隔离执行设计；已在 WSL2 专用 `AgentForge` 发行版（Docker Engine 29.9.3）与本机验收镜像上实测可用；基础镜像 `FROM` 仍为浮动 tag，正式提交前需锁定 digest。

版本与 Python 许可证元数据已从安装包检查。前端许可证以对应 node_modules 包 LICENSE 为准。开源组件不等于团队原创工作。

项目自定义部分包括：能力与工具权限双重筛选、低负载优先与专用 Agent 排序、失败重新分派、验证子图并行、独立验收与模型审查联合门控、最多两轮修正、候选哈希失效、SQLite 原子节点检查点与配置版本快照。

模型服务是外部托管 API；没有将模型权重打包。模型调用预算、费用、服务权限与许可由实际提供方约定。`deepseek-v4.1-flash` 已通过真实协议探针与多批真实运行。项目源码自身的发布许可证尚未最终确定，不自动添加授权范围。
