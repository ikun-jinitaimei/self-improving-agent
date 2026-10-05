# v0.2.1 可演示研究候选版验收（2026-10-06）

## 已完成的里程碑

不是“自我学习已经完成”，而是可以运行、解释、测试和组织真实实验的研究底座。
原 V0 保留；新版本提供显式 Agent 循环、工具调用、执行反馈、提交前复核、
确定性评分、可重复题集、失败轨迹、配对成本报告、模型配置与自由 CSV 会话。
新增/修改 Python 逻辑有中文注释，未引入 Agent 框架、多 Agent、UI 或训练依赖。

| 验收 | 实际结果 |
| --- | --- |
| Windows / Python 3.12.4 | 63 离线通过，7 容器集成默认跳过 |
| WSL Ubuntu 24.04.5 / Python 3.12.3 | 70 全部通过，无跳过 |
| Docker Engine 29.8.2 / Linux amd64 | 7 真实边界测试通过 |
| 资源 | 实际 cgroup v2：256 MiB、1 CPU、64 进程 |
| 隔离边界 | 非 root、断网、只读数据/根目录、隐藏 Key 与参考文件、超时清理 |
| 受控重复实验 | 24 题 × 3 次 × 2 策略，144 次尝试完成 |
| 来源与费用 | 固定脚本，不请求 LLM；不是模型准确率或真实账单 |
| 新版真实 API 探针 | AuthenticationError，失败记录已脱敏保存 |
| GitHub Actions | 配置已提供，但未上传运行，不算云端通过 |

Docker 首轮实测发现了短超时创建竞态，不是一次性全通过。修复与失败证据均保留。
create/start/rm 分阶段，清理失败停止执行；没有清理其他用户容器、镜像或数据。
系统与 Engine 位于 D 盘 WSL VHDX，不要求 C 盘 Docker Desktop。

## 怎样演示

在安装 openai==3.13.0 的 Python 环境、项目目录内运行：

```powershell
python -X utf8 demo.py demo --split dev --task-id task_dev_7_monthly
python -X utf8 -m unittest discover -s tests -v
```

第一条展示候选故障 → 评分失败 → 复核计算 → 恢复，同时显示增加的调用。
先指出终端的 SCRIPTED 标签，再看 comparison.md 和两个策略的轨迹。
不能把人为故障导致的 75%→100% 写成 DeepSeek 提升。
完整操作见 DEMO_RUNBOOK.md，本机 D 盘入口和安装资料另有本地 README。

## 目前唯一阻挡下一轮真实实验的输入

需要有效 API Key，必须在本地更新，不发到聊天、源码或 Git。

```powershell
python -X utf8 demo.py configure
python -X utf8 demo.py probe
python -X utf8 demo.py run --policy both --split dev --task-id task_dev_7_monthly --max-api-calls 12
```

依次进行，不在 probe 失败后继续运行下一条。环境变量优先于私有文件；
若配置文件更新后仍认证失败，检查当前进程是否设置了旧的 Key。
probe 通过仅证明认证/端点可用；单题还需验证工具调用协议，之后才扩大 dev。
请求上限不是人民币预算，不能假定 150 元必须用完，也不能在无 usage 时估算实测费用。

## 明确保留的限制

- 复核是测试时策略，没有参数学习、记忆学习、SFT 或 RL。
- 合成题只有 4 类；holdout 是新随机种子，不是跨领域泛化证据。
- 容器验收不等于生产安全审计；输出捕获未做宿主机内存硬限制，镜像使用可变标签。
- WSL 测试不等同所有 Linux 发行版、所有 Python 版本或实体服务器均通过。
- 未合并原版本、未向 GitHub 发布本候选版；凭据与未审阅实验日志不在交付包。

下一步遵循 EXPERIMENT_PLAN.md：真实单题 → dev 对照 → 冻结 → holdout → 失败分析。
若复核不提升或只增加成本，应报告负结果，再据证据选择下一种改进机制。
