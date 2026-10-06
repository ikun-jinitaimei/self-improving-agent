"""实验组织层：对照策略交错执行，事件/快照持久化，评分不回传模型。

run_experiment 保留单策略 API；run_experiments 是两策略共享数据的组织入口。
按固定 seed 打乱题目，交替先后策略，减少总先跑 baseline 的时间顺序偏差。
这不是统计显著性保证；供应商变化、低样本量和共享题型仍需审慎解释。
"""

import contextlib
import hashlib
import importlib.metadata
import io
import platform
import random
from functools import partial
from pathlib import Path

from agent import PROJECT_ROOT, run_agent
from benchmark_suite import SUITE_VERSION, suite_fingerprint
from evaluate import evaluate_tasks
from providers import ModelSettings
from run_benchmark import summarize_results, validate_tasks
from run_io import append_event, create_run_directory, write_json
from tools import execute_tool


def make_schedule(tasks: list[dict], repeats: int, policies: list[str], seed: int) -> list[tuple]:
    """固定题目随机序和交替策略顺序；相邻配对共享相同公开输入和预算。

    每个 repeat 重新开始，无跨题记忆。独立 Random 不污染生成器的随机状态。
    每个任务都保留配对身份（task_id, repeat），报告不依赖文件排列顺序。
    """
    pairs = [(task, repeat) for repeat in range(1, repeats + 1) for task in tasks]
    random.Random(seed).shuffle(pairs)
    schedule = []
    for index, (task, repeat) in enumerate(pairs):
        order = policies if index % 2 == 0 else list(reversed(policies))
        schedule.extend((policy, task, repeat) for policy in order)
    return schedule


def run_experiments(client_factory, tasks: list[dict], data_root: Path, output_root: Path,
                    *, policies: list[str], kind: str, model: str, backend: str,
                    repeats: int = 1, max_steps: int = 6, schedule_seed: int = 0,
                    settings: ModelSettings | None = None, secrets: tuple[str, ...] = (),
                    max_api_calls: int | None = None) -> list[Path]:
    """运行一个或两个策略；完整失败和未完成记录不被摘要隐藏。

    settings 只包含公开参数；secrets 只用于文件脱敏，不能进入 manifest 或任务。
    实验目录预先写 manifest；每次事件追加 JSONL，checkpoint 更新 JSON 快照。
    不做自动网络重试，不用 evaluator 反馈触发反思，不把脚本答案当模型结果。
    """
    validate_tasks(tasks)
    if repeats < 1 or max_steps < 1:
        raise ValueError("repeats 和 max_steps 必须为正整数")
    if kind not in {"scripted-demo", "live"} or backend not in {"local", "docker"}:
        raise ValueError("实验来源或执行后端无效")
    if not policies or len(set(policies)) != len(policies) or set(policies) - {"baseline", "verify"}:
        raise ValueError("策略必须为不重复的 baseline / verify")
    # 总请求上界由调度数 × 单题步数计算；SDK 重试关闭，因此没有隐式额外请求。
    # 先拒绝超出预算的整批计划，不在半途中截断某个配对而改变实验设计。
    planned_calls = len(tasks) * repeats * len(policies) * max_steps
    if max_api_calls is not None:
        if type(max_api_calls) is not int or max_api_calls < 1:
            raise ValueError("max_api_calls 必须为正整数")
        if planned_calls > max_api_calls:
            raise ValueError(f"计划最多 {planned_calls} 次请求，超过上限 {max_api_calls}")
    settings = settings or ModelSettings(model=model)
    schedule = make_schedule(tasks, repeats, policies, schedule_seed)
    source_names = [
        "agent.py", "tools.py", "evaluate.py", "run_io.py", "run_benchmark.py",
        "benchmark_suite.py", "execution.py", "offline_model.py", "providers.py",
        "experiments.py", "reporting.py", "demo.py", "sessions.py",
    ]
    sources = {name: (PROJECT_ROOT / name).read_text(encoding="utf-8") for name in source_names}
    data_hashes = {name: hashlib.sha256((data_root / name).read_bytes()).hexdigest()
                   for name in sorted({task["input"]["data_file"] for task in tasks})}
    fingerprint = suite_fingerprint(tasks, data_root)
    states = {}
    for policy in policies:
        directory = create_run_directory(output_root)
        manifest = {
            "schema_version": 1, "kind": kind, "policy": policy, "model": settings.model,
            "provider_settings": settings.public(), "suite_version": SUITE_VERSION,
            "suite_fingerprint": fingerprint, "repeats": repeats, "max_steps": max_steps,
            "schedule_seed": schedule_seed, "backend": backend,
            "max_api_calls": max_api_calls, "planned_api_calls": planned_calls,
            "status": "running", "stop_reason": None,
            "schedule": [{"policy": p, "task_id": t["task_id"], "repeat": r} for p, t, r in schedule],
            "python": platform.python_version(), "openai_version": importlib.metadata.version("openai"),
            "tasks": tasks, "data_sha256": data_hashes, "source_snapshot": sources,
            "measurement": "scripted fault injection; not model quality" if kind == "scripted-demo" else "actual model requests",
        }
        write_json(directory / "manifest.json", manifest, secrets=secrets)
        states[policy] = {"directory": directory, "manifest": manifest,
                          "records": [], "predictions": []}
    stop_reason = None
    execute = partial(execute_tool, data_root=data_root, backend=backend)
    for policy, task, repeat in schedule:
        state = states[policy]
        directory = state["directory"]
        metadata = {key: task[key] for key in ("task_id", "category", "split")}
        metadata.update({"repeat": repeat, "policy": policy, "kind": kind, "input": task["input"]})
        stem = f"{task['task_id']}__r{repeat:02d}"
        trace_path = directory / f"{stem}.json"

        def save(record):
            write_json(trace_path, {**metadata, **record}, secrets=secrets)

        def event_sink(event):
            append_event(directory / f"{stem}.events.jsonl",
                         {**metadata, **event}, secrets=secrets)

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_agent(client_factory(task["input"]), task["input"],
                    max_steps, model=settings.model, checkpoint=save, tool_executor=execute,
                    policy=policy, settings=settings, on_event=event_sink)
        except KeyboardInterrupt:
            # 中断是明确的操作事件，不是成功交卷。保留已经写下的未完成轨迹，
            # 下面统一把所有策略 manifest 标记为 aborted，并保留完整分母。
            # 只捕获 Ctrl+C，不吞掉内部编程错误；强制杀进程仍需另行检查容器。
            stop_reason = {"type": "KeyboardInterrupt", "task_id": task["task_id"],
                           "repeat": repeat, "policy": policy}
            print("Experiment interrupted; remaining attempts were NOT run.")
            break
        prediction = {"task_id": task["task_id"], "answer": result["answer"]}
        result["evaluation"] = evaluate_tasks([task], [prediction] if result["answer"] is not None else [])[0]
        save(result)
        state["records"].append({**metadata, **result})
        state["predictions"].append({**prediction, "repeat": repeat, "status": result["status"]})
        summary = summarize_results(state["records"], len(tasks) * repeats)
        summary.update({"kind": kind, "policy": policy})
        write_json(directory / "predictions.json", state["predictions"], secrets=secrets)
        write_json(directory / "summary.json", summary, secrets=secrets)
        verdict = "PASS" if result["evaluation"]["passed"] else "FAIL"
        print(f"[{policy} r{repeat} {verdict}] {task['task_id']} | "
              f"steps={result['steps']} tools={result['tool_calls']}")
        error = result["error"]
        # 认证/权限/模型端点错误与具体题目无关，继续跑不会提供有用实验信息。
        # 只处理明确的供应商失败类型；普通答错、工具错误与预算耗尽仍照计划运行。
        if error and error["type"] == "api_error" and error["message"] in {
            "AuthenticationError", "PermissionDeniedError", "NotFoundError"
        }:
            stop_reason = {"type": error["message"], "task_id": task["task_id"],
                           "repeat": repeat, "policy": policy}
            print(f"Experiment stopped: {error['message']}; remaining attempts were NOT run.")
            break
    # 即使第一个请求就失败，所有策略目录仍有摘要；未开始的策略也有完整分母。
    # manifest 明确区分 aborted 与 completed，不能把已保存文件误当完整实验。
    for policy, state in states.items():
        manifest = state["manifest"]
        manifest.update(status="aborted" if stop_reason else "completed", stop_reason=stop_reason)
        summary = summarize_results(state["records"], len(tasks) * repeats)
        summary.update(kind=kind, policy=policy, status=manifest["status"], stop_reason=stop_reason)
        write_json(state["directory"] / "manifest.json", manifest, secrets=secrets)
        write_json(state["directory"] / "summary.json", summary, secrets=secrets)
        write_json(state["directory"] / "predictions.json", state["predictions"], secrets=secrets)
    return [states[policy]["directory"] for policy in policies]


def run_experiment(client_factory, tasks: list[dict], data_root: Path, output_root: Path,
                   *, policy: str, kind: str, model: str, backend: str,
                   repeats: int = 1, max_steps: int = 6, **options) -> Path:
    """兼容原单策略 API；实际组织逻辑只保留一份，避免两条路径逐渐分叉。"""
    return run_experiments(client_factory, tasks, data_root, output_root,
        policies=[policy], kind=kind, model=model, backend=backend,
        repeats=repeats, max_steps=max_steps, **options)[0]
