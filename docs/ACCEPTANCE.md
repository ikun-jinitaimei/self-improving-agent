# v0.2.2 可演示研究候选版验收（2026-10-06）

## 已完成的里程碑

不是“自我学习已经完成”，而是可以运行、解释、测试和组织真实实验的研究底座。
原 V0 保留；新版本提供显式 Agent 循环、工具调用、执行反馈、提交前复核、
确定性评分、可重复题集、失败轨迹、配对成本报告、模型配置与自由 CSV 会话。
新增/修改 Python 逻辑有中文注释，未引入 Agent 框架、多 Agent、UI 或训练依赖。

| 验收 | 实际结果 |
| --- | --- |
| Windows / Python 3.12.4 | 80 项发现，73 离线通过，7 容器集成默认跳过 |
| WSL Ubuntu 24.04.5 / Python 3.12.3 | v0.2.2 的 80 项全部通过，无跳过 |
| Docker Engine 29.8.2 / Linux amd64 | 7 真实边界测试通过 |
| 资源 | 实际 cgroup v2：256 MiB、1 CPU、64 进程 |
| 隔离边界 | 非 root、断网、只读数据/根目录、隐藏 Key 与参考文件、超时清理 |
| 受控重复实验 | 24 题 × 3 次 × 2 策略，144 次尝试完成 |
| 来源与费用 | 固定脚本，不请求 LLM；不是模型准确率或真实账单 |
| 新版真实 API 探针 | 更新凭据后返回 OK；旧 AuthenticationError 仍保留 |
| 真实 dev | 两策略各 36/36，工具错误 0，usage 完整 |
| 真实 holdout | 两策略 36/36、35/36，复核一次预算失败，工具错误 0，usage 完整 |
| 真实自由会话 / 只读 replay | 合成 CSV 返回 rows=20，与独立读取一致；未评分，不计入 benchmark |
| wheel 安装与包外入口 | 全新 0.2.2 Linux 环境离线安装，源码目录外运行通过；analysis 模块包含在包中 |
| 安装包 + Docker 闭环 | 固定回复单题通过，工具执行错误 0；不是 LLM 成绩 |
| GitHub Actions | 5 个任务全部成功，运行 37448879935；包含 7 个实际 Docker 测试 |

Docker 首轮实测发现了短超时创建竞态，不是一次性全通过。修复与失败证据均保留。
create/start/rm 分阶段，清理失败停止执行；没有清理其他用户容器、镜像或数据。
系统与 Engine 位于 D 盘 WSL VHDX，不要求 C 盘 Docker Desktop。

本机交付文件位于 D:\AgentRuntime\release：v0.2.2 source.zip、wheel 和
release-0.2.2.json（文件清单与 SHA256）。只包含审阅过的 60 个公开文件，
没有凭据、虚拟环境或未经审阅的原始轨迹。代码分支在 GitHub，包是本机
交付文件，尚未创建 GitHub Release；两者不是同一种发布状态。
包外演示：20261006T110955Z_ae284725；安装包容器演示：
20261006T111100Z_489aef55，均明确标注 scripted-demo。

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

## 已打通的真实实验入口

本机有效 API Key 已在私有文件配置，真实探针、单题与 dev 均已通过；不要
为了新终端再次复制 Key。用 D:\AgentRuntime\Run-Agent.ps1 统一入口即可。
下面是其他机器第一次配置的通用顺序，密钥不能发到聊天、源码或 Git。

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
- 原版本未合并或改动；候选分支已推送，PR #1 留作独立审阅。
  凭据与未审阅实验日志不在 Git；公开证据仅含白名单汇总与逐尝试账本。

实际 dev 结果没有正确率提升，复核增加约 2.25 倍 token 和 1.62 倍耗时。
冻结 holdout 出现一次复核预算退化；两批完整结果见 LIVE_RESULTS.md。
保留负结果，再据证据选择下一种改进机制，不为演示好看添加未评测功能。
