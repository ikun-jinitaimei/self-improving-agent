"""自由问题会话：真实用户问题与 benchmark 评分明确分离。

参考 Pi 的会话/事件分层，保留 JSON 快照和 JSONL 生命周期事件。会话不包含
标准答案，也不会产生伪造的成功率；completed 仅说明格式合法地提交了答案。
"""

import contextlib
import hashlib
import io
import json
import shutil
import sys
from functools import partial
from pathlib import Path

from agent import run_agent
from providers import ModelSettings
from run_io import append_event, create_run_directory, write_json
from tools import execute_tool


def run_session(client, question: str, data_file: Path, answer_format: dict,
                output_root: Path, settings: ModelSettings, *, backend: str = "docker",
                policy: str = "baseline", max_steps: int = 6, json_events: bool = False,
                secrets: tuple[str, ...] = ()) -> Path:
    """只复制用户指定的 CSV 到工具可见目录，不挂载整份用户项目。

    输入格式在入口校验一次。API Key 文件、源文件和轨迹均在数据目录外。
    on_event 只订阅运行信息，不改变 messages；事件可供其他 CLI/程序集成。
    """
    source = data_file.resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".csv":
        raise ValueError("自由问题入口目前只接受单个 CSV 文件")
    if not question.strip() or not isinstance(answer_format, dict) or not answer_format:
        raise ValueError("问题和答案格式不能为空")
    if any(not isinstance(kind, str) or kind not in {"string", "number", "integer"} for kind in answer_format.values()):
        raise ValueError("答案格式只支持 string、number、integer")
    directory = create_run_directory(output_root)
    data_root = directory / "data"
    data_root.mkdir()
    copied = data_root / "input.csv"
    shutil.copyfile(source, copied)
    task_input = {"question": question, "data_file": "input.csv", "answer_format": answer_format}
    manifest = {"schema_version": 1, "kind": "user-session", "input": task_input,
                "provider_settings": settings.public(), "backend": backend, "policy": policy,
                "max_steps": max_steps, "data_sha256": hashlib.sha256(copied.read_bytes()).hexdigest(),
                "evaluation": "none; user task has no hidden reference answer"}
    write_json(directory / "manifest.json", manifest, secrets=secrets)
    stream = sys.stdout

    def save(record):
        write_json(directory / "session.json", {"kind": "user-session", **record}, secrets=secrets)

    def event_sink(event):
        append_event(directory / "events.jsonl", event, secrets=secrets)
        if json_events:
            text = json.dumps(event, ensure_ascii=False)
        else:
            text = f"[step {event['step']}] {event['type']}"
            if "name" in event:
                text += f" {event['name']}"
        # 不用 print，因为模型循环的旧调试输出被重定向；事件写入原标准流。
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]") if secret else text
        stream.write(text + "\n")
        stream.flush()

    with contextlib.redirect_stdout(io.StringIO()):
        result = run_agent(client, task_input, max_steps, policy=policy, settings=settings,
            tool_executor=partial(execute_tool, data_root=data_root, backend=backend),
            checkpoint=save, on_event=event_sink)
    if json_events:
        stream.write(json.dumps({"type": "session_saved", "directory": str(directory)}, ensure_ascii=False) + "\n")
    else:
        answer = json.dumps(result["answer"], ensure_ascii=False, indent=2)
        for secret in secrets:
            answer = answer.replace(secret, "[REDACTED]") if secret else answer
        stream.write(answer + "\n")
        stream.write(f"Status: {result['status']} (not graded)\nSession: {directory}\n")
    return directory


def replay_session(directory: Path) -> str:
    """查看保存的事件，不重复执行工具、不请求模型，也不是断点续跑。"""
    record = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    if record.get("kind") != "user-session":
        raise ValueError("replay 仅用于自由问题会话")
    lines = [f"Session: {record['status']} (not graded)"]
    lines += [f"{event['sequence']:03d} step={event['step']} {event['type']}"
              for event in record["events"]]
    lines += ["Answer:", json.dumps(record["answer"], ensure_ascii=False, indent=2)]
    return "\n".join(lines)
