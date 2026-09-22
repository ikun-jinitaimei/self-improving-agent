"""批量实验入口：运行任务、逐步保存轨迹、评分并汇总。

使用方式：python run_benchmark.py
只测试一道题：python run_benchmark.py --task-id task_001
代码的三个职责很明确：agent 做题、evaluate 判题、本文件组织并记录实验。
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from uuid import uuid4

from agent import DEFAULT_MODEL, PROJECT_ROOT, create_client, run_agent
from evaluate import evaluate_tasks, load_json_array, print_report


def create_run_directory(root: Path) -> Path:
    """时间方便人阅读，随机后缀避免同一秒运行两次时覆盖旧结果。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / f"{stamp}_{uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def write_json(path: Path, value: object) -> None:
    """先完整写临时文件，再替换目标，降低中途退出留下半个 JSON 的可能。

    ensure_ascii=False 保留可读中文；allow_nan=False 拒绝不规范的数字。
    回调反复保存的是同一题的最新状态，不会新建大量零碎文件。
    """
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    # 防止当前密钥被意外嵌入错误信息。不要依靠它替代执行环境隔离。
    secret = os.environ.get("DEEPSEEK_API_KEY")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_tasks(tasks: list[dict]) -> None:
    """在花费 API 额度前拒绝空任务、重复 ID、危险文件名和缺失字段。"""
    if not tasks:
        raise ValueError("任务列表不能为空")
    seen = set()
    for task in tasks:
        task_id = task["task_id"]
        if not re.fullmatch(r"task_[A-Za-z0-9_-]+", task_id) or task_id in seen:
            raise ValueError(f"任务 ID 非法或重复：{task_id}")
        seen.add(task_id)
        public = task["input"]
        for key in ("question", "data_file", "answer_format"):
            if key not in public:
                raise ValueError(f"{task_id} 缺少 input.{key}")
        if not public["answer_format"] or any(
            kind not in {"string", "number", "integer"}
            for kind in public["answer_format"].values()
        ):
            raise ValueError(f"{task_id} 的答案格式无效")
        if set(public["answer_format"]) != set(task["evaluation"]["reference_answer"]):
            raise ValueError(f"{task_id} 公开格式与标准答案字段不一致")


def run_benchmark(client, tasks: list[dict], output_root: Path,
                  model: str = DEFAULT_MODEL, max_steps: int = 6) -> Path:
    """逐题执行，失败题也进入分母，避免只统计成功交卷的题而夸大成绩。

    client 可以是真实 SDK，也可以是测试替身。替身仅用于验证工程流程，不能
    把测试得到的分数称为 DeepSeek baseline。
    """
    validate_tasks(tasks)
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps 必须为正整数")
    directory = create_run_directory(output_root)
    print(f"Run directory: {directory}")
    # manifest 固定本次任务和运行配置。代码/数据哈希用于辨别版本是否变化；
    # 这里保存代码文本快照，使尚未 git commit 的实验也能回溯到具体实现。
    source_names = ["agent.py", "tools.py", "evaluate.py", "run_benchmark.py", "requirements.txt"]
    sources = {name: (PROJECT_ROOT / name).read_text(encoding="utf-8") for name in source_names}
    data_hashes = {}
    for task in tasks:
        path = (PROJECT_ROOT / task["input"]["data_file"]).resolve()
        path.relative_to(PROJECT_ROOT / "data")
        data_hashes[task["input"]["data_file"]] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(directory / "manifest.json", {
        "created_at": datetime.now(timezone.utc).isoformat(), "model": model,
        "max_steps": max_steps, "python": sys.version,
        "openai_version": importlib.metadata.version("openai"),
        "tasks": tasks, "source_snapshot": sources, "data_sha256": data_hashes,
        "scope": "controlled local smoke benchmark; no OS sandbox",
    })
    records, predictions = [], []
    for task in tasks:
        task_id = task["task_id"]
        print(f"\n=== {task_id} ===")
        trace_path = directory / f"{task_id}.json"

        def save(record):
            # 只有 input 进入模型；evaluation 由本地 evaluator 在结束后使用。
            write_json(trace_path, {"task_id": task_id, "input": task["input"], **record})

        result = run_agent(client, task["input"], max_steps=max_steps,
                           model=model, checkpoint=save)
        prediction = {"task_id": task_id, "answer": result["answer"]}
        evaluation = evaluate_tasks([task], [prediction] if result["answer"] is not None else [])[0]
        result["evaluation"] = evaluation
        save(result)
        records.append(result)
        if result["answer"] is not None:
            predictions.append(prediction)
        write_json(directory / "predictions.json", predictions)
        # 每题完成后更新汇总；批次中断时已完成题目的成绩仍然存在。
        results = [record["evaluation"] for record in records]
        passed = sum(item["passed"] for item in results)
        known_usage = [record["usage"] for record in records if record["usage"] is not None]
        summary = {
            "status": "completed" if len(records) == len(tasks) else "running",
            "task_count": len(tasks), "tasks_finished": len(records),
            "passed": passed, "success_rate": passed / len(tasks),
            "failed_tasks": [item["task_id"] for item in results if not item["passed"]],
            "average_steps": sum(r["steps"] for r in records) / len(records),
            "tool_calls": sum(r["tool_calls"] for r in records),
            "invalid_tool_calls": sum(r["invalid_tool_calls"] for r in records),
            "execution_failures": sum(r["execution_failures"] for r in records),
            "latency_seconds": sum(r["latency_seconds"] for r in records),
            "usage": ({key: sum(u[key] for u in known_usage) for key in known_usage[0]}
                      if known_usage else None),
            "usage_complete": all(r["usage_complete"] for r in records),
            "results": results,
        }
        write_json(directory / "summary.json", summary)
    print_report(summary["results"])
    return directory


def main() -> int:
    """命令行参数只用于实验配置；密钥始终从当前进程环境变量读取。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", help="可选，只运行指定任务")
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-steps", type=int, default=6)
    args = parser.parse_args()
    tasks = load_json_array(PROJECT_ROOT / "tasks" / "tasks.json")
    if args.task_id:
        tasks = [t for t in tasks if t["task_id"] == args.task_id]
    validate_tasks(tasks)
    try:
        client = create_client()
    except RuntimeError as error:
        print(f"无法开始真实实验：{error}")
        return 2
    with client:
        directory = run_benchmark(client, tasks, PROJECT_ROOT / "runs", args.model, args.max_steps)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    return 0 if summary["passed"] == summary["task_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
