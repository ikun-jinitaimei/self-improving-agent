"""离线验证 Agent 与批量实验流程，不调用网络、不消耗 API。

FakeClient 按顺序返回预先写好的模型回复。它验证的是控制流程和日志，
这些答案由测试作者指定，绝不能作为模型能力或真实 baseline 的证据。
"""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx2
from openai import APIConnectionError
from openai.types.chat import ChatCompletion

from agent import load_task, parse_final_answer, run_agent
from run_benchmark import run_benchmark, summarize_results
from run_io import write_json
from tools import execute_tool


def reply(content=None, name=None, arguments=None, usage=True):
    """构造真实 SDK 类型，确保 model_dump 和工具消息协议也经过验证。"""
    message = {"role": "assistant", "content": content}
    if name:
        message["tool_calls"] = [{"id": "call_test", "type": "function",
            "function": {"name": name, "arguments": arguments or "{}"}}]
    return ChatCompletion.model_validate({
        "id": "offline_test", "created": 0, "model": "offline-test-model",
        "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if name else "stop",
                     "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15} if usage else None,
    })


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        # deepcopy 留下当时的消息快照，防止后续 append 改写测试证据。
        self.requests.append(copy.deepcopy(kwargs))
        item = next(self.replies)
        if isinstance(item, Exception):
            raise item
        return item


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.task = load_task("task_001")
        self.answer = '{"region":"South","sales_amount":23478.0}'
        # 抑制演示打印，让 unittest 结果容易阅读；每条断言仍检查实际返回值。
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_tool_then_answer_and_checkpoint(self):
        client = FakeClient([reply(name="list_files"), reply(self.answer)])
        saved = []
        result = run_agent(client, self.task, checkpoint=lambda r: saved.append(copy.deepcopy(r)))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["usage"]["total_tokens"], 30)
        self.assertEqual(saved[-1]["answer"]["region"], "South")
        tool_message = client.requests[1]["messages"][-1]
        self.assertEqual(tool_message["tool_call_id"], "call_test")
        self.assertIn("sales.csv", tool_message["content"])
        self.assertNotIn("reference_answer", json.dumps(client.requests))

    def test_invalid_arguments_are_observation_and_can_recover(self):
        client = FakeClient([reply(name="run_python", arguments="{broken"),
                             reply(name="list_files"), reply(self.answer)])
        result = run_agent(client, self.task)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["invalid_tool_calls"], 1)
        self.assertEqual(result["execution_failures"], 0)
        self.assertIn("ToolError", client.requests[1]["messages"][-1]["content"])

    def test_execution_error_is_returned_to_model(self):
        client = FakeClient([reply(name="run_python", arguments=json.dumps({"code": "print(1 / 0)"})),
                             reply(name="list_files"), reply(self.answer)])
        result = run_agent(client, self.task)
        self.assertEqual(result["execution_failures"], 1)
        self.assertEqual(result["status"], "completed")
        self.assertIn("ZeroDivisionError", result["events"][1]["observation"])

    def test_step_limit_preserves_tool_result(self):
        result = run_agent(FakeClient([reply(name="list_files")]), self.task, max_steps=1)
        self.assertEqual(result["error"]["type"], "step_limit")
        self.assertEqual(result["trajectory"][-1]["role"], "tool")

    def test_api_failure_preserves_prior_trace(self):
        error = APIConnectionError(request=httpx2.Request("POST", "https://example.test"), message="private HTTP body")
        result = run_agent(FakeClient([reply(name="list_files"), error]), self.task)
        self.assertEqual(result["error"]["type"], "api_error")
        self.assertEqual(result["tool_calls"], 1)
        self.assertNotIn("private HTTP body", json.dumps(result))
        self.assertFalse(result["usage_complete"])

    def test_missing_usage_is_unknown_not_zero(self):
        result = run_agent(FakeClient([reply(name="list_files", usage=False),
                                       reply(self.answer, usage=False)]), self.task)
        self.assertIsNone(result["usage"])
        self.assertFalse(result["usage_complete"])

    def test_answer_format_failure_preserves_raw_answer(self):
        result = run_agent(FakeClient([reply(name="list_files"), reply("not json")]), self.task)
        self.assertEqual(result["error"]["type"], "answer_format_error")
        self.assertEqual(result["trajectory"][-1]["content"], "not json")

    def test_answer_type_and_nonfinite_values_rejected(self):
        for content in ('{"x":true}', '{"x":"1"}', '{"x":NaN}', '{"x":Infinity}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_final_answer(content, {"x": "number"})

    def test_tool_validation_timeout_and_secret_environment(self):
        for args in ({"directory": 3}, {"unexpected": 1}):
            self.assertEqual(execute_tool("list_files", args)["status"], "invalid_arguments")
        self.assertIn("unknown tool", execute_tool("unknown", {})["observation"])
        self.assertIn("integer", execute_tool("run_python", {"code": "pass", "timeout_seconds": True})["observation"])
        self.assertIn("exceeded", execute_tool("run_python", {
            "code": "import time; time.sleep(3)", "timeout_seconds": 1})["observation"])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only-secret"}):
            result = execute_tool("run_python", {
                "code": "import os; print('DEEPSEEK_API_KEY' in os.environ)"})
        self.assertIn("False", result["observation"])

    def test_benchmark_continues_after_failure_and_does_not_overwrite(self):
        tasks = [{"task_id": f"task_{i}", "input": self.task,
                  "evaluation": {"reference_answer": json.loads(self.answer), "numeric_tolerance": .01}}
                 for i in (1, 2)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            error = APIConnectionError(request=httpx2.Request("POST", "https://example.test"))
            client = FakeClient([error, reply(name="list_files"), reply(self.answer)])
            first = run_benchmark(client, tasks, root, model="offline-test-model")
            summary = json.loads((first / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["success_rate"], .5)
            failed = json.loads((first / "task_1.json").read_text(encoding="utf-8"))
            self.assertEqual(failed["error"]["type"], "api_error")
            self.assertTrue((first / "manifest.json").exists())
            second = run_benchmark(FakeClient([reply(name="list_files"), reply(self.answer)]),
                                   tasks[:1], root, model="offline-test-model")
            self.assertNotEqual(first, second)
            self.assertTrue((first / "summary.json").exists())

    def test_error_words_in_stdout_do_not_mean_failure(self):
        # 真正启动子进程，验证状态取自退出码，而不是 stdout 中的错误字样。
        code = "print('EXIT CODE: 1'); print('ToolError: example')"
        client = FakeClient([
            reply(name="run_python", arguments=json.dumps({"code": code})),
            reply(self.answer),
        ])
        result = run_agent(client, self.task)
        self.assertEqual(result["execution_failures"], 0)
        self.assertEqual(result["events"][1]["status"], "success")

    def test_programming_errors_are_not_converted_to_external_failures(self):
        # 自己的程序错误应直接暴露，不能伪装成模型/API/执行环境的失败。
        with self.assertRaises(TypeError):
            run_agent(FakeClient([TypeError("wrong SDK argument")]), self.task)
        with patch("tools.list_files", side_effect=TypeError("internal bug")):
            with self.assertRaises(TypeError):
                execute_tool("list_files", {})

    def test_file_errors_are_execution_errors(self):
        for directory in ("../tasks", "sales.csv", "__missing_test_directory__"):
            with self.subTest(directory=directory):
                result = execute_tool("list_files", {"directory": directory})
                self.assertEqual(result["status"], "execution_error")

    def test_summary_includes_failures_and_unknown_usage(self):
        # 纯统计函数无需模型或文件。失败题进入平均步数和成功率的分母。
        records = [
            {"evaluation": {"task_id": "task_1", "passed": False}, "steps": 1,
             "tool_calls": 0, "invalid_tool_calls": 0, "execution_failures": 0,
             "latency_seconds": .5, "usage": None, "usage_complete": False},
            {"evaluation": {"task_id": "task_2", "passed": True}, "steps": 3,
             "tool_calls": 2, "invalid_tool_calls": 1, "execution_failures": 0,
             "latency_seconds": 1.5,
             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
             "usage_complete": True},
        ]
        summary = summarize_results(records, 2)
        self.assertEqual(summary["success_rate"], .5)
        self.assertEqual(summary["average_steps"], 2)
        self.assertFalse(summary["usage_complete"])
        self.assertEqual(summary["usage"]["total_tokens"], 15)

    def test_writer_redacts_current_key(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake-secret"}):
            path = Path(temporary) / "trace.json"
            write_json(path, {"text": "fake-secret"})
            self.assertEqual(json.loads(path.read_text())["text"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
