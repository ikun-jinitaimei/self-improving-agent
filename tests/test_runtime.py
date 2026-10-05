"""模型边界、生命周期和自由会话的验收；所有模型回复均为 mock。

这些测试验证协议和存储，不验证 DeepSeek 能力。CSV 子进程只运行我们明确
编写的受控代码；Docker 是否可用仍需单独的集成实验。
"""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx2
from openai import AuthenticationError, NotFoundError, PermissionDeniedError

from agent import run_agent
from benchmark_suite import build_suite, task_ids
from demo import main, probe
from experiments import make_schedule, run_experiments
from offline_model import ScriptedClient
from providers import ModelSettings, configured_client, load_settings, request_completion, resolve_key, save_key
from reporting import compare_runs, load_run, summarize
from run_io import append_event, write_json
from sessions import replay_session, run_session
from test_demo import response


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = io.StringIO()
        context = contextlib.redirect_stdout(self.output)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def config(self, data):
        """外部配置测试都走真实 JSON 文件读取边界。"""
        path = self.root / "model.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_config_override_and_provider_parameters(self):
        settings = load_settings(self.config({"provider": "compatible", "extra_body": {},
            "base_url": "http://localhost:8000/v1"}), model="local-test")
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock())))
        request_completion(client, [{"role": "user", "content": "hello"}], [], settings)
        options = client.chat.completions.create.call_args.kwargs
        self.assertEqual(options["model"], "local-test")
        self.assertEqual(options["extra_body"], {})
        self.assertNotIn("tools", options)
        self.assertNotIn("api_key", settings.public())

    def test_config_rejects_secrets_bad_urls_and_bad_numbers(self):
        invalid = [{"api_key": "not-a-real-key"}, {"base_url": "http://remote.example/v1"},
            {"base_url": "https://username:password@example.com/v1"},
            {"base_url": "https://example.com/v1?key=secret"},
            {"max_tokens": True}, {"timeout_seconds": float("nan")},
            {"temperature": -1}, {"extra_body": {"messages": []}}]
        for data in invalid:
            with self.subTest(config=data), self.assertRaises(ValueError):
                load_settings(self.config(data))

    def test_key_file_and_environment_precedence(self):
        settings = ModelSettings(key_env="TEST_AGENT_KEY")
        path = self.root / "credentials.private.json"
        save_key(settings, path, "fake-file-key")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_key(settings, path), "fake-file-key")
            with configured_client(settings, path) as client:
                self.assertEqual(client.max_retries, 0)
                self.assertEqual(client.api_key, "fake-file-key")
        with patch.dict(os.environ, {"TEST_AGENT_KEY": "fake-environment-key"}):
            self.assertEqual(resolve_key(settings, path), "fake-environment-key")
        with self.assertRaises(ValueError):
            save_key(settings, self.root / "public.json", "fake-key")

    def test_private_key_redacted_in_json_and_jsonl(self):
        secret = "fake-file-key"
        snapshot, events = self.root / "trace.json", self.root / "events.jsonl"
        write_json(snapshot, {"text": secret}, secrets=(secret,))
        append_event(events, {"text": secret}, secrets=(secret,))
        self.assertEqual(json.loads(snapshot.read_text())["text"], "[REDACTED]")
        self.assertEqual(json.loads(events.read_text())["text"], "[REDACTED]")

    def test_configure_uses_hidden_input_and_never_network(self):
        destination = self.root / "credentials.private.json"
        with patch("demo.getpass.getpass", return_value="fake-input-key"), patch("demo.configured_client") as client:
            self.assertEqual(main(["configure", "--key-file", str(destination)]), 0)
        client.assert_not_called()
        self.assertEqual(json.loads(destination.read_text())["DEEPSEEK_API_KEY"], "fake-input-key")
        self.assertNotIn("fake-input-key", self.output.getvalue())

    def test_doctor_does_not_request_api_or_expose_credentials(self):
        destination = self.root / "credentials.private.json"
        save_key(ModelSettings(), destination, "fake-doctor-key")
        with patch("demo.check_docker", side_effect=RuntimeError("Docker unavailable")), patch("demo.configured_client") as client:
            self.assertEqual(main(["doctor", "--key-file", str(destination)]), 0)
        client.assert_not_called()
        result = json.loads(self.output.getvalue())
        self.assertTrue(result["key_available"])
        self.assertNotIn("fake-doctor-key", self.output.getvalue())

    def test_dry_run_checks_task_and_never_creates_files_or_network(self):
        destination = self.root / "no-experiment"
        with patch("demo.configured_client") as client:
            self.assertEqual(main(["run", "--policy", "both", "--repeats", "3", "--dry-run",
                                   "--max-api-calls", "432",
                                   "--output", str(destination)]), 0)
        client.assert_not_called()
        result = json.loads(self.output.getvalue())
        self.assertEqual(result["max_api_calls"], 12 * 3 * 2 * 6)
        self.assertEqual(result["request_limit"], 432)
        self.assertFalse(destination.exists())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["run", "--task-id", "task_unknown", "--dry-run"])

    def test_schedule_is_repeatable_balanced_and_keeps_pairs_adjacent(self):
        tasks = [{"task_id": str(n)} for n in range(4)]
        schedule = make_schedule(tasks, 3, ["baseline", "verify"], 7)
        self.assertEqual(schedule, make_schedule(tasks, 3, ["baseline", "verify"], 7))
        self.assertNotEqual(schedule, make_schedule(tasks, 3, ["baseline", "verify"], 91))
        self.assertEqual(len(schedule), 24)
        self.assertEqual(sum(schedule[n][0] == "baseline" for n in range(0, 24, 2)), 6)
        for index in range(0, len(schedule), 2):
            self.assertEqual(schedule[index][1:], schedule[index + 1][1:])
            self.assertNotEqual(schedule[index][0], schedule[index + 1][0])

    def test_cli_rejects_over_budget_plan_before_files_or_client(self):
        """dry-run 与真实入口都不能绕过上限；拒绝发生在付费请求之前。"""
        destination = self.root / "over-budget"
        for tail in ([], ["--dry-run"]):
            with patch("demo.configured_client") as client, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["run", "--policy", "both", "--output", str(destination), *tail])
            client.assert_not_called()
        self.assertFalse(destination.exists())

    def test_runner_request_cap_is_checked_before_creating_directory(self):
        """直接调用 Python 组织层也检查预算，不能只依赖 CLI 校验。"""
        data = self.root / "budget-data"
        tasks = build_suite(data)[:1]
        destination = self.root / "budget-runs"
        factory = Mock()
        with self.assertRaisesRegex(ValueError, "超过上限"):
            run_experiments(factory, tasks, data, destination, policies=["baseline", "verify"],
                            kind="live", model="mock", backend="local", max_api_calls=11)
        factory.assert_not_called()
        self.assertFalse(destination.exists())

    def test_permanent_api_errors_stop_batch_preserve_denominators_and_redact(self):
        """替身模拟三类不可继续的 API 错误；不发送 HTTP，不执行模型代码。"""
        data = self.root / "auth-data"
        tasks = build_suite(data)[:2]
        for error_type, code in ((AuthenticationError, 401), (PermissionDeniedError, 403), (NotFoundError, 404)):
            with self.subTest(error=error_type.__name__):
                error = error_type("fake-private-key", response=httpx2.Response(code,
                    request=httpx2.Request("POST", "https://example.com")), body=None)
                create = Mock(side_effect=error)
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                directories = run_experiments(lambda task: client, tasks, data, self.root / error_type.__name__,
                    policies=["baseline", "verify"], kind="live", model="mock", backend="local", max_api_calls=24)
                self.assertEqual(create.call_count, 1)
                metrics = []
                for directory in directories:
                    manifest, records = load_run(directory)
                    self.assertEqual(manifest["status"], "aborted")
                    self.assertEqual(manifest["stop_reason"]["type"], error_type.__name__)
                    metric = summarize(manifest, records)
                    metrics.append(metric)
                    self.assertEqual(metric["expected"], 2)
                    self.assertEqual(metric["passed"], 0)
                    summary = json.loads((directory / "summary.json").read_text())
                    self.assertEqual(summary["task_count"], 2)
                    self.assertFalse(summary["usage_complete"])
                    self.assertNotIn("fake-private-key", (directory / "manifest.json").read_text())
                self.assertEqual(sum(m["finished"] for m in metrics), 1)
                self.assertEqual(sum(m["failures"].get("not_started", 0) for m in metrics), 3)
                with self.assertRaisesRegex(ValueError, "尚未完成"):
                    compare_runs(*directories)

    def test_cli_aborted_experiment_saves_reports_but_not_comparison(self):
        """用户入口返回失败码，同时保留可查看报告；不能生成误导性对照结论。"""
        error = AuthenticationError("fake-private-key", response=httpx2.Response(401,
                    request=httpx2.Request("POST", "https://example.com")), body=None)
        create = Mock(side_effect=error)
        client = SimpleNamespace(api_key="fake-private-key", chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        destination = self.root / "cli-auth"
        with patch("demo.check_docker"), patch("demo.configured_client", return_value=contextlib.nullcontext(client)):
            self.assertEqual(main(["run", "--policy", "both", "--task-id", "task_dev_7_monthly",
                                   "--output", str(destination)]), 1)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(len(list(destination.glob("*/experiments/*/report.md"))), 2)
        self.assertFalse(list(destination.glob("*/comparison.md")))

    def test_task_plan_matches_generated_suite(self):
        for split in ("dev", "holdout", "both"):
            tasks = build_suite(self.root / split, split)
            self.assertEqual(task_ids(split), {t["task_id"] for t in tasks})

    def test_events_have_lifecycle_and_do_not_enter_model_messages(self):
        task = {"question": "Count rows", "data_file": "test.csv", "answer_format": {"rows": "integer"}}
        calls, emitted = [], []
        replies = iter([response(name="run_python", arguments='{"code":"print(2)"}'), response('{"rows":2}')])

        def completion(**options):
            calls.append(copy.deepcopy(options))
            return next(replies)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
        result = run_agent(client, task, on_event=emitted.append,
                           tool_executor=lambda name, args: {"status": "success", "observation": "2"})
        kinds = [e["type"] for e in emitted]
        self.assertEqual(kinds, ["agent_start", "turn_start", "model_response", "tool_start", "tool",
                                 "turn_end", "turn_start", "model_response", "turn_end", "agent_end"])
        self.assertEqual([e["sequence"] for e in emitted], list(range(1, 11)))
        self.assertEqual(result["events"], emitted)
        self.assertNotIn("agent_start", json.dumps(calls))

    def test_failed_session_still_has_terminal_event(self):
        task = {"question": "Count rows", "data_file": "test.csv", "answer_format": {"rows": "integer"}}
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response('{"rows":2}')))))
        result = run_agent(client, task)
        self.assertEqual(result["error"]["type"], "protocol_error")
        self.assertEqual(result["events"][-1]["type"], "agent_end")
        self.assertEqual(result["events"][-1]["status"], "failed")

    def test_paired_runner_persists_actual_order_and_event_snapshots(self):
        data = self.root / "data"
        tasks = build_suite(data, "dev")[:2]
        received = []

        def factory(task_input):
            received.append(task_input)
            return ScriptedClient(task_input)

        directories = run_experiments(factory, tasks, data, self.root / "runs", policies=["baseline", "verify"],
            kind="scripted-demo", model="scripted-demo", backend="local", schedule_seed=7)
        left, records = load_run(directories[0])
        right, _ = load_run(directories[1])
        self.assertEqual(left["schedule"], right["schedule"])
        by_id = {t["task_id"]: t["input"] for t in tasks}
        self.assertEqual(received, [by_id[item["task_id"]] for item in left["schedule"]])
        for directory in directories:
            for path in directory.glob("*.events.jsonl"):
                events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(events[0]["type"], "agent_start")
                self.assertEqual(events[-1]["type"], "agent_end")
                self.assertEqual(len(events), len(next(r for r in load_run(directory)[1] if r["task_id"] == events[0]["task_id"])["events"]))
        self.assertIn("recovery: 1", compare_runs(*directories))
        self.assertEqual(len(records), 2)

    def test_free_session_copies_only_csv_no_grade_and_replays_without_api(self):
        source = self.root / "arbitrary.csv"
        source.write_text("x\n1\n2\n", encoding="utf-8")
        replies = [response(name="run_python", arguments=json.dumps({"code": "import csv; print(len(list(csv.DictReader(open('input.csv')))))"})),
                   response('{"rows":2}')]
        create = Mock(side_effect=replies)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        directory = run_session(client, "统计行数", source, {"rows": "integer"}, self.root / "runs",
                                ModelSettings(model="mock"), backend="local", json_events=True)
        record = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(record["answer"], {"rows": 2})
        self.assertNotIn("evaluation", record)
        self.assertEqual(list((directory / "data").iterdir()), [directory / "data" / "input.csv"])
        self.assertEqual((directory / "data" / "input.csv").read_bytes(), source.read_bytes())
        before = create.call_count
        self.assertIn("not graded", replay_session(directory))
        self.assertEqual(create.call_count, before)
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(events[-1]["type"], "session_saved")

    def test_probe_has_no_tools_small_budget_and_redacts_key(self):
        create = Mock(return_value=response("fake-probe-key"))
        client = SimpleNamespace(api_key="fake-probe-key", chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.assertEqual(probe(client, ModelSettings(), self.root / "runs"), 0)
        options = create.call_args.kwargs
        self.assertNotIn("tools", options)
        self.assertEqual(options["max_tokens"], 16)
        record = json.loads(next((self.root / "runs").glob("*/probe.json")).read_text())
        self.assertEqual(record["content"], "[REDACTED]")
        self.assertIsNone(record["usage"])

    def test_cli_internal_typeerror_is_not_hidden(self):
        with patch("demo.execute", side_effect=TypeError("programming error")), self.assertRaises(TypeError):
            main(["demo"])


if __name__ == "__main__":
    unittest.main()
