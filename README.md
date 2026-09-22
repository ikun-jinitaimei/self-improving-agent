# Self-Improving Agent — V0

当前目标：在 3 个小型 CSV 任务上，完成工具调用、轨迹保存、自动评估的闭环。
这是一套受控本地冒烟测试，尚不能证明泛化能力或自我改进。

2026-09-22 首次真实运行：3/3 通过，7 次模型请求、5 次工具调用、无执行错误。
详细指标及局限见 [BASELINE_RESULTS.md](BASELINE_RESULTS.md)。

## 当前架构与文件

运行流程：任务公开输入 → agent.py → DeepSeek → tools.py → observation →
下一次模型请求 → 最终答案 → evaluate.py → 保存汇总。

| 文件 | 职责 |
| --- | --- |
| tasks/tasks.json | 3 道题；input 给模型，evaluation 仅给本地评分器 |
| data/sales.csv | 人工合成的数据 |
| agent.py | 单题循环、答案格式校验、状态与事件记录 |
| tools.py | 列文件、执行 Python、参数检查及超时 |
| evaluate.py | 用标准答案逐字段评分，数值允许给定误差 |
| run_benchmark.py | 批量运行、逐步保存轨迹、评分、汇总 |
| run_io.py | 独立的实验目录创建与 JSON 保存，供两个入口复用 |
| tests/test_agent.py | 不联网的循环、错误恢复、工具和批量流程测试 |
| tests/test_evaluate.py | 评估器单元测试 |
| test_deepseek_api.py | 早期单次连接测试，不是 benchmark |
| predictions/predictions.json | 早期手写示例，不代表模型真实结果 |

## 安装与运行（PowerShell，项目根目录）

已有 agent_env 可以直接使用；新机器需先用 Python 3.12 创建环境：

    python -m venv agent_env
    .\agent_env\Scripts\python.exe -m pip install -r requirements.txt

在运行命令的同一个 PowerShell 窗口设置密钥。以下写法兼容 Windows
PowerShell 5.1 和 PowerShell 7，输入不会明文显示，也不会写到源文件。

    $taskKey = Read-Host "DeepSeek API Key" -AsSecureString
    $env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $taskKey).Password
    Remove-Variable taskKey

设置后运行全部三题：

    .\agent_env\Scripts\python.exe -X utf8 run_benchmark.py

或先运行指定题目：

    .\agent_env\Scripts\python.exe -X utf8 run_benchmark.py --task-id task_001

可选参数：--model、--max-steps。默认 deepseek-flash、6 次模型请求；
非思考模式、temperature=0、单次最多输出 2048 tokens、网络超时 60 秒，
关闭 SDK 自动重试。工具运行最多 30 秒。每次模型输出仍可能不同。

环境变量属于当前终端进程；另一窗口（包括 Codex）不一定能读取到。
项目不自动加载 .env，密钥不能写入 tasks、代码或轨迹。

默认模型名与非思考模式参数参考：
[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。

## 结果保存与验收

每次运行产生 runs/时间_随机编号/，不会覆盖前次结果。

- manifest.json：任务快照、模型、Python/SDK 版本、源代码快照、数据哈希。
- task_001.json 等：公开输入、消息轨迹、工具事件、计数、状态和评分。
- predictions.json：这次真实生成的答案；没有成功交卷的题不编造答案。
- summary.json：全部题目的分数、失败题 ID、步数、调用次数、耗时、token 用量。

每次请求前、回复后、工具执行后都保存检查点。异常或步数耗尽返回 failed，
保留当时轨迹；正常提交返回 completed，仍需评分才能确定答案是否正确。
这里的异常处理针对 API 错误、文件操作失败及超时等外部问题；内部编程错误
保留 traceback，不会包装成模型失败。此时磁盘检查点可能仍显示 running。
强行关闭进程可能留下 running 状态，那表示未完成，不能当作完整成绩。
终端单独执行 agent.py 也保存轨迹，但只有 benchmark 命令负责自动评分。

指标定义：steps 是模型请求尝试次数；tool_calls 包括非法调用；
invalid_tool_calls 统计参数 JSON、类型、范围或名称错误；execution_failures
统计合法请求执行时的错误（包括超时）。这两项不重复统计。
usage 缺失用 null，部分缺失时 usage_complete=false；已知 token 仅为下界，
不能当作全部费用。耗时包含请求和工具执行，但不包含批次启动及最终评分。
平均步数统计已结束的全部题，失败题也计入。重试没有实现，不报告假重试数据。

工具入口统一解析和校验参数，返回 status 与 observation。状态来自真实执行
结果，不从输出文字推断。轨迹中的工具事件增加 status；arguments 保存模型的
原始 JSON 字符串，方便检查非法参数。先前实验中的 arguments 字典仍保留原样。

本次代码整理后的离线测试为 19 个，包含输出文字误判、内部编程错误传播、
文件错误及独立汇总验证；此前真实 baseline 的 3/3 是整理前版本的实验成绩。

验收命令：

    .\agent_env\Scripts\python.exe -X utf8 -m unittest discover -s tests -v

测试替身输出的答案是人为指定的，仅验证程序流程；测试通过不等于模型解题成功。
真实验收需检查三题轨迹和 summary，确认工具从 sales.csv 计算结果，未读取标准答案。

## 当前限制与下一步

run_python 不是操作系统沙箱，工作目录不能阻止访问其他文件。已避免继承 API
密钥环境变量，但不能阻止读取磁盘上的其他文件或网络访问。只用于受控本地任务。
提示词不传标准答案不等于严格答案隔离；正式扩大 benchmark 前需要完善隔离。

源码快照和数据哈希支持追溯，不保证供应商模型升级后逐位复现输出。
如需在其他机器复查，应保留本次对应的数据文件；哈希可验证文件是否相同。
runs 默认不进入 Git，分享前审阅内容。现有示例 predictions 不会被覆盖。

下一步：补真实 baseline → 检查失败轨迹 → 再决定扩题或研究 reflection。
尚未实现 reflection、verifier、memory、训练或 RL。
