"""工具执行层：在唯一入口校验参数，然后执行并返回结构化结果。

execute_tool 接受模型的 JSON 参数文本，也接受本地调用传入的字典。
返回 {"status": ..., "observation": ...}：
- success：执行成功，输出中出现错误字样也不改变状态；
- invalid_arguments：工具名或参数不合法；
- execution_error：合法调用遇到文件错误、超时或非零退出码。
Agent 用 status 统计，用 observation 向模型反馈，不再从文字猜测是否失败。
"""

import json
import os
import subprocess
import sys
from pathlib import Path


# 以脚本位置确定数据目录，不依赖启动程序时终端所在的位置。
DATA_ROOT = Path(__file__).resolve().parent / "data"


def list_files(directory: str = ".") -> dict[str, str]:
    """执行已校验的列目录请求；路径范围属于本工具的执行边界。

    resolve 处理 .. 后再检查范围。iterdir 只列直接子项，排序使输出稳定。
    文件不存在、不是目录或没有权限会抛 OSError，由统一入口转换成反馈。
    """
    requested = (DATA_ROOT / directory).resolve()
    if not requested.is_relative_to(DATA_ROOT):
        return {"status": "execution_error",
                "observation": "ToolError: directory must stay inside data/"}
    entries = sorted(requested.iterdir(), key=lambda path: path.name.lower())
    lines = []
    for entry in entries:
        kind = "DIR" if entry.is_dir() else "FILE"
        lines.append(f"[{kind}] {entry.relative_to(DATA_ROOT)}")
    return {"status": "success", "observation": "\n".join(lines) or "(empty directory)"}


def run_python(code: str, timeout_seconds: int = 10) -> dict[str, str]:
    """用当前 Python 解释器执行已校验的代码，按真实退出码确定状态。

    参数只在 execute_tool 校验一次。这里专注执行：
    - sys.executable 复用当前虚拟环境；-c 执行代码字符串；
    - cwd 设为 data，因此代码中使用 open("sales.csv")；
    - capture_output 捕获 stdout/stderr；UTF-8 避免中文解码错误；
    - check=False 让我们自己处理非零退出码；
    - timeout 防止一次调用无限等待，超时由统一入口处理。

    子进程只继承基本环境变量，不继承 API 密钥。这不是操作系统沙箱：
    cwd 无法禁止代码通过绝对路径或 .. 访问其他文件。
    """
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG"}
    child_env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=DATA_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    parts = []
    if completed.stdout.strip():
        parts.append(f"STDOUT:\n{completed.stdout.strip()}")
    if completed.stderr.strip():
        parts.append(f"STDERR:\n{completed.stderr.strip()}")
    if completed.returncode != 0:
        parts.append(f"EXIT CODE: {completed.returncode}")
    # stderr 可能只是警告；输出文字不参与状态判断。
    return {
        "status": "success" if completed.returncode == 0 else "execution_error",
        "observation": "\n".join(parts) or "(no output)",
    }


def validate_tool_arguments(tool_name: str, arguments: dict) -> str | None:
    """仅供执行入口使用：检查模型输入，合法返回 None，否则返回错误说明。

    JSON Schema 给模型描述参数，本地仍需校验真实输入。
    使用 type(timeout) is int 排除 bool，因为 Python 中 bool 是 int 的子类。
    """
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    if tool_name == "list_files":
        if set(arguments) - {"directory"}:
            return "unexpected list_files arguments"
        if not isinstance(arguments.get("directory", "."), str):
            return "directory must be a string"
    elif tool_name == "run_python":
        if set(arguments) - {"code", "timeout_seconds"}:
            return "unexpected run_python arguments"
        if not isinstance(arguments.get("code"), str) or not arguments["code"].strip():
            return "code must be a non-empty string"
        timeout = arguments.get("timeout_seconds", 10)
        if type(timeout) is not int or not 1 <= timeout <= 30:
            return "timeout_seconds must be an integer from 1 to 30"
    else:
        return f"unknown tool: {tool_name}"
    return None


def execute_tool(tool_name: str, arguments: str | dict) -> dict[str, str]:
    """统一边界：解析参数 → 校验一次 → 分发执行 → 返回状态及反馈。

    JSON 格式错误是模型输入错误；文件操作失败与超时是环境执行错误。
    不捕获 Exception，内部代码的 TypeError 等应保留 traceback，便于修复。
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as error:
            return {"status": "invalid_arguments",
                    "observation": f"ToolError: invalid JSON arguments: {error.msg}"}
    error = validate_tool_arguments(tool_name, arguments)
    if error:
        return {"status": "invalid_arguments", "observation": f"ToolError: {error}"}

    try:
        if tool_name == "list_files":
            return list_files(**arguments)
        return run_python(**arguments)
    except subprocess.TimeoutExpired as error:
        return {"status": "execution_error",
                "observation": f"ToolError: Python execution exceeded {error.timeout} seconds"}
    except OSError as error:
        return {"status": "execution_error",
                "observation": f"ToolError: {type(error).__name__}: {error.strerror}"}


if __name__ == "__main__":
    # 导入模块时不执行演示；直接运行时人工检查两个工具的状态和输出。
    print(execute_tool("list_files", {"directory": "."}))
    demo_code = (
        "import csv\n"
        "with open('sales.csv', encoding='utf-8') as file:\n"
        "    rows = list(csv.DictReader(file))\n"
        "print(f'row_count={len(rows)}')"
    )
    print(execute_tool("run_python", {"code": demo_code}))
