"""实验分析与 Markdown 报告：所有成绩从磁盘轨迹推导，不编造模型指标。

报告不包含标准答案、代码快照或完整消息，方便审阅后分享。inspect 单题才
展示完整轨迹，并明确这是本地材料。对比只接受同源、同题、同预算的完整实验。
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def load_run(directory: Path) -> tuple[dict, list[dict]]:
    """读取新版实验；不猜测缺失来源，也不把旧手写预测升级成真实模型结果。"""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("kind") not in {"live", "scripted-demo"}:
        raise ValueError("只支持带 schema_version=1 和明确来源的新实验")
    records = [json.loads(path.read_text(encoding="utf-8"))
               for path in sorted(directory.glob("task_*__r*.json"))]
    # 日志也是外部输入。只在读取边界检查样本身份，防止重复/额外轨迹让
    # 通过率超过真实分母；后面的统计函数可以直接使用已对齐的记录。
    if not manifest["tasks"] or manifest["repeats"] < 1:
        raise ValueError("manifest 中任务与 repeats 必须非空/为正")
    expected = {(task["task_id"], repeat) for task in manifest["tasks"]
                for repeat in range(1, manifest["repeats"] + 1)}
    seen = set()
    for record in records:
        key = (record["task_id"], record["repeat"])
        if key not in expected or key in seen:
            raise ValueError("轨迹包含额外或重复的 task_id/repeat")
        if record["kind"] != manifest["kind"] or record["policy"] != manifest["policy"]:
            raise ValueError("轨迹来源/策略与 manifest 不一致")
        seen.add(key)
    return manifest, records


def failure_reason(record: dict) -> str:
    """区分运行失败和合法但错误的答案；失败原因不靠输出中某个英文单词猜测。"""
    if record["status"] == "running" or "evaluation" not in record:
        return "unfinished"
    if record["error"]:
        return record["error"]["type"]
    if not record["evaluation"]["passed"]:
        return "incorrect_answer"
    return "passed"


def summarize(manifest: dict, records: list[dict]) -> dict:
    """预计题数是分母；中断、未运行和执行失败不能从成功率中消失。"""
    expected = len(manifest["tasks"]) * manifest["repeats"]
    finished = [r for r in records if failure_reason(r) != "unfinished"]
    passed = sum(failure_reason(r) == "passed" for r in records)
    groups = defaultdict(lambda: {"expected": 0, "passed": 0})
    for task in manifest["tasks"]:
        groups[f"{task['split']}/{task['category']}"]["expected"] += manifest["repeats"]
    for record in records:
        groups[f"{record['split']}/{record['category']}"]["passed"] += failure_reason(record) == "passed"
    failures = Counter(failure_reason(r) for r in records if failure_reason(r) != "passed")
    if expected > len(records):
        failures["not_started"] = expected - len(records)
    known = [r["usage"] for r in records if r["usage"] is not None]
    return {
        "expected": expected, "finished": len(finished), "passed": passed,
        "success_rate": passed / expected, "groups": dict(groups), "failures": dict(failures),
        "steps": sum(r["steps"] for r in records),
        "tool_calls": sum(r["tool_calls"] for r in records),
        "invalid_tool_calls": sum(r["invalid_tool_calls"] for r in records),
        "execution_failures": sum(r["execution_failures"] for r in records),
        "latency_seconds": sum(r["latency_seconds"] for r in records),
        "known_tokens": sum(u["total_tokens"] for u in known) if known else None,
        "usage_complete": len(finished) == expected and all(r["usage_complete"] for r in records),
    }


def run_report(directory: Path) -> str:
    """生成汇总 Markdown；显著标注 scripted-demo，避免截图失去来源标签。"""
    manifest, records = load_run(directory)
    m = summarize(manifest, records)
    title = "OFFLINE SCRIPTED DEMO — NOT LLM RESULTS" if manifest["kind"] == "scripted-demo" else "LIVE MODEL EXPERIMENT"
    lines = [f"# {title}", "", f"Policy: `{manifest['policy']}` | Model: `{manifest['model']}`",
             f"Suite: `{manifest['suite_version']}` | Backend: `{manifest['backend']}`",
             f"Fingerprint: `{manifest['suite_fingerprint']}`", "",
             f"Batch status: `{manifest.get('status', 'unknown')}`",
             f"Stop reason: `{(manifest.get('stop_reason') or {}).get('type', 'none')}`", "",
             "| Metric | Value |", "| --- | ---: |",
             f"| Passed / expected | {m['passed']} / {m['expected']} |",
             f"| Finished | {m['finished']} |", f"| Success rate | {m['success_rate']:.1%} |",
             f"| Model request attempts | {m['steps']} |", f"| Tool calls | {m['tool_calls']} |",
             f"| Invalid calls / execution errors | {m['invalid_tool_calls']} / {m['execution_failures']} |",
             f"| Recorded task latency (seconds) | {m['latency_seconds']:.4f} |",
             f"| Known tokens | {m['known_tokens'] if m['known_tokens'] is not None else 'N/A'} |",
             f"| Complete usage | {m['usage_complete']} |", "", "## By split and category", "",
             "| Group | Passed / expected |", "| --- | ---: |"]
    lines += [f"| {key} | {value['passed']} / {value['expected']} |" for key, value in sorted(m["groups"].items())]
    lines += ["", "## Failures", ""]
    lines += [f"- {key}: {count}" for key, count in sorted(m["failures"].items())] or ["None."]
    lines += ["", "## Interpretation", "",
              "Synthetic tasks only; no claim of domain generalization, training or RL.",
              "Missing usage is unknown, not zero cost. Verify consumes the same total step budget."]
    if manifest["kind"] == "scripted-demo":
        lines += ["The monthly candidate is deliberately corrupted by +1. This demonstrates fault recovery;",
                  "the accuracy difference is NOT evidence that a real model improves."]
    return "\n".join(lines) + "\n"


def compare_runs(baseline: Path, candidate: Path) -> str:
    """配对比较同一任务与 repeat，拒绝 mock/live 混比和不完整的批次。

    除 policy 外的实验条件必须相同。额外步数/调用/token 单独展示；不把更高
    推理成本换来的结果称为免费改进，也不做小样本统计显著性的虚假声明。
    """
    left, a = load_run(baseline)
    right, b = load_run(candidate)
    for field in ("kind", "model", "suite_fingerprint", "repeats", "max_steps", "backend", "source_snapshot", "python", "openai_version"):
        if left[field] != right[field]:
            raise ValueError(f"不能配对：{field} 不一致")
    for field in ("provider_settings", "schedule_seed", "max_api_calls"):
        if left.get(field) != right.get(field):
            raise ValueError(f"不能配对：{field} 不一致")
    if left["policy"] != "baseline" or right["policy"] != "verify":
        raise ValueError("对比顺序必须为 baseline、verify")
    ma, mb = summarize(left, a), summarize(right, b)
    if ma["finished"] != ma["expected"] or mb["finished"] != mb["expected"]:
        raise ValueError("实验尚未完成，不能发布完整配对结论")
    indexed_a = {(r["task_id"], r["repeat"]): r for r in a}
    indexed_b = {(r["task_id"], r["repeat"]): r for r in b}
    if indexed_a.keys() != indexed_b.keys():
        raise ValueError("任务与 repeat 未对齐")
    pairs = Counter()
    for key, record in indexed_a.items():
        before, after = record["evaluation"]["passed"], indexed_b[key]["evaluation"]["passed"]
        pairs["both_pass" if before and after else "regression" if before else "recovery" if after else "both_fail"] += 1
    label = "SCRIPTED FAULT-INJECTION COMPARISON — NOT MODEL IMPROVEMENT" if left["kind"] == "scripted-demo" else "LIVE PAIRED COMPARISON"
    # 同批交替调度与分别运行都能按身份配对，但时间顺序风险不同，必须可见。
    shared = left.get("schedule") is not None and left.get("schedule") == right.get("schedule")
    order = "shared interleaved schedule" if shared else "separate batches; possible order effects"
    lines = [f"# {label}", "", f"Matched attempts: {ma['expected']}", f"Order: {order}", "",
             "| Metric | Baseline | Verify |", "| --- | ---: | ---: |"]
    for name in ("passed", "steps", "tool_calls", "execution_failures", "known_tokens"):
        before = "N/A" if ma[name] is None else ma[name]
        after = "N/A" if mb[name] is None else mb[name]
        lines.append(f"| {name} | {before} | {after} |")
    lines += [f"| success_rate | {ma['success_rate']:.1%} | {mb['success_rate']:.1%} |",
              f"| latency_seconds | {ma['latency_seconds']:.4f} | {mb['latency_seconds']:.4f} |", "",
              f"Accuracy delta: {(mb['success_rate'] - ma['success_rate']) * 100:+.1f} percentage points.", ""]
    lines += [f"- {name}: {pairs[name]}" for name in ("recovery", "regression", "both_pass", "both_fail")]
    lines += ["", "No statistical significance or generalization claim. Inspect task traces before interpreting the difference.",
              "Token totals are comparable only if both reports have complete usage."]
    return "\n".join(lines) + "\n"


def inspect_trace(path: Path) -> str:
    """本地查看完整消息；动态代码围栏避免模型文本中的反引号破坏报告结构。"""
    record = json.loads(path.read_text(encoding="utf-8"))
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", payload)), default=0) + 1)
    return f"# LOCAL TRACE — {record['task_id']} ({record.get('kind', 'unknown')})\n\n{fence}json\n{payload}\n{fence}\n"
