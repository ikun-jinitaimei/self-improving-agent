"""Python 执行命令的构造：本地演示与 Docker 执行采用同一工具协议。

Docker 路径只挂载本次 CSV 目录，禁止网络，限制内存/进程，根文件系统只读。
它减少了宿主机文件暴露，但仍依赖 Docker daemon 与镜像安全，不承诺绝对安全。
本地模式不是沙箱，只能在用户明确接受风险或运行受控离线演示时使用。
"""

import sys
import shutil
import subprocess
from pathlib import Path


DOCKER_IMAGE = "python:3.12-slim"


def check_docker() -> None:
    """在花费 API 额度前检查 CLI、daemon 和预装镜像；不隐式 pull。

    inspect 只是读取本机镜像。backend 真正挂载与执行还要单独验收，不能把
    这个检查或命令构造单元测试当作 Docker 集成测试已通过。
    """
    if shutil.which("docker") is None:
        raise RuntimeError("未找到 Docker CLI；请准备 Docker Engine / Desktop，或明确选择不安全的本地模式。")
    try:
        result = subprocess.run(["docker", "image", "inspect", DOCKER_IMAGE],
                                capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("无法访问 Docker daemon，请确认 Docker Engine 已启动。") from error
    if result.returncode:
        raise RuntimeError(f"Docker daemon 不可用或缺少镜像；先启动 Docker 并执行 docker pull {DOCKER_IMAGE}。")


def python_command(code: str, data_root: Path, backend: str, name: str) -> list[str]:
    """返回参数列表，不经过 shell；模型代码永远只是 -c 的一个参数。

    --mount 以只读方式提供 data，隐藏项目源码/答案/凭据。--pull=never 避免一次
    做题隐式下载镜像。Docker 命令只创建不启动，工具层在创建完成后才执行
    start，并在结束/超时后清理；避免 docker run 超时与异步创建的竞态。
    """
    if backend == "local":
        return [sys.executable, "-X", "utf8", "-c", code]
    if backend != "docker":
        raise ValueError("backend 必须是 local 或 docker")
    root = str(data_root.resolve())
    if "," in root:
        raise ValueError("Docker 数据目录不能包含逗号（--mount 的分隔符）")
    return [
        "docker", "create", "--pull=never", "--name", name,
        "--network=none", "--memory=256m", "--cpus=1", "--pids-limit=64",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--user=65534:65534", "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        "--mount", f"type=bind,source={root},target=/data,readonly",
        "--workdir=/data", DOCKER_IMAGE, "python", "-X", "utf8", "-c", code,
    ]
