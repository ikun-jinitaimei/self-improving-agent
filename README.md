# Self-Improving Agent Lab

一个可读、可复现的工具调用 Agent 实验项目：CSV 任务 → 模型决策 → 工具执行 → 反馈 → 候选答案 → 可选复核 → 确定性评分 → 轨迹与配对报告。

当前是独立研究候选版 v0.2.1：可接受自由 CSV 问题、配置兼容模型端点，并组织真实对照实验。V0 原工作区与提交 `d40a3b2` 保留不动。Self-Improving 是研究方向，**尚未实现参数学习、记忆学习或 RL**；复核是测试时计算策略，不是训练。

## 两分钟离线演示

需要 Python 3.12+。在本版本项目目录创建独立环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\agent-lab.exe demo
```

Linux/macOS 使用 `.venv/bin/python` 和 `.venv/bin/agent-lab`。已有 openai 环境也可直接执行 `python -X utf8 demo.py demo`。

此命令不联网、不需要 API Key。生成 6 份 CSV、24 道题，运行 baseline 和 verify，保存完整记录和 comparison.md。演示客户端是固定脚本，不是 LLM；工具确实读取 CSV 并执行计算。每道 monthly 题第一次候选故意多加 1 元，用于展示失败评分与复核恢复。来源在终端、报告和 manifest 中标注为 scripted-demo。

预期的 **18/24 → 24/24 是故障注入，不是 DeepSeek 能力提升**。额外请求/工具次数也展示，不能只截图成功率。单题演示：

```powershell
python -X utf8 demo.py demo --split dev --task-id task_dev_7_monthly
```

## 架构

```text
demo.py（统一命令行）
  ├─ providers.py：公开模型配置、私有凭据、兼容 API 请求
  ├─ sessions.py：自由 CSV 问题、事件输出、会话保存/查看
  ├─ benchmark_suite.py：任务、Decimal 标准答案、数据指纹
  ├─ experiments.py：固定种子/交替策略调度、重复实验、manifest、评分
  │    ├─ agent.py：模型决策、工具反馈、baseline / verify
  │    ├─ tools.py + execution.py：参数边界、本地 / Docker 执行
  │    ├─ evaluate.py：确定性评分，不调用模型
  │    └─ run_io.py：JSON 快照、JSONL 事件、运行 Key 脱敏
  └─ reporting.py：分组统计、失败分类、配对比较、轨迹查看
```

offline_model.py 只用于离线演示；真实模式默认 DeepSeek。配置层仅支持 OpenAI-compatible Chat Completions，不宣称其他供应商已集成验收。借鉴 [Pi](https://github.com/earendil-works/pi) 的模型层/运行层/会话分离，详见 [参考与取舍](docs/PI_REFERENCE.md)。没有移植 Pi 源码，也没有引入新 Agent 框架、Web 服务、多 Agent 或训练依赖。

## 不只跑预设题：自由问题

先在新版本目录配置密钥（隐藏输入），检查连通性。probe 是一条短文本请求，收费，但不执行模型生成代码：

```powershell
python -X utf8 demo.py configure
python -X utf8 demo.py doctor
python -X utf8 demo.py probe
```

密钥保存到当前目录的 `credentials.private.json`，被 Git 忽略，**明文保存，不是保险箱**。环境变量优先。`--key-file` 可显式指定其他私有路径；不会扫描磁盘或 Cursor 配置。`--config configs/deepseek.json` 指定公开配置，`--model` 覆盖模型 ID。兼容端点示例需自己填写，未经过其他供应商验收。

Docker 验收后，可对任意指定 CSV 提问：

```powershell
python -X utf8 demo.py ask "统计这份 CSV 的数据行数，不包括表头" --data data/sales.csv --answer-format-file configs/row-count.json
python -X utf8 demo.py replay "SESSION_DIRECTORY"
```

默认答案格式 `{"answer":"string"}`；也支持明确的 string/number/integer 字段。会话只复制指定 CSV，保存 manifest、session.json、events.jsonl；`--json-events` 输出逐行事件。无标准答案时不评分，completed 只表示提交合法答案。replay 只查看，不执行工具、不请求模型，**不是恢复续跑**。

## 基准与假设

| 项目 | 定义 |
| --- | --- |
| 数据 | 6 份合成 CSV，每份 40 行 |
| 任务 | 地区最大销售额、月销售额、缺失值、加权均价，共 24 题 |
| 边界 | 退货负数、地区空格、空白 / NA / null、月份筛选 |
| dev / holdout | 各 3 个固定种子、12 题；相同题型，不是跨领域泛化 |
| baseline | 原始工具调用循环 |
| verify | 候选答案后要求新的成功 Python 执行，再提交答案 |
| 预算 | 两策略默认各最多 6 次请求，复核消耗同一总预算 |
| 评分 | 隐藏标准答案逐字段比较；数值绝对容差 0.01 |

假设：提交前重新检查数据/公式可能减少错误，也可能增加成本、超出预算或引入错误。需真实实验；离线演示只验证实验机制。新执行证据不保证独立算法或正确解释。

## 真实 DeepSeek 实验

默认 Docker。可用 Linux Docker Engine，也可用 Docker Desktop 的 Linux containers；不要求一定安装 Desktop。先准备镜像，并启用真实容器验收：

```powershell
docker pull python:3.12-slim
$env:AGENT_LAB_TEST_DOCKER = "1"
python -X utf8 -m unittest discover -s tests -p test_container.py -v
Remove-Item Env:AGENT_LAB_TEST_DOCKER
python -X utf8 demo.py run --policy both --split dev --repeats 3 --max-api-calls 432 --dry-run
python -X utf8 demo.py run --policy both --split dev --task-id task_dev_7_monthly
```

单题协议通过后再扩大：

```powershell
python -X utf8 demo.py run --policy both --split dev --repeats 3 --max-api-calls 432
python -X utf8 demo.py run --policy both --split holdout --repeats 3 --max-api-calls 432
```

真实调用收费。默认沿用原项目 deepseek-flash；用 `--model` 指定账户当前有效的模型 ID，probe 检查是否可用。公开配置默认 timeout=60 秒、重试关闭、temperature=0、非思考模式、max_tokens=2048；温度 0 不保证复现。`--dry-run` 不创建文件/请求 API，只显示最多请求次数，不估算真实费用。

整批 `--max-api-calls` 默认 72。若任务数 × repeats × 策略数 × max-steps 超过上限，会在创建会话/请求前拒绝；它是请求次数上限，**不是人民币硬预算**。认证、权限或模型端点错误会停止整批后续请求，保存第一条失败和 aborted 状态；未运行题保留在分母里，不生成完整 comparison.md。普通答错/工具错误仍按计划执行。

题目按 `--schedule-seed` 固定随机序排列，每个 task/repeat 相邻运行两策略，交替谁先执行；实际计划写入 manifest。此设计降低顺序偏差，不消除供应商漂移或保证统计显著性。每个任务独立上下文，不跨题共享记忆。

可信受控任务可显式接受宿主机执行风险：

```powershell
python -X utf8 demo.py run --policy both --split dev --task-id task_dev_7_monthly --backend local --allow-local-execution
```

**本地模式不是沙箱，不用于不可信输入。** Cursor 设置不会给 Python 进程自动配置 Key，项目不自动加载 .env。Docker 未通过后端检查时不会先请求模型。

## 结果与分析

每次会话保存到 runs/UTC时间_编号/，不覆盖旧结果：

```text
data/                           # CSV，Docker 只挂载这里
session.json                    # 实验目录索引
comparison.md                   # 两策略配对报告
experiments/时间_编号/
  manifest.json                 # 来源、配置、任务、代码快照、数据指纹
  task_dev_7_monthly__r01.json   # 完整交互、事件、状态、错误、评分
  task_dev_7_monthly__r01.events.jsonl # 生命周期事件，与模型上下文分离
  predictions.json              # 包含失败项及 repeat
  summary.json                  # 正确率、请求、工具、耗时、token
  report.md                     # 分类成绩与失败原因
```

```powershell
python -X utf8 demo.py report "RUN_DIRECTORY"
python -X utf8 demo.py compare "BASELINE_DIRECTORY" "VERIFY_DIRECTORY"
python -X utf8 demo.py inspect "TRACE_JSON"
```

比较器拒绝 mock/live 混比、不同题目/模型/预算/后端/版本与不完整实验；展示恢复、退化和计算代价，不宣称统计显著性。缺失 token 是未知，不是零费用。completed 是合法交卷，passed 才是正确答案。未完成、未运行和失败题不会从分母消失。

## 验收与边界

```powershell
python -X utf8 -m unittest discover -s tests -v
```

当前 Windows 63 个离线测试通过（含原有 19 项），7 项 Docker 集成默认跳过；本机 WSL Ubuntu 24.04.5 上显式启用后，**70 项全部通过，含 7 项真实容器边界测试**。核实了实际 CPU/内存/进程限额、断网、只读数据、凭据/答案不可见、非 root 与超时清理。见 [验收说明](docs/ACCEPTANCE.md) 和 [迭代记录](docs/ITERATION_LOG.md)。Windows/Linux、Python 3.12/3.13 CI 配置已提供，尚未上传运行，不能称为云端 CI 通过。本轮真实 API 探针返回 AuthenticationError，新版尚无真实模型 benchmark。

历史 V0 的 3/3 真实成绩见 [BASELINE_RESULTS.md](BASELINE_RESULTS.md)，不是新版 24 题成绩。V0 脚本入口保留，本地执行风险也保留；新版对外演示请用 demo.py。

详见 [演示流程](docs/DEMO_RUNBOOK.md)、[实验计划](docs/EXPERIMENT_PLAN.md)、[安全边界](SECURITY.md)、[简历表述](docs/RESUME_NOTES.md)。凭据、环境、未审阅日志不提交 Git，分享报告前仍需审阅。公开许可证尚未选择；未代作者决定许可条款。
