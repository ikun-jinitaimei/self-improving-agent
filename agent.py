"""Self-Improving Agent V0：一个最小、透明的 Agent 循环。

这个文件只负责运行一个任务：

任务输入 -> 调用 DeepSeek -> 执行工具 -> 返回 observation
         -> 再次调用 DeepSeek -> 得到最终 JSON 答案

run_agent 通过 checkpoint 回调交出可保存的轨迹；run_benchmark.py 负责具体
写文件、批量实验和评分。单题命令行入口也会调用保存函数，留下本次轨迹。
"""

import json
import math
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai import OpenAI

from tools import execute_tool, validate_tool_arguments


# 无论从哪个工作目录启动 agent.py，都以本文件所在目录作为项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parent
TASKS_PATH = PROJECT_ROOT / "tasks" / "tasks.json"

# DeepSeek 提供与 OpenAI SDK 兼容的接口。
# SDK 负责发送 HTTP 请求；真正处理问题的模型仍然是 DeepSeek。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


# System prompt 规定 Agent 的基本行为和最终输出协议。
# 这里不放任何任务的标准答案，因此不会把 evaluation 信息泄露给模型。
SYSTEM_PROMPT = """You are a data-analysis agent.

You can inspect files and run Python by calling the provided tools.
Use tool results as evidence; do not invent values.
Use the Python standard library (csv, json, collections) for these small tasks.
Read only the supplied task data. Do not read reference answers or project source.
You must use at least one tool before giving the final answer.

The run_python tool executes with data/ as its working directory.
For example, the project path data/sales.csv should be opened as sales.csv
inside Python code.

When you finish, respond with exactly one JSON object matching the requested
answer format. Do not wrap the JSON in Markdown and do not add explanations.
"""


# 这是发给模型的“工具说明书”，不是工具的真正实现。
# 模型读取这些 JSON Schema 后，才能知道有哪些工具以及参数应该怎样填写。
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files or directories inside the data directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": (
                            "Directory relative to data/. Use '.' for the data root."
                        ),
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Run Python code in the data directory and return stdout, stderr, "
                "and a non-zero exit code when execution fails."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code used to inspect or analyze data.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Maximum running time in seconds.",
                        "minimum": 1,
                        "maximum": 30,
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        },
    },
]


def create_client() -> OpenAI:
    """读取环境变量并创建连接 DeepSeek 的 API 客户端。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")

    if not api_key:
        raise RuntimeError(
            "没有找到 DEEPSEEK_API_KEY。请先在当前终端设置环境变量。"
        )

    return OpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL,
        # 限制单次网络等待，关闭 SDK 隐式重试以明确请求计数。
        timeout=60.0,
        max_retries=0,
    )


def load_task(task_id: str) -> dict[str, Any]:
    """根据 task_id 读取任务，并且只返回公开的 input 部分。"""
    with TASKS_PATH.open(encoding="utf-8") as file:
        tasks = json.load(file)

    for task in tasks:
        if task["task_id"] == task_id:
            # evaluation 中含有标准答案，绝不能把整个 task 返回给 Agent。
            return task["input"]

    raise ValueError(f"没有找到任务：{task_id}")


def build_user_message(task_input: dict[str, Any]) -> str:
    """把结构化任务信息整理成一条发给模型的用户消息。"""
    question = task_input["question"]
    data_file = task_input["data_file"]
    answer_format = task_input["answer_format"]

    return (
        f"Question:\n{question}\n\n"
        f"Data file (relative to the project root):\n{data_file}\n\n"
        "Required final answer format:\n"
        f"{json.dumps(answer_format, ensure_ascii=False, indent=2)}"
    )


def parse_tool_arguments(arguments_text: str) -> dict[str, Any]:
    """把模型生成的 JSON 参数文本转换为 Python 字典。"""
    try:
        arguments = json.loads(arguments_text)
    except json.JSONDecodeError as error:
        # 参数格式错误也要变成 observation 返回模型，而不是让程序直接崩溃。
        return {"_argument_error": f"invalid JSON arguments: {error.msg}"}

    if not isinstance(arguments, dict):
        return {"_argument_error": "tool arguments must be a JSON object"}

    return arguments


def parse_final_answer(
    content: str,
    answer_format: dict[str, str],
) -> dict[str, Any]:
    """解析最终 JSON，并检查字段是否符合任务公开规定的格式。"""
    try:
        answer = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"模型的最终回答不是合法 JSON：{error.msg}\n原始回答：{content}"
        ) from error

    if not isinstance(answer, dict):
        raise ValueError("模型的最终回答必须是一个 JSON object。")

    expected_fields = set(answer_format)
    actual_fields = set(answer)

    if actual_fields != expected_fields:
        missing_fields = sorted(expected_fields - actual_fields)
        extra_fields = sorted(actual_fields - expected_fields)
        raise ValueError(
            "模型最终答案的字段不符合 answer_format。"
            f"缺少字段：{missing_fields}；多余字段：{extra_fields}"
        )

    # 检查公开格式规定的类型，不读取隐藏答案。bool 不能冒充整数。
    for field, kind in answer_format.items():
        value = answer[field]
        if kind == "string":
            valid = isinstance(value, str)
        elif kind == "integer":
            valid = type(value) is int
        elif kind == "number":
            valid = type(value) in (int, float) and math.isfinite(value)
        else:
            valid = False
        if not valid:
            raise ValueError(f"字段 {field} 必须是 {kind}")
    # 这里只检查输出协议，不判断答案数值是否正确。
    # 正确性仍然由 evaluate.py 使用隐藏的 reference_answer 判断。
    return answer


def run_agent(
    client: OpenAI,
    task_input: dict[str, Any],
    max_steps: int = 6,
    *,
    model: str | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """运行一道题；无论成功或可预期失败，都保留轨迹与计数。

    checkpoint 是调用者提供的保存函数。Agent 只提供记录，runner 决定存到哪。
    status=completed 只表示交出了合法答案，答案正确与否由 evaluator 判断。
    steps 是模型请求次数（含失败请求）；tool_calls 是模型提出的调用总数。
    """
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps 必须是正整数")
    started = time.perf_counter()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(task_input)},
    ]

    result = {
        "status": "running", "answer": None, "error": None,
        "trajectory": messages, "events": [], "steps": 0, "tool_calls": 0,
        "invalid_tool_calls": 0, "execution_failures": 0,
        "latency_seconds": 0.0, "usage": None,
        "usage_complete": True, "responses_received": 0,
        "config": {"model": model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
                   "max_steps": max_steps, "thinking": "disabled", "temperature": 0,
                   "max_tokens": 2048, "timeout_seconds": 60, "sdk_max_retries": 0},
    }

    def save() -> None:
        # 使用单调时钟测耗时，避免系统时间校准影响差值。
        result["latency_seconds"] = round(time.perf_counter() - started, 4)
        if checkpoint is not None:
            checkpoint(result)

    def fail(kind: str, detail: str) -> dict[str, Any]:
        result["status"] = "failed"
        result["error"] = {"type": kind, "message": detail}
        save()
        return result

    save()
    # 每轮是一次模型决策。回调在请求前后执行，网络中断也能留下请求前的状态。
    for step in range(1, max_steps + 1):
        print(f"\n--- Step {step} ---")
        result["steps"] = step
        save()
        try:
            response = client.chat.completions.create(
                model=result["config"]["model"], messages=messages,
                tools=TOOL_DEFINITIONS, stream=False, temperature=0, max_tokens=2048,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception as error:
            # 不保存原始 HTTP 错误体/请求头，以免将凭据写入实验文件。
            result["usage_complete"] = False
            return fail("api_error", type(error).__name__)
        result["responses_received"] += 1
        usage = response.usage.model_dump() if response.usage else None
        result["events"].append({"step": step, "type": "model_response",
                                 "response_id": response.id, "model": response.model,
                                 "usage": usage})
        if usage is None:
            result["usage_complete"] = False
        else:
            if result["usage"] is None:
                result["usage"] = {k: 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
            for key in result["usage"]:
                result["usage"][key] += usage[key]
        if not response.choices:
            return fail("response_error", "API returned no choices")
        assistant_message = response.choices[0].message

        # SDK 返回的是对象；model_dump() 把它变成普通字典，便于继续发送和保存。
        messages.append(assistant_message.model_dump(exclude_none=True))
        save()
        if response.choices[0].finish_reason in ("length", "content_filter"):
            return fail("response_error", response.choices[0].finish_reason)
        tool_calls = assistant_message.tool_calls or []

        # 没有工具调用表示模型决定结束任务并提交最终答案。
        if not tool_calls:
            if result["tool_calls"] == 0:
                return fail("protocol_error", "模型未调用工具就提交答案")
            try:
                result["answer"] = parse_final_answer(
                    assistant_message.content or "", task_input["answer_format"])
            except ValueError as error:
                return fail("answer_format_error", str(error))
            result["status"] = "completed"
            save()
            return result

        # 一次模型回复可能同时请求多个工具，因此这里逐个执行。
        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            arguments = parse_tool_arguments(tool_call.function.arguments)

            argument_error = arguments.get("_argument_error") or validate_tool_arguments(tool_name, arguments)
            if argument_error:
                observation = f"ToolError: {argument_error}"
                result["invalid_tool_calls"] += 1
            else:
                try:
                    observation = execute_tool(tool_name, arguments)
                except Exception as error:
                    observation = f"ToolError: {type(error).__name__}"
            # STDERR 也可能只是警告；仅非零退出码或 ToolError 算执行失败。
            execution_failed = not argument_error and (
                observation.startswith("ToolError:") or "\nEXIT CODE:" in observation)
            result["execution_failures"] += int(bool(execution_failed))
            result["tool_calls"] += 1
            result["events"].append({"step": step, "type": "tool",
                "tool_call_id": tool_call.id, "name": tool_name, "arguments": arguments,
                "observation": observation, "invalid_arguments": bool(argument_error),
                "execution_failed": bool(execution_failed)})

            print(f"Action: {tool_name}")
            print(f"Arguments: {json.dumps(arguments, ensure_ascii=False)}")
            print(f"Observation:\n{observation}")

            # role='tool' 的消息就是环境反馈。tool_call_id 用来告诉模型，
            # 这条 observation 对应它刚才提出的哪一次工具调用。
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": observation,
                }
            )
            save()

    return fail("step_limit", f"Agent 在 {max_steps} 步内没有产生最终答案")


def main() -> None:
    """命令行入口：默认运行 task_001，也可以在命令后指定 task_id。"""
    task_id = sys.argv[1] if len(sys.argv) > 1 else "task_001"
    task_input = load_task(task_id)
    client = create_client()
    # 命令行单题运行也写日志。局部导入避免 agent 与 runner 的模块级循环导入。
    from run_benchmark import create_run_directory, write_json
    output_dir = create_run_directory(PROJECT_ROOT / "runs")
    result = run_agent(client, task_input, checkpoint=lambda record: write_json(
        output_dir / f"{task_id}.json", {"task_id": task_id, "input": task_input, **record}))

    print("\n=== Final Answer ===")
    print(json.dumps(result["answer"], ensure_ascii=False, indent=2))
    print(f"Steps: {result['steps']}")
    print(f"Tool calls: {result['tool_calls']}")
    print(f"Status: {result['status']}; trace: {output_dir}")
    if result["error"]:
        print(result["error"])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
