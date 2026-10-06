# 参考 Pi，而不是复制功能列表

2026-10-05 查阅官方 [Pi 仓库](https://github.com/earendil-works/pi)
（原 badlogic/pi-mono 链接现重定向至此）。它将模型 API、Agent runtime 和
coding CLI 分层，也提供自动化入口。这些机制适合参考，但不是本项目已达到
相同成熟度的证据。没有复制其源码或把 TypeScript 依赖加入 Python 项目。

| 可借鉴机制 | 解决当前问题 | 本项目落实 |
| --- | --- | --- |
| 模型层与运行层分离 | 请求参数写死在循环里，配置/凭据混淆 | providers.py、公开 JSON 与私有 Key 分离 |
| 运行事件不等于模型消息 | 只能查看最终文件，难以跟踪过程 | on_event、生命周期、JSONL，事件不进入 messages |
| 用户会话与工具循环分离 | 只能跑固定 tasks，难以真实使用 | sessions.py、ask、单 CSV 输入、只读 replay |
| CLI 的机器可读入口 | 其他程序难以订阅运行状态 | --json-events；不是完整 RPC 服务 |

对照来源：[模型层 README](https://github.com/earendil-works/pi/blob/main/packages/ai/README.md)、
[Agent core README](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)。

没有照搬 TUI、session 分支/续跑、技能插件、多 Agent、MCP、OAuth 或多供应商原生
API。当前只实现可验证的 CSV 环境和兼容 Chat Completions，够用后再按证据扩展。
Pi 官方也明确默认继承宿主权限；不能因为参考开源 Agent 就认为代码执行已隔离。
本项目默认采用单独的 Docker 工具后端，本机已验收 7 项真实容器边界测试；
这不代表完整安全审计，也不代表已经达到 Pi 的功能广度。

下一步的标准是：真正回答用户 CSV 问题，能复查失败原因，有同预算真实实验。
不是增加工具数量或声称自我进化。已有工程机制不等于模型能力或学习效果。
