"""Self-Improving Agent V0 的最小工具执行层。

这个文件负责把模型提出的“工具请求”变成真实操作。完整流程是：

    模型生成工具名称和参数
        -> agent.py 调用 execute_tool()
        -> tools.py 执行真实操作
        -> 返回文本 observation
        -> agent.py 把 observation 再发给模型

因此，本文件不是让模型思考的地方，而是 Agent 与真实环境之间的接口。
V0 暂时只提供两个工具：

1. list_files：查看 data/ 目录中的文件；
2. run_python：在 data/ 目录中运行 Python 数据分析代码。

所有工具统一返回字符串。成功时返回执行结果，失败时返回以 ``ToolError:``
开头的错误信息（指参数、启动、超时等错误）；代码执行失败则返回 STDERR
和 EXIT CODE。错误也会成为 observation，使模型有机会理解并修正行动。
"""

import os
import subprocess
import sys
from pathlib import Path


# __file__ 是当前 tools.py 文件的路径。
# resolve() 得到规范的绝对路径，parent 再取得该文件所在的项目根目录。
# 因此，无论从哪个工作目录启动程序，工具都能稳定找到 data/。
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_ROOT = PROJECT_ROOT / "data"

# DATA_ROOT 是工具允许模型直接查看和分析的数据区域。
# tasks/tasks.json 中含有隐藏标准答案，因此 list_files 必须阻止模型通过
# "../tasks" 等路径离开 data/，否则评估结果就不可信了。


def list_files(directory: str = ".") -> str:
    """列出 data/ 内指定目录的直接子项。

    参数：
        directory：相对于 data/ 的目录；默认 ``"."`` 表示 data/ 本身。

    返回：
        适合直接作为 observation 发给模型的文本，例如：
        ``[FILE] sales.csv``。

        如果路径非法、不存在或不是目录，则返回 ``ToolError: ...``。
        这里不直接抛出异常，是因为 Agent 需要读取错误原因，才可能在下一步
        改正自己生成的路径。
    """
    # pathlib 允许用 / 拼接路径：DATA_ROOT / "raw" 就是 data/raw/。
    # resolve() 会处理路径中的 "." 和 ".."，得到最终绝对路径。
    requested_directory = (DATA_ROOT / directory).resolve()

    # 检查 resolve() 后的最终路径是否仍位于 DATA_ROOT 内。
    #
    # 如果路径在 data/ 内，relative_to(DATA_ROOT) 可以正常计算相对路径；
    # 如果路径已经逃到 data/ 外，它就会抛出 ValueError。
    # 这里不需要保存计算结果，只利用它完成合法性检查。
    #
    # directory="raw"      -> 允许，最终是 data/raw/
    # directory="../tasks" -> 拒绝，最终路径已经离开 data/
    try:
        requested_directory.relative_to(DATA_ROOT)
    except ValueError:
        return "ToolError: directory must stay inside data/"

    # exists() 判断路径是否存在。把常见问题转换成清晰的工具反馈，
    # 比让底层系统异常直接中断 Agent 循环更容易理解和恢复。
    if not requested_directory.exists():
        return f"ToolError: directory does not exist: {directory}"

    # 路径存在不代表它一定是目录。例如传入 sales.csv 时，exists() 为 True，
    # 但文件不能使用 iterdir()，所以还要用 is_dir() 单独检查。
    if not requested_directory.is_dir():
        return f"ToolError: path is not a directory: {directory}"

    # iterdir() 只返回该目录的直接子项，不会递归遍历更深层目录。
    # sorted() 让输出顺序保持稳定，有利于实验复现；lower() 减少英文大小写
    # 对排序的影响。lambda 接收一个 path，并返回用于排序的文件名。
    entries = sorted(requested_directory.iterdir(), key=lambda path: path.name.lower())

    # 空目录也返回明确文本，避免模型误以为工具根本没有运行。
    if not entries:
        return "(empty directory)"

    # 将每个项目整理为模型容易读取的格式。这里只暴露相对于 data/ 的路径，
    # 不把用户计算机上的完整绝对路径发给模型。
    lines = []
    for entry in entries:
        entry_type = "DIR" if entry.is_dir() else "FILE"
        relative_path = entry.relative_to(DATA_ROOT)
        lines.append(f"[{entry_type}] {relative_path}")

    # join() 用换行符连接所有项目，最终仍然只返回一个字符串 observation。
    return "\n".join(lines)


def run_python(code: str, timeout_seconds: int = 10) -> str:
    """在一个新的 Python 子进程中执行模型提供的代码。

    参数：
        code：需要运行的 Python 源代码字符串。
        timeout_seconds：最长运行时间，默认 10 秒。

    返回：
        汇总后的文本 observation，可能包含：

        - STDOUT：print() 等方式产生的正常输出；
        - STDERR：Python traceback、错误或警告；
        - EXIT CODE：非零退出状态，表示执行失败；
        - ToolError：参数错误或执行超时。

    为什么使用子进程：
        如果在 Agent 主进程中直接 exec(code)，模型代码的异常、死循环或全局
        变量修改可能破坏 Agent 本身。单独的子进程可以隔开一次代码执行，并
        允许设置超时。不过，这种隔离仍然不等于操作系统级安全沙箱。
    """
    # 同时拒绝非字符串、空字符串和只包含空白字符的代码。
    # strip() 删除首尾空白；删除后为空，说明没有真正的可执行内容。
    if not isinstance(code, str) or not code.strip():
        return "ToolError: code must be a non-empty string"

    # Schema 不能替代本地校验；bool 是 int 子类，必须显式排除。
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30:
        return "ToolError: timeout_seconds must be an integer from 1 to 30"
    # 只传基础环境变量，防止模型生成的子进程继承 API 密钥。
    # 这不限制文件访问，仍不是操作系统沙箱。
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG"}
    child_env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    try:
        # subprocess.run() 启动新的进程，并等待它结束。
        #
        # [sys.executable, "-X", "utf8", "-c", code] 的含义：
        #   sys.executable：使用运行 Agent 的同一个 Python 解释器，例如
        #                   agent_env/Scripts/python.exe；
        #   -X utf8：       开启 UTF-8 模式，减少中文乱码；
        #   -c code：       执行字符串中的 Python 代码。
        #
        # 其他参数：
        #   cwd：            将子进程工作目录设为 data/，因此代码中应使用
        #                   open("sales.csv")，而不是 open("data/sales.csv")；
        #   capture_output：捕获 stdout 和 stderr，不让它们直接散落到终端；
        #   text/encoding： 将捕获的字节按 UTF-8 解码为字符串；
        #   errors=replace：无法解码的字符用替代符表示，避免工具崩溃；
        #   timeout：       超过指定秒数后抛出 TimeoutExpired；
        #   check=False：   退出码非零时不自动抛异常，由下面的代码统一整理
        #                  stderr 和退出码并返回给模型。
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
    except subprocess.TimeoutExpired:
        # 超时是可预期的执行失败，把它转换成统一的工具错误文本。
        return f"ToolError: Python execution exceeded {timeout_seconds} seconds"

    except OSError as error:
        return f"ToolError: cannot start Python ({type(error).__name__})"

    # completed 是 CompletedProcess 对象，主要包含 stdout、stderr、returncode。
    # output_parts 只收集实际出现的输出部分。
    output_parts = []

    # stdout 是正常输出，通常来自模型生成代码中的 print()。
    if completed.stdout.strip():
        output_parts.append(f"STDOUT:\n{completed.stdout.strip()}")

    # stderr 通常包含 traceback、运行错误或部分警告。
    if completed.stderr.strip():
        output_parts.append(f"STDERR:\n{completed.stderr.strip()}")

    # 按照操作系统惯例，returncode=0 表示成功，非零表示失败。
    # 保留退出码有利于以后统计 Agent 的代码执行失败次数。
    if completed.returncode != 0:
        output_parts.append(f"EXIT CODE: {completed.returncode}")

    # 如果代码成功执行但没有打印内容，明确返回 "(no output)"。
    # Agent 因而能区分“工具没有运行”和“运行了但没有可观察输出”。
    return "\n".join(output_parts) if output_parts else "(no output)"


def validate_tool_arguments(tool_name: str, arguments: dict) -> str | None:
    """本地校验工具名、参数和范围；合法返回 None，非法返回错误说明。

    Agent 用它统计非法调用，执行入口也使用它，避免只依靠提示词约束模型。
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


def execute_tool(tool_name: str, arguments: dict) -> str:
    """根据工具名称，把调用分发给对应的真实 Python 函数。

    agent.py 从模型回复中取得的内容类似：

        tool_name = "run_python"
        arguments = {"code": "print(1 + 1)"}

    execute_tool() 是工具层的统一入口。Agent 只需提供工具名和参数，不必知道
    每个函数具体怎样调用。所有分支最终都返回字符串 observation。
    """
    # arguments.get("directory", ".") 的意思是：模型提供了 directory 就使用
    # 它；没有提供时使用默认值 "."，也就是列出 data/ 根目录。
    error = validate_tool_arguments(tool_name, arguments)
    if error:
        return f"ToolError: {error}"
    if tool_name == "list_files":
        return list_files(directory=arguments.get("directory", "."))

    # code 是必需参数；如果模型遗漏它，就传入空字符串，让 run_python()
    # 返回容易理解的 ToolError。未提供 timeout_seconds 时默认使用 10 秒。
    if tool_name == "run_python":
        return run_python(
            code=arguments.get("code", ""),
            timeout_seconds=arguments.get("timeout_seconds", 10),
        )

    # 模型也可能生成不存在的工具名。将其变成 observation 后，模型下一轮
    # 可以看到错误并改用合法工具，而不是让整个 Agent 程序崩溃。
    return f"ToolError: unknown tool: {tool_name}"


if __name__ == "__main__":
    # 导入本文件时，__name__ 等于 "tools"，下面的演示不会执行。
    # 只有直接运行 `python tools.py` 时，__name__ 才等于 "__main__"。
    # 所以 agent.py 可以安静地导入 execute_tool，不会意外打印演示内容。
    print("list_files observation:")
    print(execute_tool("list_files", {"directory": "."}))

    # demo_code 是一个多行 Python 字符串。相邻字符串会自动拼接，\n 表示换行。
    # 示例代码读取 sales.csv，并通过 print() 输出 CSV 数据行数。
    demo_code = (
        "import csv\n"
        "with open('sales.csv', encoding='utf-8') as file:\n"
        "    rows = list(csv.DictReader(file))\n"
        "print(f'row_count={len(rows)}')"
    )
    print("\nrun_python observation:")
    print(execute_tool("run_python", {"code": demo_code}))


# 重要安全边界：
# cwd=DATA_ROOT 只设置子进程的“默认工作目录”，并不能禁止 Python 代码使用
# 绝对路径或 ".." 访问其他位置；timeout 也只限制运行时间。因此 run_python
# 不是操作系统级沙箱。目前它只适用于本项目的受控实验，不能执行来源不可信的
# 任意代码。如果以后允许外部用户提交任务，就需要真正的容器或系统沙箱。
