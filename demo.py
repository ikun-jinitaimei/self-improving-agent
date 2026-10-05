"""独立研究版入口：配置 → 连通检查 → 自由问题/对照实验 → 查看记录。

    python demo.py configure                 # 本地隐藏输入密钥，不发到聊天里
    python demo.py doctor                    # 无网络检查配置与 Docker
    python demo.py probe                     # 一次真实 API 请求，不执行 Python
    python demo.py run --policy both --dry-run
    python demo.py ask "统计行数" --data data/sales.csv
    python demo.py demo                      # 无网络故障注入演示，不算模型成绩

公开配置与私有凭据分开；不自动扫描 .env 或读取编辑器的凭据存储。
"""

import argparse
import getpass
import importlib.metadata
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from openai import APIError

from benchmark_suite import build_suite, task_ids
from execution import check_docker
from experiments import run_experiments
from offline_model import ScriptedClient
from providers import configured_client, load_settings, request_completion, resolve_key, save_key
from reporting import compare_runs, inspect_trace, run_report
from run_io import create_run_directory, write_json
from sessions import replay_session, run_session


def add_model_options(cmd):
    """所有真实入口共用同一配置边界；命令行模型 ID 可覆盖公开配置。"""
    cmd.add_argument("--config", type=Path, help="公开模型配置 JSON")
    cmd.add_argument("--model", help="覆盖配置中的模型 ID")
    cmd.add_argument("--key-file", type=Path, default=Path.cwd() / "credentials.private.json")


def add_execution_options(cmd):
    """本地执行是显式风险选择，不作为缺少 Docker 时的自动回退。"""
    cmd.add_argument("--backend", choices=["docker", "local"], default="docker")
    cmd.add_argument("--allow-local-execution", action="store_true",
                     help="接受模型代码能访问宿主机文件；本地模式不是沙箱")


def parser() -> argparse.ArgumentParser:
    """声明用户入口，不让运行核心依赖 argparse 或命令行全局变量。"""
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("configure", "doctor", "probe", "ask"):
        cmd = commands.add_parser(name)
        add_model_options(cmd)
        if name in {"probe", "ask"}:
            cmd.add_argument("--output", type=Path, default=Path.cwd() / "runs")
        if name == "ask":
            cmd.add_argument("question")
            cmd.add_argument("--data", type=Path, required=True)
            formats = cmd.add_mutually_exclusive_group()
            formats.add_argument("--answer-format", help="答案字段类型 JSON；默认 answer:string")
            formats.add_argument("--answer-format-file", type=Path, help="从 JSON 文件读格式，避免终端引号转义")
            cmd.add_argument("--policy", choices=["baseline", "verify"], default="baseline")
            cmd.add_argument("--max-steps", type=int, default=6)
            cmd.add_argument("--json-events", action="store_true", help="逐行输出 JSON 事件")
            add_execution_options(cmd)
    for name in ("demo", "run"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--split", choices=["dev", "holdout", "both"], default="both" if name == "demo" else "dev")
        cmd.add_argument("--repeats", type=int, default=1)
        cmd.add_argument("--max-steps", type=int, default=6)
        cmd.add_argument("--schedule-seed", type=int, default=0)
        cmd.add_argument("--task-id")
        cmd.add_argument("--output", type=Path, default=Path.cwd() / "runs")
        if name == "run":
            cmd.add_argument("--max-api-calls", type=int, default=72,
                             help="整批计划最多请求次数，默认 72；不是人民币费用上限")
            cmd.add_argument("--policy", choices=["baseline", "verify", "both"], default="baseline")
            cmd.add_argument("--dry-run", action="store_true", help="只显示计划，不写文件或请求 API")
            add_model_options(cmd)
            add_execution_options(cmd)
    for name, argument in (("report", "directory"), ("inspect", "trace"), ("replay", "directory")):
        commands.add_parser(name).add_argument(argument, type=Path)
    cmd = commands.add_parser("compare")
    cmd.add_argument("baseline", type=Path)
    cmd.add_argument("candidate", type=Path)
    return result


def execute(args, factory, kind: str, model: str, backend: str, policies: list[str],
            *, settings=None, secrets: tuple[str, ...] = ()) -> Path:
    """一次调度交替运行策略，不是先跑完所有基线，再跑完所有候选。

    策略各有独立目录；记录完整顺序、配置和代码快照以检查对照可比性。
    参考答案只供运行结束后的 evaluator 使用。
    """
    session = create_run_directory(args.output)
    data_root = session / "data"
    tasks = build_suite(data_root, args.split)
    if args.task_id:
        tasks = [task for task in tasks if task["task_id"] == args.task_id]
    directories = run_experiments(factory, tasks, data_root, session / "experiments",
        policies=policies, kind=kind, model=model, backend=backend, settings=settings,
        repeats=args.repeats, max_steps=args.max_steps, schedule_seed=args.schedule_seed, secrets=secrets,
        max_api_calls=getattr(args, "max_api_calls", None))
    for directory in directories:
        (directory / "report.md").write_text(run_report(directory), encoding="utf-8")
    write_json(session / "session.json", {"kind": kind, "runs": [str(p) for p in directories]}, secrets=secrets)
    # 认证失败的未完成批次可查看逐策略报告，但不能发布完整配对比较。
    complete = all(json.loads((p / "summary.json").read_text(encoding="utf-8"))["status"] == "completed"
                   for p in directories)
    if len(directories) == 2 and complete:
        report = compare_runs(*directories)
        (session / "comparison.md").write_text(report, encoding="utf-8")
        print("\n" + report)
    elif len(directories) == 2:
        print("Comparison unavailable: experiment aborted; inspect individual reports.")
    print(f"Session saved: {session}")
    return session


def preflight_backend(args):
    """只做只读预检，不安装 Docker、拉镜像或自动降低执行隔离。"""
    if args.backend == "local" and not args.allow_local_execution:
        raise ValueError("本地模式不是沙箱；必须显式添加 --allow-local-execution")
    if args.backend == "docker":
        check_docker()


def probe(client, settings, output: Path) -> int:
    """一条短文本请求只确认端点/认证，不证明工具调用或任务正确。

    保存实际 token usage、延迟与失败类型；不输出 HTTP headers 或原始异常。
    max_retries=0 避免隐式请求增加花费。
    """
    start = time.monotonic()
    probe_settings = replace(settings, max_tokens=16)
    record = {"kind": "live-api-probe", "provider_settings": probe_settings.public()}
    try:
        completion = request_completion(client, [{"role": "user", "content": "Reply with OK."}], [],
                                        probe_settings)
        record.update(response_id=completion.id,
                      usage=completion.usage.model_dump() if completion.usage else None)
        if not completion.choices:
            record.update(status="failed", error_type="empty_choices")
        else:
            choice = completion.choices[0]
            record.update(status="completed" if choice.finish_reason == "stop" else "failed",
                          content=choice.message.content, finish_reason=choice.finish_reason)
    except APIError as error:
        record.update(status="failed", error_type=type(error).__name__)
    record["elapsed_seconds"] = round(time.monotonic() - start, 4)
    directory = create_run_directory(output)
    write_json(directory / "probe.json", record, secrets=(client.api_key,))
    print(f"API probe: {record['status']}; record: {directory / 'probe.json'}")
    return 0 if record["status"] == "completed" else 1


def dispatch(args) -> int:
    """编排入口；具体模型调用、工具、评分、存储分别由独立模块负责。"""
    if args.command == "demo":
        print("OFFLINE SCRIPTED DEMO: fixed responses + real CSV tools; NOT LLM results.\n")
        execute(args, ScriptedClient, "scripted-demo", "scripted-demo", "local", ["baseline", "verify"])
        return 0
    if args.command in {"configure", "doctor", "probe", "run", "ask"}:
        settings = load_settings(args.config, model=args.model)
        if args.command == "configure":
            key = getpass.getpass(f"{settings.key_env} (hidden input): ")
            save_key(settings, args.key_file, key)
            print(f"Saved locally: {args.key_file}\n明文私有文件，请勿上传；环境变量的值优先于此文件。")
            return 0
        if args.command == "doctor":
            info = {"provider_settings": settings.public(), "key_available": bool(resolve_key(settings, args.key_file)),
                    "openai_version": importlib.metadata.version("openai")}
            try:
                check_docker()
                info["docker"] = "ready"
            except RuntimeError as error:
                info["docker"] = str(error)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run" and args.dry_run:
            policies = ["baseline", "verify"] if args.policy == "both" else [args.policy]
            count = 1 if args.task_id else len(task_ids(args.split))
            print(json.dumps({"kind": "plan-only", "provider_settings": settings.public(),
                "tasks": count, "repeats": args.repeats, "policies": policies, "backend": args.backend,
                "max_api_calls": count * args.repeats * len(policies) * args.max_steps,
                "request_limit": args.max_api_calls,
                "schedule_seed": args.schedule_seed, "note": "upper bound, not measured cost or results"}, indent=2))
            return 0
        answer_format = None
        if args.command == "ask":
            text = args.answer_format_file.read_text(encoding="utf-8") if args.answer_format_file else args.answer_format or '{"answer":"string"}'
            answer_format = json.loads(text)
        # 会话输入只在 run_session 边界校验；客户端构造不会请求 API。
        if args.command in {"run", "ask"}:
            preflight_backend(args)
        with configured_client(settings, args.key_file) as client:
            if args.command == "probe":
                return probe(client, settings, args.output)
            if args.command == "ask":
                directory = run_session(client, args.question, args.data, answer_format, args.output,
                    settings, backend=args.backend, policy=args.policy, max_steps=args.max_steps,
                    json_events=args.json_events, secrets=(client.api_key,))
                record = json.loads((directory / "session.json").read_text(encoding="utf-8"))
                return 0 if record["status"] == "completed" else 1
            policies = ["baseline", "verify"] if args.policy == "both" else [args.policy]
            session = execute(args, lambda task_input: client, "live", settings.model,
                              args.backend, policies, settings=settings, secrets=(client.api_key,))
        info = json.loads((session / "session.json").read_text(encoding="utf-8"))
        summaries = [json.loads((Path(path) / "summary.json").read_text(encoding="utf-8")) for path in info["runs"]]
        return 0 if all(s["passed"] == s["task_count"] for s in summaries) else 1
    if args.command == "report":
        print(run_report(args.directory))
    elif args.command == "compare":
        print(compare_runs(args.baseline, args.candidate))
    elif args.command == "replay":
        print(replay_session(args.directory))
    else:
        print(inspect_trace(args.trace))
    return 0


def main(argv=None) -> int:
    """只处理外部配置错误；内部 TypeError 等编程错误继续暴露 traceback。"""
    arg_parser = parser()
    args = arg_parser.parse_args(argv)
    if args.command in {"demo", "run", "ask"} and args.max_steps < 1:
        arg_parser.error("max-steps 必须为正整数")
    if args.command in {"demo", "run"}:
        if args.repeats < 1:
            arg_parser.error("repeats 必须为正整数")
        if args.task_id and args.task_id not in task_ids(args.split):
            arg_parser.error("task-id 不在当前 split 中")
    if args.command == "run":
        # 统一在命令行输入边界检查；dry-run 也受同一限制，保证预览与实际一致。
        count = 1 if args.task_id else len(task_ids(args.split))
        policy_count = 2 if args.policy == "both" else 1
        planned = count * args.repeats * policy_count * args.max_steps
        if args.max_api_calls < 1 or planned > args.max_api_calls:
            arg_parser.error(f"计划最多 {planned} 次请求，超过/无效上限 {args.max_api_calls}；"
                             "减少任务、repeats 或显式调整 --max-api-calls")
    try:
        return dispatch(args)
    except (RuntimeError, ValueError, OSError) as error:
        print(f"Configuration error: {error}")
        return 2


def cli() -> int:
    """安装后的入口统一 UTF-8；main 保持可被 StringIO 捕获测试。"""
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    return main()


if __name__ == "__main__":
    raise SystemExit(cli())
