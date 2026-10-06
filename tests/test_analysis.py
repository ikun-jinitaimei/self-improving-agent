"""离线验收事件用量和费用计算；测试数据不是 DeepSeek 运行结果。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analyze_run import analyze_run, estimate_cost, response_tokens


PRICES = {"off_peak": {"cache_hit": .003, "cache_miss": .15, "output": .6},
          "peak": {"cache_hit": .006, "cache_miss": .3, "output": 1.2}}


def record(usage, *, complete=True):
    """最小事件夹具只覆盖用量计算，不伪造任务评分。"""
    return {"usage_complete": complete,
            "events": [{"type": "turn_start"}, {"type": "model_response", "usage": usage}]}


class AnalysisTests(unittest.TestCase):
    def test_counts_each_response_and_cache_category(self):
        """不能把 cache hit 和 miss 全部按输入原价收费，也不能漏算多轮回复。"""
        usage = {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 200,
                 "completion_tokens": 50}
        self.assertEqual(response_tokens([record(usage), record(usage)]),
                         {"prompt_cache_hit_tokens": 200, "prompt_cache_miss_tokens": 400,
                          "completion_tokens": 100})

    def test_missing_usage_is_unknown_not_free(self):
        self.assertIsNone(response_tokens([record(None)]))
        self.assertIsNone(response_tokens([record({"completion_tokens": 10})]))
        self.assertIsNone(response_tokens([]))
        self.assertIsNone(estimate_cost(None, PRICES))

    def test_failed_request_with_partial_usage_is_unknown(self):
        usage = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 20,
                 "completion_tokens": 5}
        self.assertIsNone(response_tokens([record(usage, complete=False)]))

    def test_peak_and_off_peak_are_explicit_estimates(self):
        tokens = {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000,
                  "completion_tokens": 1_000_000}
        self.assertEqual(estimate_cost(tokens, PRICES),
                         {"off_peak_usd": .753, "peak_usd": 1.506})

    def test_real_zero_tokens_differs_from_unknown(self):
        usage = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
                 "completion_tokens": 0}
        self.assertEqual(estimate_cost(response_tokens([record(usage)]), PRICES),
                         {"off_peak_usd": 0.0, "peak_usd": 0.0})

    def test_stability_keeps_unstarted_tasks_in_denominator(self):
        """三次重复只通过两次不是稳定通过；未开始题不能悄悄从分母消失。"""
        tasks = [{"task_id": name, "split": "dev", "category": "monthly"}
                 for name in ("task_a", "task_b")]
        manifest = {"kind": "live", "status": "aborted", "policy": "baseline",
                    "model": "deepseek-flash", "suite_fingerprint": "fixture",
                    "tasks": tasks, "repeats": 3}
        records = [{"task_id": "task_a", "repeat": repeat, "split": "dev",
                    "category": "monthly", "status": "completed", "error": None,
                    "evaluation": {"passed": True}, "events": [], "steps": 2,
                    "tool_calls": 1, "invalid_tool_calls": 0, "execution_failures": 0,
                    "latency_seconds": 1, "usage": None, "usage_complete": False}
                   for repeat in (1, 2)]
        with patch("analyze_run.load_run", return_value=(manifest, records)):
            result = analyze_run(Path("fixture"))
        self.assertEqual(result["stable_tasks"], 0)
        self.assertEqual(result["task_count"], 2)
        self.assertEqual(result["metrics"]["expected"], 6)
        self.assertEqual(result["metrics"]["failures"]["not_started"], 4)

    def test_scripted_experiment_cannot_be_priced_as_real_api(self):
        """真正生成并读取离线实验，确保分析入口不会给 scripted-demo 虚构账单。"""
        from benchmark_suite import build_suite
        from experiments import run_experiment
        from offline_model import ScriptedClient

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = build_suite(root / "data")[:1]
            with contextlib.redirect_stdout(io.StringIO()):
                directory = run_experiment(ScriptedClient, tasks, root / "data", root / "runs",
                    policy="baseline", kind="scripted-demo", model="scripted-demo", backend="local")
            self.assertEqual(analyze_run(directory)["stable_tasks"], 1)
            with self.assertRaisesRegex(ValueError, "真实实验"):
                analyze_run(directory, {"model": "scripted-demo", **PRICES})


if __name__ == "__main__":
    unittest.main()
