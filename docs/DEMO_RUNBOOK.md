# 五分钟项目演示

1. 介绍问题：工具调用能提供执行证据，但候选答案仍可能计算/解释错误。
2. 展示架构：模型决定，工具执行，evaluator 判分，runner 保存；无隐藏评分反馈。
3. 运行 `python -X utf8 demo.py demo --split dev --task-id task_dev_7_monthly`。
4. 打开该会话 `comparison.md`：先说明这是固定脚本 + 人工故障注入，不是 LLM。
5. 打开 baseline 的 `report.md`：显示错误答案已合法交卷，但确定性评分失败。
6. 对照 verify 轨迹：候选答案之后新增复核请求、计算证据、最终答案。
7. 解释开销：baseline 3 次请求/2 次工具，verify 5 次请求/3 次工具；不能隐藏增加的计算。
8. 展示测试命令、来源标签、配置/源码快照、数据指纹、失败保留和预算耗尽测试。
9. 展示 ask 与 JSONL 事件入口，说明用户问题无标准答案，不算 benchmark 成绩。
10. 如果已有真实 API/Docker 集成验收，再运行真实单题；否则明确此项待验收。

无需启动 Web 服务或把 API Key 放进代码。使用终端输出与 Markdown 报告即可。
演示记录包含原始候选与最终答案，而非只有一张成功率截图。

## 从无密钥演示转向真正使用

在独立版本目录使用已安装 SDK 的 Python：

```powershell
python -X utf8 demo.py configure
python -X utf8 demo.py doctor
python -X utf8 demo.py probe
```

configure 的输入不回显；不要截图/分享明文凭据文件。doctor 无 API 请求，
probe 只发一条短文本，不执行 Python，不能证明 benchmark 正确。
API 通过后仍需验收 Docker，再执行真实任务；不要自动回退到本地执行。
Docker 准备与测试命令见 README。用户可以显式接受受控本地执行风险，
但这不是隔离，不适合不可信任务、私人工作目录或不可信数据。

```powershell
python -X utf8 demo.py ask "统计这份 CSV 的数据行数，不包括表头" --data data/sales.csv --answer-format-file configs/row-count.json
python -X utf8 demo.py replay "SESSION_DIRECTORY"
python -X utf8 demo.py run --policy both --split dev --task-id task_dev_7_monthly
```

只读 replay 不需要密钥，也不重新执行模型代码。自由会话显示 completed
只是合法提交，不能当通过率。实际对照 manifest 中记录题序/策略序，
报告同时显示恢复、退化、token 和额外计算，不挑成功案例代替整体结果。
