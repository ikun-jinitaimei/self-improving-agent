"""Self-Improving Agent 研究版：保留可读循环，添加可观测事件和复核协议。

这个文件只负责运行一个任务：

任务输入 -> 模型决策 -> 执行工具 -> 返回 observation
         -> 下一次模型决策 -> 可选复核 -> 最终 JSON 答案

providers.py 管理模型请求；run_agent 通过 checkpoint/on_event 交出记录。
experiments.py 组织对照评分，sessions.py 处理无标准答案的用户会话。
原单题/三题入口仍兼容，不代表原 V0 工作区被修改。
"""

import json
import math
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai import APIError, OpenAI

from run_io import create_run_directory, write_json
from tools import execute_tool
from providers import ModelSettings, request_completion


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
    """把公开任务整理成模型消息；answer_format 是类型声明，不是答案示例。

    真实加权均价回归中，模型计算正确却提交了带引号的数字。明确 JSON 类型
    的含义，不向模型提供标准答案，也不在解析器中把字符串偷偷转成数字。
    baseline 和 verify 共用这条说明，不能只改其中一组以影响对照。
    """
    question = task_input["question"]
    data_file = task_input["data_file"]
    answer_format = task_input["answer_format"]

    return (
        f"Question:\n{question}\n\n"
        f"Data file (relative to the project root):\n{data_file}\n\n"
        "Required JSON value types (this is a type declaration, not an example answer):\n"
        f"{json.dumps(answer_format, ensure_ascii=False, indent=2)}\n\n"
        "For number and integer fields, use unquoted JSON numeric values. "
        "Do not put numeric results in strings. String fields must contain JSON strings."
    )


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
    tool_executor: Callable[[str, str], dict[str, str]] = execute_tool,
    policy: str = "baseline",
    settings: ModelSettings | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """运行一道题；无论成功或可预期失败，都保留轨迹与计数。

    checkpoint 是调用者提供的保存函数。Agent 只提供记录，runner 决定存到哪。
    status=completed 只表示交出了合法答案，答案正确与否由 evaluator 判断。
    steps 是模型请求次数（含失败请求）；tool_calls 是模型提出的调用总数。
    """
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps 必须是正整数")
    if policy not in {"baseline", "verify"}:
        raise ValueError("policy 必须是 baseline 或 verify")
    # 复核策略只使用公开题目、候选答案和执行反馈；从不读取评分结果。
    # 两种策略共享 max_steps 总预算，额外复核不是免费的隐藏计算。
    verification_requested = False
    verification_calculated = False
    started = time.perf_counter()
    settings = settings or ModelSettings(model=model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL))
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
        "config": {"model": settings.model,
                   "policy": policy,
                   "max_steps": max_steps, "sdk_max_retries": 0,
                   "provider_settings": settings.public()},
    }

    def emit(kind: str, **details) -> None:
        # 事件与发给模型的 messages 分开，日志/UI 元数据绝不能混进模型上下文。
        # checkpoint 保留快照，on_event 则允许终端进度和 JSONL 会话订阅事件。
        event = {"sequence": len(result["events"]) + 1, "type": kind,
                 "step": result["steps"], "elapsed_seconds": round(time.perf_counter() - started, 4),
                 **details}
        result["events"].append(event)
        if on_event is not None:
            on_event(event)

    def save() -> None:
        # 使用单调时钟测耗时，避免系统时间校准影响差值。
        result["latency_seconds"] = round(time.perf_counter() - started, 4)
        if checkpoint is not None:
            checkpoint(result)

    def fail(kind: str, detail: str) -> dict[str, Any]:
        result["status"] = "failed"
        result["error"] = {"type": kind, "message": detail}
        if result["steps"] and result["events"][-1]["type"] != "turn_end":
            emit("turn_end", status="failed")
        emit("agent_end", status="failed", error_type=kind)
        save()
        return result

    emit("agent_start", policy=policy)
    save()
    # 每轮是一次模型决策。回调在请求前后执行，网络中断也能留下请求前的状态。
    for step in range(1, max_steps + 1):
        print(f"\n--- Step {step} ---")
        result["steps"] = step
        emit("turn_start")
        save()
        try:
            response = request_completion(client, messages, TOOL_DEFINITIONS, settings)
        except APIError as error:
            # 不保存原始 HTTP 错误体/请求头，以免将凭据写入实验文件。
            # 只捕获 SDK 的 API 异常，TypeError 等内部错误保留 traceback。
            result["usage_complete"] = False
            # SDK 的 APITimeoutError 无法区分连接阶段与服务响应阶段。
            # 只记录异常类/状态码，不存原始异常文本、请求头或响应体；这些
            # 元数据用于分析环境故障，不回传模型，也不触发自动重试。
            emit("api_error", error_type=type(error).__name__,
                 cause_type=type(error.__cause__).__name__ if error.__cause__ else None,
                 status_code=getattr(error, "status_code", None))
            return fail("api_error", type(error).__name__)
        result["responses_received"] += 1
        usage = response.usage.model_dump() if response.usage else None
        emit("model_response", response_id=response.id, model=response.model, usage=usage)
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
            if policy == "verify" and not verification_requested:
                verification_requested = True
                emit("verification_requested")
                messages.append({"role": "user", "content": (
                    "Before submitting, independently check your candidate answer with "
                    "at least one new tool call. Re-read the supplied CSV; check filters, "
                    "returns, missing values and units. Do not access reference answers. "
                    "Use an alternative calculation where possible. This is a tool-based "
                    "check, not a written review: keep verification notes in tool outputs. "
                    "After checking, respond with ONLY the final JSON object matching the "
                    "original value types, with unquoted numbers. No explanations, "
                    "verification summaries, Markdown, or text outside the JSON object."
                )})
                emit("turn_end", status="verification_requested")
                save()
                continue
            if verification_requested and not verification_calculated:
                return fail("verification_missing", "复核阶段没有成功执行新的 Python 计算")
            try:
                result["answer"] = parse_final_answer(
                    assistant_message.content or "", task_input["answer_format"])
            except ValueError as error:
                return fail("answer_format_error", str(error))
            result["status"] = "completed"
            emit("turn_end", status="completed")
            emit("agent_end", status="completed")
            save()
            return result

        # 一次模型回复可能同时请求多个工具，因此这里逐个执行。
        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            # 原始 JSON 参数交给工具入口统一解析和校验。保留原文便于分析坏调用。
            arguments = tool_call.function.arguments
            emit("tool_start", name=tool_name, tool_call_id=tool_call.id)
            save()
            tool_result = tool_executor(tool_name, arguments)
            observation = tool_result["observation"]
            invalid = tool_result["status"] == "invalid_arguments"
            failed = tool_result["status"] == "execution_error"
            # 仅列目录或提交非法调用不能冒充重新计算；成功执行也只说明有
            # 新的执行证据，并不保证模型采用了独立算法或正确解释了结果。
            if verification_requested and tool_name == "run_python" and tool_result["status"] == "success":
                verification_calculated = True
            result["invalid_tool_calls"] += int(invalid)
            result["execution_failures"] += int(failed)
            result["tool_calls"] += 1
            emit("tool", tool_call_id=tool_call.id, name=tool_name,
                 arguments=arguments, observation=observation, status=tool_result["status"],
                 invalid_arguments=invalid, execution_failed=failed)

            print(f"Action: {tool_name}")
            print(f"Arguments: {arguments}")
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
        emit("turn_end")

    return fail("step_limit", f"Agent 在 {max_steps} 步内没有产生最终答案")


def main() -> None:
    """命令行入口：默认运行 task_001，也可以在命令后指定 task_id。"""
    task_id = sys.argv[1] if len(sys.argv) > 1 else "task_001"
    task_input = load_task(task_id)
    # 保存模块独立于 Agent 和 runner，单题运行也可以直接复用。
    output_dir = create_run_directory(PROJECT_ROOT / "runs")
    with create_client() as client:
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
