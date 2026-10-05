"""模型服务配置与凭据边界：参考 Pi 的模型层/运行层分离，但不引入新框架。

公开配置只包含模型 ID、端点、请求参数和环境变量名称。密钥从环境或用户
明确保存的 private JSON 中读取；不写入 manifest、模型消息或公开配置。
目前只支持 OpenAI-compatible Chat Completions，不宣称原生支持所有供应商。
"""

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from openai import OpenAI


@dataclass(frozen=True)
class ModelSettings:
    """一个可序列化的非秘密模型配置；extra_body 用于供应商特定请求参数。

    frozen 禁止重新赋值配置字段（嵌套字典仍须由调用者保持只读）。兼容模型不应自动携带 DeepSeek
    专有参数，应在公开配置中明确设置 extra_body={}。
    """

    provider: str = "deepseek"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0
    max_tokens: int = 2048
    timeout_seconds: float = 60
    extra_body: dict = field(default_factory=lambda: {"thinking": {"type": "disabled"}})

    def public(self) -> dict:
        """只导出公开配置；API Key 不属于这个 dataclass。"""
        return asdict(self)


def load_settings(path: Path | None = None, *, model: str | None = None) -> ModelSettings:
    """只在配置文件输入边界校验，不默默忽略未知字段或包含密钥的配置。

    HTTPS 用于外部服务；HTTP 仅允许 localhost 用于本地开发，避免误把密钥
    发到明文远端。端点不能包含 URL 用户名、密码、查询参数或片段。
    """
    data = json.loads(path.read_text(encoding="utf-8")) if path else {}
    allowed = set(ModelSettings.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("模型配置必须是 object，且不能含 api_key 或未知字段")
    if model is not None:
        data["model"] = model
    settings = ModelSettings(**data)
    for key in ("provider", "model", "base_url", "key_env"):
        if not isinstance(getattr(settings, key), str) or not getattr(settings, key).strip():
            raise ValueError(f"{key} 必须是非空字符串")
    url = urlparse(settings.base_url)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if not url.hostname or (url.scheme != "https" and not (local and url.scheme == "http")):
        raise ValueError("远端服务必须使用 HTTPS；HTTP 仅允许本机端点")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("base_url 不能含凭据、查询参数或 fragment")
    if type(settings.max_tokens) is not int or settings.max_tokens < 1:
        raise ValueError("max_tokens 必须是正整数")
    if type(settings.timeout_seconds) not in (int, float) or not math.isfinite(settings.timeout_seconds) or settings.timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须为正数")
    if type(settings.temperature) not in (int, float) or not 0 <= settings.temperature <= 2:
        raise ValueError("temperature 必须在 0..2 范围内")
    if not isinstance(settings.extra_body, dict):
        raise ValueError("extra_body 必须是 object")
    # 防止凭据经 extra_body 混入日志；供应商附加参数不能覆盖已有请求协议。
    forbidden = {"api_key", "authorization", "messages", "tools", "model", "stream"}
    if set(settings.extra_body) & forbidden:
        raise ValueError("extra_body 不能含凭据或覆盖核心请求字段")
    return settings


def resolve_key(settings: ModelSettings, key_file: Path | None = None) -> str | None:
    """环境变量优先；只读取指定私有文件，不扫描磁盘或编辑器凭据存储。"""
    value = os.environ.get(settings.key_env)
    if value:
        return value
    if key_file is None or not key_file.exists():
        return None
    data = json.loads(key_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(not isinstance(v, str) for v in data.values()):
        raise ValueError("私有凭据文件必须是字符串键值映射")
    return data.get(settings.key_env) or None


def configured_client(settings: ModelSettings, key_file: Path | None = None) -> OpenAI:
    """只负责创建客户端，不请求模型；调用者用 with 关闭网络连接资源。"""
    key = resolve_key(settings, key_file)
    if not key:
        raise RuntimeError(f"没有找到 {settings.key_env}；请运行 configure 或在本地设置环境变量。")
    return OpenAI(api_key=key, base_url=settings.base_url,
                  timeout=settings.timeout_seconds, max_retries=0)


def save_key(settings: ModelSettings, key_file: Path, key: str) -> None:
    """保存到用户指定的私有文件，不修改公开配置或系统环境。

    文件名必须以 .private.json 结尾，与仓库忽略规则匹配。JSON 是明文，
    不是加密保险箱；POSIX 收紧权限，Windows 依赖所在目录的用户 ACL。
    此处不能使用日志 write_json：日志会脱敏，凭据则必须保留真实值。
    """
    if not key_file.name.endswith(".private.json") or not key.strip():
        raise ValueError("密钥不能为空，凭据文件名必须以 .private.json 结尾")
    data = json.loads(key_file.read_text(encoding="utf-8")) if key_file.exists() else {}
    if not isinstance(data, dict) or any(not isinstance(v, str) for v in data.values()):
        raise ValueError("原凭据文件必须是字符串键值映射")
    data[settings.key_env] = key.strip()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.touch(mode=0o600, exist_ok=True)
    if os.name != "nt":
        key_file.chmod(0o600)
    key_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def request_completion(client, messages: list[dict], tools: list[dict], settings: ModelSettings):
    """模型调用唯一入口；核心循环只提供消息与工具，不拼供应商特殊参数。"""
    options = {"model": settings.model, "messages": messages, "stream": False,
               "temperature": settings.temperature, "max_tokens": settings.max_tokens,
               "extra_body": settings.extra_body}
    if tools:
        options["tools"] = tools
    return client.chat.completions.create(**options)
