"""无密钥演示用的脚本化客户端，不是 LLM，也不证明模型能力。

    它根据公开 answer_format 选预写计算程序，并读取真实工具输出形成答案。
    monthly 家族故意注入 +1 元错误；复核时执行另一段固定程序纠正错误。
    这是故障注入演示，不能把两个策略之间的分差称为 DeepSeek 改进。
"""

import json
from types import SimpleNamespace

from openai.types.chat import ChatCompletion


def calculation_code(task_input: dict, *, alternative: bool = False) -> str:
    """从公开字段选择演示程序；代码读取 CSV，不访问 reference_answer。

    alternative 使用列表推导表达月筛选，与第一次循环式聚合不同；其他家族
    重算相同公式，只展示复核协议，不宣称真正的独立推理。
    """
    filename = task_input["data_file"]
    prefix = (
        "import csv, json\nfrom collections import defaultdict\n"
        f"with open({filename!r}, encoding='utf-8') as f:\n"
        "    rows = list(csv.DictReader(f))\n"
    )
    fields = task_input["answer_format"]
    if "region" in fields:
        body = (
            "totals = defaultdict(float)\nfor r in rows:\n"
            "    totals[r['region'].strip()] += float(r['quantity']) * float(r['unit_price'])\n"
            "best = sorted(totals, key=lambda k: (-totals[k], k))[0]\n"
            "answer = {'region': best, 'sales_amount': round(totals[best], 2)}\n"
        )
    elif "year_month" in fields:
        if alternative:
            body = "total = sum(float(r['quantity']) * float(r['unit_price']) for r in rows if r['date'][:7] == '2026-01')\n"
        else:
            body = "total = 0.0\nfor r in rows:\n    if r['date'].startswith('2026-01'):\n        total += float(r['quantity']) * float(r['unit_price'])\n"
        body += "answer = {'year_month': '2026-01', 'sales_amount': round(total, 2)}\n"
    elif "missing_count" in fields:
        body = "count = sum(r['product'].strip().casefold() in {'', 'na', 'null'} for r in rows)\nanswer = {'column': 'product', 'missing_count': count}\n"
    elif "weighted_unit_price" in fields:
        body = "total = sum(float(r['quantity']) * float(r['unit_price']) for r in rows)\nquantity = sum(int(r['quantity']) for r in rows)\nanswer = {'weighted_unit_price': round(total / quantity, 2)}\n"
    else:
        raise ValueError("离线演示不支持这种答案格式")
    return prefix + body + "print(json.dumps(answer))"


class ScriptedClient:
    """按固定状态返回 SDK 对象，使真实 Agent/工具/评分/日志流程可离线验收。

    usage=None 是刻意的：脚本化回复没有真实 token 账单，不能伪造模型成本。
    对外仍提供 chat.completions.create 接口，不要求运行循环依赖某个框架。
    """

    def __init__(self, task_input: dict):
        self.task_input = task_input
        self.chat = SimpleNamespace(completions=self)
        self.calls = 0
        self.checked = False

    def create(self, **kwargs):
        """根据历史消息演示列文件、计算、提交和复核；不接收隐藏任务对象。"""
        self.calls += 1
        messages = kwargs["messages"]
        tool_name, arguments, content = None, None, None
        if self.calls == 1:
            tool_name, arguments = "list_files", {"directory": "."}
        elif self.calls == 2:
            tool_name, arguments = "run_python", {"code": calculation_code(self.task_input)}
        elif messages[-1]["role"] == "user":
            self.checked = True
            tool_name, arguments = "run_python", {
                "code": calculation_code(self.task_input, alternative=True)}
        else:
            # 工具标准输出是 JSON。错误输出不能伪装为答案；解析失败直接暴露
            # 演示程序 bug，而不是在这里偷偷读取标准答案兜底。
            output = next(m["content"] for m in reversed(messages) if m["role"] == "tool")
            answer = json.loads(output.removeprefix("STDOUT:\n"))
            if "year_month" in answer and not self.checked:
                answer["sales_amount"] = round(answer["sales_amount"] + 1, 2)
            content = json.dumps(answer)
        message = {"role": "assistant", "content": content}
        if tool_name:
            message["tool_calls"] = [{"id": f"scripted_{self.calls}", "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(arguments)}}]
        return ChatCompletion.model_validate({
            "id": f"scripted_demo_{self.calls}", "created": 0, "model": "scripted-demo",
            "object": "chat.completion", "usage": None,
            "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_name else "stop",
                         "message": message}],
        })
