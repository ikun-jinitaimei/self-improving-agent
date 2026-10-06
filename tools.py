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
from pathlib import Path
from uuid import uuid4

from execution import python_command


# 以脚本位置确定数据目录，不依赖启动程序时终端所在的位置。
DATA_ROOT = Path(__file__).resolve().parent / "data"


def list_files(directory: str = ".", *, data_root: Path = DATA_ROOT) -> dict[str, str]:
    """执行已校验的列目录请求；路径范围属于本工具的执行边界。

    resolve 处理 .. 后再检查范围。iterdir 只列直接子项，排序使输出稳定。
    文件不存在、不是目录或没有权限会抛 OSError，由统一入口转换成反馈。
    """
    data_root = data_root.resolve()
    requested = (data_root / directory).resolve()
    if not requested.is_relative_to(data_root):
        return {"status": "execution_error",
                "observation": "ToolError: directory must stay inside data/"}
    entries = sorted(requested.iterdir(), key=lambda path: path.name.lower())
    lines = []
    for entry in entries:
        kind = "DIR" if entry.is_dir() else "FILE"
        lines.append(f"[{kind}] {entry.relative_to(data_root)}")
    return {"status": "success", "observation": "\n".join(lines) or "(empty directory)"}


def run_python(code: str, timeout_seconds: int = 10, *,
               data_root: Path = DATA_ROOT, backend: str = "local") -> dict[str, str]:
    """在所选后端执行已校验的代码，按真实退出码确定状态。

    参数只在 execute_tool 校验一次。这里专注执行：
    - 本地复用当前 Python；Docker 使用只读数据挂载与资源限制；
    - 两后端工作目录都是本次数据目录，代码使用相对文件名；
    - capture_output 捕获 stdout/stderr；UTF-8 避免中文解码错误；
    - check=False 让我们自己处理非零退出码；
    - timeout 限制 Python 执行，Docker 创建另有 10 秒限额，清理最多 5 秒；
      完整调用耗时仍写入轨迹，不能把准备/清理时间从延迟中删掉。

    本地子进程不继承 API 密钥，但 cwd 不禁止访问其他宿主文件，所以不是沙箱。
    Docker 的信任边界、预安装镜像与未验收项详见 execution.py 和 SECURITY.md。
    """
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG"}
    child_env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if backend == "docker":
        # 这些配置只供 Docker CLI 使用，不通过 -e 传给容器；容器中的 Python
        # 仍看不到 API 密钥。Windows Docker Desktop 需要用户目录定位 context。
        docker_keys = {"USERPROFILE", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                       "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
        child_env.update({k: v for k, v in os.environ.items() if k.upper() in docker_keys})
    # 容器名由程序生成，绝不使用模型参数。超时后只清理本次创建的容器，
    # 因为终止 docker CLI 本身并不保证终止后台容器。
    name = f"sia-{uuid4().hex}"
    command = python_command(code, data_root, backend, name)
    def invoke(arguments, timeout):
        # 复用同一环境/编码配置，参数列表不经过 shell；不在各阶段重复校验模型参数。
        return subprocess.run(arguments, cwd=data_root, env=child_env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)

    def remove_container():
        # --rm 不能解决 docker run 被杀时容器还在创建的竞态。这里显式验证
        # 本次名称的 rm 结果；清理失败直接抛出，不掩盖失败并继续执行下一题。
        try:
            removed = invoke(["docker", "rm", "-f", name], 5)
        except (subprocess.TimeoutExpired, OSError) as error:
            # 清理阶段超时/CLI 无法启动不能落入外层“Python 超时”反馈，
            # 否则 Agent 会在清理未确认时继续调用模型。仅捕获这两类环境错误。
            raise RuntimeError(f"Docker cleanup unconfirmed for owned container {name}; stop execution") from error
        if removed.returncode:
            raise RuntimeError(f"Docker cleanup failed for owned container {name}; stop execution")

    if backend == "local":
        completed = invoke(command, timeout_seconds)
    else:
        # create 只登记容器，不运行模型代码。只有收到成功返回后，才进入 start。
        # 这样 Python 的短超时不会杀掉仍在创建容器的 CLI。若创建本身超时，
        # 也尝试删除本次名称；无法确认清理时停止，绝不假装安全地自动重试。
        try:
            prepared = invoke(command, 10)
        except subprocess.TimeoutExpired:
            remove_container()
            raise
        if prepared.returncode:
            completed = prepared
        else:
            try:
                completed = invoke(["docker", "start", "--attach", name], timeout_seconds)
            finally:
                # 正常退出、非零退出、超时、内部异常均走清理。没有宽泛 except，
                # 清理错误和内部编程错误保留异常，不变成普通的模型观察。
                remove_container()
    parts = []
    if completed.stdout.strip():
        parts.append(f"STDOUT:\n{completed.stdout.strip()}")
    if completed.stderr.strip():
        parts.append(f"STDERR:\n{completed.stderr.strip()}")
    if completed.returncode != 0:
        parts.append(f"EXIT CODE: {completed.returncode}")
    # stderr 可能只是警告；输出文字不参与状态判断。
    observation = "\n".join(parts) or "(no output)"
    if len(observation) > 12000:
        observation = observation[:12000] + "\n[OUTPUT TRUNCATED]"
    return {
        "status": "success" if completed.returncode == 0 else "execution_error",
        # 限制进入模型上下文和日志的输出长度，不把它宣称为内存硬限制。
        "observation": observation,
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


def execute_tool(tool_name: str, arguments: str | dict, *,
                 data_root: Path = DATA_ROOT, backend: str = "local") -> dict[str, str]:
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
            return list_files(**arguments, data_root=data_root)
        return run_python(**arguments, data_root=data_root, backend=backend)
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
