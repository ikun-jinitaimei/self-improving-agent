"""实验文件保存：提供独立的目录创建和 JSON 写入函数。

Agent 单题入口和 benchmark 入口都依赖本模块，本模块不导入它们。
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def append_event(path: Path, event: dict, *, secrets: tuple[str, ...] = ()) -> None:
    """追加一条 JSONL 事件并关闭文件；事件不是模型消息，也不执行代码。

    JSON 快照仍是恢复/分析的主记录；强制关机可能留下最后一条不完整 JSONL，
    所以不能把 append 宣称为数据库事务。私有文件 Key 通过显式参数脱敏。
    """
    text = json.dumps(event, ensure_ascii=False, allow_nan=False)
    for value in (*secrets, os.environ.get("DEEPSEEK_API_KEY", "")):
        if value:
            text = text.replace(value, "[REDACTED]")
    with path.open("a", encoding="utf-8") as file:
        file.write(text + "\n")


def create_run_directory(root: Path) -> Path:
    """时间方便人阅读，随机后缀避免同一秒运行两次时覆盖旧结果。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / f"{stamp}_{uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def write_json(path: Path, value: object, *, secrets: tuple[str, ...] = ()) -> None:
    """先完整写临时文件，再替换目标，降低中途退出留下半个 JSON 的可能。

    ensure_ascii=False 保留可读中文；allow_nan=False 拒绝不规范的数字。
    回调反复保存的是同一题的最新状态，不会新建大量零碎文件。
    """
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    # 防止当前密钥被意外嵌入错误信息。不要依靠它替代执行环境隔离。
    secret = os.environ.get("DEEPSEEK_API_KEY")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    for item in secrets:
        if item:
            text = text.replace(item, "[REDACTED]")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(path)
