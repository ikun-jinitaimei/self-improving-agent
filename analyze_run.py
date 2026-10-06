"""离线分析真实实验：从已保存事件核算用量，不请求模型、不执行工具。

reporting 负责实验身份校验和正确率；本文件补充缓存用量、费用估算区间、
每题重复稳定性和失败索引。分析不会修改历史 manifest 或给模型评分反馈。
价格必须显式提供：token 数是实测值，金额是按价格快照计算的估算，不是账单。
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from reporting import failure_reason, load_run, summarize
from run_io import write_json


def response_tokens(records: list[dict]) -> dict | None:
    """从每次 model_response 的 usage 累加缓存命中、未命中和输出 token。

    Agent 的汇总 usage 保留了总输入/输出，缓存明细在事件中。缺失任何响应的
    usage 或缓存字段时，不能用 0 冒充；直接返回 None，报告费用未知。
    未完成题的已有响应仍可统计，但 usage_complete=False 的请求不估算费用。
    """
    fields = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens")
    responses = [event for record in records for event in record["events"]
                 if event["type"] == "model_response"]
    if not responses or not all(record["usage_complete"] for record in records):
        return None
    usages = [event["usage"] for event in responses]
    if any(usage is None or any(field not in usage for field in fields) for usage in usages):
        return None
    return {field: sum(usage[field] for usage in usages) for field in fields}


def estimate_cost(tokens: dict | None, prices: dict) -> dict | None:
    """价格单位为 USD / 百万 token；峰谷分别计算，避免猜测实际结算档位。

    不查询余额、不换算即时汇率，也不把估算当费用硬限制。价格快照由调用方
    明确传入；内部直接访问约定字段，错误配置应显示异常而不是套用默认价格。
    """
    if tokens is None:
        return None
    result = {}
    for period in ("off_peak", "peak"):
        rates = prices[period]
        amount = (tokens["prompt_cache_hit_tokens"] * rates["cache_hit"]
                  + tokens["prompt_cache_miss_tokens"] * rates["cache_miss"]
                  + tokens["completion_tokens"] * rates["output"]) / 1_000_000
        result[f"{period}_usd"] = round(amount, 8)
    return result


def analyze_run(directory: Path, prices: dict | None = None) -> dict:
    """复用日志读取边界；这里只分析，不重跑、覆盖或挑选成绩。

    stable_tasks 统计每道题是否所有计划重复均通过，不把同题三次运行当作
    三个独立的新任务。未开始的题仍在任务分母里，状态来自原始 manifest。
    """
    manifest, records = load_run(directory)
    tokens = response_tokens(records)
    passed_by_task = Counter(record["task_id"] for record in records
                             if failure_reason(record) == "passed")
    stable_tasks = sum(passed_by_task[task["task_id"]] == manifest["repeats"]
                       for task in manifest["tasks"])
    result = {"kind": manifest["kind"], "status": manifest["status"],
              "policy": manifest["policy"], "model": manifest["model"],
              "suite_fingerprint": manifest["suite_fingerprint"],
              "metrics": summarize(manifest, records), "response_tokens": tokens,
              "stable_tasks": stable_tasks, "task_count": len(manifest["tasks"]),
              "failures": [{"task_id": record["task_id"], "repeat": record["repeat"],
                            "reason": failure_reason(record)}
                           for record in records if failure_reason(record) != "passed"]}
    if prices is not None:
        # 费用快照只能用于匹配模型的真实实验；mock 不能被换算成实际 API 费用。
        if manifest["kind"] != "live" or manifest["model"] != prices["model"]:
            raise ValueError("价格快照只适用于匹配模型的真实实验")
        result["price_snapshot"] = prices
        result["estimated_cost"] = estimate_cost(tokens, prices)
    return result


def main() -> None:
    """默认只显示分析；显式 --output 才另外保存，不改已有实验文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--prices", type=Path, help="公开价格快照 JSON，非 API Key")
    parser.add_argument("--output", type=Path, help="可选分析 JSON 输出路径")
    args = parser.parse_args()
    prices = json.loads(args.prices.read_text(encoding="utf-8")) if args.prices else None
    result = analyze_run(args.directory, prices)
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
