"""新版 demo 的离线验收：生成器、复核协议、对照分析和执行安全配置。

    网络请求从不发生。Docker 相关测试只构造命令/替换进程返回值，不等于
    Docker Desktop 集成已验收。真实 CSV 子进程测试使用临时生成的受控文件。
"""

import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai.types.chat import ChatCompletion

from agent import run_agent
from benchmark_suite import build_suite, generate_rows, reference_answers, suite_fingerprint
from demo import main, parser
from execution import check_docker, python_command
from experiments import run_experiment
from offline_model import ScriptedClient, calculation_code
from reporting import compare_runs, failure_reason, inspect_trace, load_run, run_report, summarize
from tools import execute_tool


def response(content=None, name=None, arguments="{}"):
    """构造 SDK 回复，供协议边界测试使用，不冒充模型生成结果。"""
    message = {"role": "assistant", "content": content}
    if name:
        message["tool_calls"] = [{"id": "test_call", "type": "function",
            "function": {"name": name, "arguments": arguments}}]
    return ChatCompletion.model_validate({
        "id": "test", "created": 0, "model": "test", "object": "chat.completion", "usage": None,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if name else "stop", "message": message}],
    })


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.tasks = build_suite(self.data, "both")
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def experiment(self, policy="baseline", tasks=None, repeats=1, max_steps=6):
        """走真实组织层，只替换模型；默认两题以控制单元测试耗时。"""
        return run_experiment(ScriptedClient, tasks or self.tasks[:2], self.data, self.root / "runs",
            policy=policy, kind="scripted-demo", model="scripted-demo", backend="local",
            repeats=repeats, max_steps=max_steps)

    def test_suite_contains_disjoint_ids_and_all_categories(self):
        self.assertEqual(len(self.tasks), 24)
        self.assertEqual(len({t["task_id"] for t in self.tasks}), 24)
        self.assertEqual({t["split"] for t in self.tasks}, {"dev", "holdout"})
        self.assertEqual({t["category"] for t in self.tasks}, {"region", "monthly", "missing", "weighted"})
        self.assertEqual(len(list(self.data.iterdir())), 6)
        self.assertTrue(all(p.suffix == ".csv" for p in self.data.iterdir()))

    def test_suite_is_deterministic_and_hash_tracks_data(self):
        other = self.root / "other"
        tasks = build_suite(other, "both")
        original = suite_fingerprint(self.tasks, self.data)
        self.assertEqual(original, suite_fingerprint(tasks, other))
        path = other / tasks[0]["input"]["data_file"]
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        self.assertNotEqual(original, suite_fingerprint(tasks, other))

    def test_suite_does_not_overwrite_existing_dataset(self):
        with self.assertRaises(FileExistsError):
            build_suite(self.data)
        with self.assertRaises(ValueError):
            build_suite(self.root / "bad", "invalid")

    def test_reference_handles_returns_missing_whitespace_and_ties(self):
        rows = [
            {"date": "2026-01-01", "region": " B ", "product": "NA", "quantity": "3", "unit_price": "2.50"},
            {"date": "2026-01-02", "region": "B", "product": "null", "quantity": "-1", "unit_price": "2.50"},
            {"date": "2026-02-01", "region": "A", "product": "x", "quantity": "2", "unit_price": "2.50"},
        ]
        answers = reference_answers(rows)
        self.assertEqual(answers["region"], {"region": "A", "sales_amount": 5.0})
        self.assertEqual(answers["monthly"]["sales_amount"], 5.0)
        self.assertEqual(answers["missing"]["missing_count"], 2)
        self.assertEqual(answers["weighted"]["weighted_unit_price"], 2.5)

    def test_generator_contains_explicit_edge_cases(self):
        rows = generate_rows(7)
        self.assertTrue(any(int(r["quantity"]) < 0 for r in rows))
        self.assertEqual({r["product"] for r in rows if r["product"] in {"", "NA", "null"}}, {"", "NA", "null"})
        self.assertTrue(any(r["region"] != r["region"].strip() for r in rows))

    def test_offline_calculations_match_all_reference_answers(self):
        # 24 题的参考算法是 Decimal，执行算法是 float；对每题独立比较。
        for task in self.tasks:
            with self.subTest(task=task["task_id"]):
                result = execute_tool("run_python", {"code": calculation_code(task["input"], alternative=True)}, data_root=self.data)
                answer = json.loads(result["observation"].removeprefix("STDOUT:\n"))
                self.assertEqual(answer, task["evaluation"]["reference_answer"])

    def test_verify_recovers_injected_fault_and_counts_extra_work(self):
        left, right = self.experiment(), self.experiment("verify")
        report = compare_runs(left, right)
        self.assertIn("NOT MODEL IMPROVEMENT", report)
        self.assertIn("recovery: 1", report)
        manifest, records = load_run(right)
        metric = summarize(manifest, records)
        self.assertEqual(metric["passed"], 2)
        self.assertEqual(metric["steps"], 10)
        self.assertEqual(metric["tool_calls"], 6)
        self.assertIsNone(metric["known_tokens"])
        self.assertFalse(metric["usage_complete"])

    def test_repeat_trace_files_are_distinct(self):
        directory = self.experiment(tasks=self.tasks[:1], repeats=2)
        self.assertEqual(len(list(directory.glob("task_*__r*.json"))), 2)
        manifest, records = load_run(directory)
        self.assertEqual({r["repeat"] for r in records}, {1, 2})
        self.assertEqual(summarize(manifest, records)["expected"], 2)
        # 复制一份已有样本不能让通过次数虚增；外部日志边界必须拒绝重复身份。
        duplicate = directory / "task_duplicate__r01.json"
        duplicate.write_text(json.dumps(records[0]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "额外或重复"):
            load_run(directory)

    def test_step_budget_failure_is_preserved_and_scored(self):
        directory = self.experiment("verify", tasks=self.tasks[:1], max_steps=3)
        manifest, records = load_run(directory)
        self.assertEqual(records[0]["error"]["type"], "step_limit")
        self.assertEqual(records[0]["steps"], 3)
        self.assertFalse(records[0]["evaluation"]["passed"])
        self.assertEqual(summarize(manifest, records)["success_rate"], 0)

    def test_factory_only_receives_public_input(self):
        received = []

        def factory(task_input):
            received.append(copy.deepcopy(task_input))
            return ScriptedClient(task_input)

        run_experiment(factory, self.tasks[:1], self.data, self.root / "runs", policy="baseline",
            kind="scripted-demo", model="scripted-demo", backend="local")
        self.assertEqual(received, [self.tasks[0]["input"]])
        self.assertNotIn("reference_answer", json.dumps(received))

    def test_compare_rejects_mock_live_and_budget_mismatch(self):
        left, right = self.experiment(), self.experiment("verify")
        path = right / "manifest.json"
        original = json.loads(path.read_text())
        for field, value in (("kind", "live"), ("max_steps", 7), ("backend", "docker"),
                             ("suite_fingerprint", "different"), ("source_snapshot", {}),
                             ("python", "0.0"), ("openai_version", "0.0")):
            altered = {**original, field: value}
            path.write_text(json.dumps(altered), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(ValueError):
                compare_runs(left, right)
        path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaises(ValueError):
            compare_runs(right, left)

    def test_unfinished_runs_keep_denominator_and_cannot_compare(self):
        left, right = self.experiment(), self.experiment("verify")
        path = next(right.glob("task_*__r*.json"))
        record = json.loads(path.read_text())
        record["status"] = "running"
        record.pop("evaluation")
        path.write_text(json.dumps(record), encoding="utf-8")
        manifest, records = load_run(right)
        metric = summarize(manifest, records)
        self.assertEqual(metric["expected"], 2)
        self.assertEqual(metric["finished"], 1)
        self.assertEqual(metric["success_rate"], .5)
        with self.assertRaisesRegex(ValueError, "尚未完成"):
            compare_runs(left, right)

    def test_report_labels_source_and_does_not_publish_private_snapshots(self):
        report = run_report(self.experiment())
        self.assertIn("NOT LLM RESULTS", report)
        self.assertIn("incorrect_answer: 1", report)
        self.assertNotIn("reference_answer", report)
        self.assertNotIn("source_snapshot", report)

    def test_inspector_fence_handles_backticks(self):
        path = self.root / "trace.json"
        path.write_text(json.dumps({"task_id": "task_test", "content": "prefix````suffix"}), encoding="utf-8")
        report = inspect_trace(path)
        self.assertIn("`````json", report)

    def test_cli_defaults_and_local_execution_requires_consent(self):
        self.assertEqual(parser().parse_args(["run"]).backend, "docker")
        self.assertEqual(parser().parse_args(["demo"]).split, "both")
        self.assertEqual(main(["run", "--backend", "local"]), 2)

    def test_cli_missing_key_never_creates_session(self):
        destination = self.root / "should_not_exist"
        with patch("demo.check_docker"), patch("demo.configured_client", side_effect=RuntimeError("missing key")):
            self.assertEqual(main(["run", "--output", str(destination)]), 2)
        self.assertFalse(destination.exists())

    def test_docker_command_mounts_only_data_and_disables_network(self):
        command = python_command("print(1)", self.data, "docker", "sia-test")
        self.assertEqual(command[:2], ["docker", "create"])
        self.assertIn("--network=none", command)
        self.assertIn("--read-only", command)
        self.assertIn("--memory=256m", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--pull=never", command)
        mount = command[command.index("--mount") + 1]
        self.assertIn(str(self.data.resolve()), mount)
        self.assertTrue(mount.endswith("readonly"))
        self.assertNotIn("-e", command)

    def test_docker_preflight_is_read_only_and_fails_without_cli(self):
        with patch("execution.shutil.which", return_value=None), self.assertRaises(RuntimeError):
            check_docker()
        with patch("execution.shutil.which", return_value="docker"), patch("execution.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            check_docker()
            self.assertEqual(run.call_args.args[0][:3], ["docker", "image", "inspect"])

    def test_docker_timeout_cleans_only_generated_container(self):
        expired = subprocess.TimeoutExpired("docker", 1)
        with patch("tools.subprocess.run", side_effect=[expired, SimpleNamespace(returncode=0)]) as run:
            result = execute_tool("run_python", {"code": "pass", "timeout_seconds": 1}, data_root=self.data, backend="docker")
        self.assertEqual(result["status"], "execution_error")
        initial, cleanup = (call.args[0] for call in run.call_args_list)
        name = initial[initial.index("--name") + 1]
        self.assertTrue(name.startswith("sia-"))
        self.assertEqual(cleanup, ["docker", "rm", "-f", name])

    def test_tool_output_truncation_is_explicit(self):
        completed = SimpleNamespace(stdout="x" * 13000, stderr="", returncode=0)
        with patch("tools.subprocess.run", return_value=completed):
            result = execute_tool("run_python", {"code": "pass"}, data_root=self.data)
        self.assertTrue(result["observation"].endswith("[OUTPUT TRUNCATED]"))
        self.assertEqual(result["status"], "success")

    def test_docker_create_start_remove_have_separate_timeouts(self):
        """固定 Docker 回复检验生命周期；真实容器边界另外验收。"""
        prepared = SimpleNamespace(stdout="container-id", stderr="", returncode=0)
        completed = SimpleNamespace(stdout="result", stderr="", returncode=0)
        with patch("tools.subprocess.run", side_effect=[prepared, completed, prepared]) as run:
            result = execute_tool("run_python", {"code": "pass", "timeout_seconds": 2},
                                  data_root=self.data, backend="docker")
        calls = run.call_args_list
        name = calls[0].args[0][calls[0].args[0].index("--name") + 1]
        self.assertEqual(calls[0].args[0][:2], ["docker", "create"])
        self.assertEqual(calls[1].args[0], ["docker", "start", "--attach", name])
        self.assertEqual(calls[2].args[0], ["docker", "rm", "-f", name])
        self.assertEqual([call.kwargs["timeout"] for call in calls], [10, 2, 5])
        self.assertEqual(result["observation"], "STDOUT:\nresult")

    def test_failed_docker_cleanup_is_not_hidden(self):
        """清理失败必须停止执行；不能作为可重试观察交给模型继续调用。"""
        good = SimpleNamespace(stdout="", stderr="", returncode=0)
        bad = SimpleNamespace(stdout="", stderr="cleanup failed", returncode=1)
        with patch("tools.subprocess.run", side_effect=[good, good, bad]):
            with self.assertRaisesRegex(RuntimeError, "Docker cleanup failed"):
                execute_tool("run_python", {"code": "pass"}, data_root=self.data, backend="docker")

    def test_docker_cleanup_timeout_stops_instead_of_returning_observation(self):
        """执行结束不代表清理成功；清理超时应停止，而不是鼓励下一次模型调用。"""
        good = SimpleNamespace(stdout="", stderr="", returncode=0)
        with patch("tools.subprocess.run", side_effect=[good, good, subprocess.TimeoutExpired("docker rm", 5)]):
            with self.assertRaisesRegex(RuntimeError, "cleanup unconfirmed"):
                execute_tool("run_python", {"code": "pass"}, data_root=self.data, backend="docker")

    def test_verify_requires_successful_new_calculation_not_listing(self):
        task = self.tasks[0]["input"]
        answer = json.dumps(self.tasks[0]["evaluation"]["reference_answer"])
        replies = [response(name="list_files"), response(answer),
                   response(name="list_files"), response(answer)]
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=replies))))
        result = run_agent(client, task, policy="verify",
                           tool_executor=lambda name, args: {"status": "success", "observation": "files"})
        self.assertEqual(result["error"]["type"], "verification_missing")

    def test_verify_never_sees_hidden_answers(self):
        requests = []

        class RecordingClient(ScriptedClient):
            def create(self, **kwargs):
                requests.append(copy.deepcopy(kwargs))
                return super().create(**kwargs)

        run_experiment(RecordingClient, self.tasks[:1], self.data, self.root / "runs", policy="verify",
            kind="scripted-demo", model="scripted-demo", backend="local")
        self.assertNotIn("reference_answer", json.dumps(requests))
        self.assertEqual(len(requests), 5)
        self.assertIn("independently check", requests[3]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
