"""实验文件保存：提供独立的目录创建和 JSON 写入函数。

Agent 单题入口和 benchmark 入口都依赖本模块，本模块不导入它们。
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


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
