"""ruyi 指纹参数库和浏览器环境指纹配置。"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from server.python.utils_s.path_utils import get_server_paths

from ..browser_task_setting import ProxySetting
from ..browser_module_s.browser_error import BrowserError
from .ruyi_browser_fingerprint_default import DEFAULT_RUYI_FINGERPRINTS
from .ruyi_browser_default import RUYI_FINGERPRINT_ROOT


RUYI_FINGERPRINT_DIRECTORY = RUYI_FINGERPRINT_ROOT
RUYI_PROFILE_FINGERPRINT = "Fingerprint.txt"
RUYI_PROFILE_FINGERPRINT_INFO = "fingerprint_source.json"

_REQUIRED_KEYS = {
    "webdriver",
    "local_webrtc",
    "public_webrtc",
    "timezone",
    "font_system",
    "useragent",
    "hardwareConcurrency",
    "webglrenderer",
    "webglvendor",
    "width",
    "height",
    "canvas",
}
_LOCAL_IP_PREFIXES = (
    "192.168.1.",
    "192.168.2.",
    "192.168.4.",
    "192.168.6.",
    "192.168.7.",
    "192.168.8.",
    "192.168.9.",
    "10.0.0.",
    "10.0.1.",
    "10.0.3.",
    "10.0.4.",
    "10.0.5.",
    "10.0.6.",
    "172.16.1.",
    "172.16.2.",
    "172.16.3.",
    "172.16.4.",
    "172.16.5.",
    "172.20.1.",
)


def ensure_ruyi_fingerprint_library(
    directory: Path | None = None,
) -> Path:
    """保证执行端程序旁存在指纹参数目录，并首次释放 20 套默认参数。"""

    root = directory or get_server_paths().resolve_resource(
        RUYI_FINGERPRINT_DIRECTORY
    )
    root.mkdir(parents=True, exist_ok=True)
    if any(root.glob("*.txt")):
        return root

    for name, content in DEFAULT_RUYI_FINGERPRINTS.items():
        _atomic_write_text(root / name, content)
    return root


def list_ruyi_fingerprint_templates(
    directory: Path | None = None,
) -> tuple[Path, ...]:
    """列出当前可用的指纹参数；用户后来放入的文件也会自动参与随机。"""

    root = ensure_ruyi_fingerprint_library(directory)
    valid: list[Path] = []
    for path in sorted(root.glob("*.txt"), key=_natural_name):
        try:
            _read_fingerprint(path)
        except ValueError:
            continue
        valid.append(path)
    if valid:
        return tuple(valid)
    raise BrowserError(
        provider="ruyi",
        operation="fingerprint",
        code="fingerprint_library_empty",
        message="ruyi 指纹参数目录没有可用的配置文件",
        retryable=False,
    )


def assign_ruyi_fingerprint(
    profile_dir: Path,
    *,
    library_directory: Path | None = None,
    template_name: str = "",
) -> str:
    """首次创建环境时选择一套指纹，之后固定复用该环境的文件。"""

    profile_dir.mkdir(parents=True, exist_ok=True)
    target = profile_dir / RUYI_PROFILE_FINGERPRINT
    info_path = profile_dir / RUYI_PROFILE_FINGERPRINT_INFO
    if target.is_file():
        return _read_template_name(info_path)

    templates = list_ruyi_fingerprint_templates(library_directory)
    source = (
        _select_ruyi_fingerprint_template(templates, template_name)
        if template_name.strip()
        else secrets.choice(templates)
    )
    _atomic_write_text(target, source.read_text(encoding="utf-8"))
    _atomic_write_text(
        info_path,
        json.dumps(
            {"template": source.name},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return source.name


def _select_ruyi_fingerprint_template(
    templates: tuple[Path, ...],
    template_name: str,
) -> Path:
    """按安全文件名选择模板，禁止客户端传入任意执行端路径。"""

    requested = Path(template_name.strip()).name
    if requested != template_name.strip() or requested in {"", ".", ".."}:
        raise BrowserError(
            provider="ruyi",
            operation="fingerprint",
            code="fingerprint_template_invalid",
            message="ruyi 固定指纹文件名不正确",
            retryable=False,
        )
    requested_name = requested if requested.lower().endswith(".txt") else f"{requested}.txt"
    for template in templates:
        if template.name.casefold() == requested_name.casefold():
            return template
    raise BrowserError(
        provider="ruyi",
        operation="fingerprint",
        code="fingerprint_template_not_found",
        message="执行端没有找到指定的 ruyi 指纹文件，请让维护人员检查参数库",
        retryable=False,
    )


def prepare_ruyi_fingerprint(
    options: Any,
    *,
    profile_dir: Path,
    proxy: ProxySetting | None,
    geo_timeout: float,
    geo_retries: int,
    library_directory: Path | None = None,
) -> dict[str, Any]:
    """更新当前出口网络信息，并把固定指纹文件交给 ruyi 内核。"""

    fingerprint_path = profile_dir / RUYI_PROFILE_FINGERPRINT
    assign_ruyi_fingerprint(
        profile_dir,
        library_directory=library_directory,
    )
    try:
        geo = _fetch_ruyi_geo(
            proxy,
            timeout=geo_timeout,
            attempts=max(1, geo_retries),
        )
        config = _read_fingerprint(fingerprint_path)
        config.update(
            {
                "local_webrtc": (
                    secrets.choice(_LOCAL_IP_PREFIXES)
                    + str(secrets.randbelow(253) + 2)
                ),
                "public_webrtc": geo["ip"],
                "timezone": geo["timezone"],
            }
        )
        _write_fingerprint(fingerprint_path, config)
        if proxy is not None:
            options.set_proxy(
                f"{proxy.protocol}://{proxy.host}:{proxy.port}"
            )
        options.set_fpfile(str(fingerprint_path))
        width = int(config.get("width") or 0)
        height = int(config.get("height") or 0)
        if width > 0 and height > 0:
            options.set_window_size(width, height)
        return config
    except BrowserError:
        raise
    except Exception as exc:
        raise BrowserError(
            provider="ruyi",
            operation="fingerprint",
            code="proxy_or_network_failed",
            message="ruyi 无法读取当前网络信息，请检查代理、网络和账号密码",
            retryable=True,
        ) from exc


def _fetch_ruyi_geo(
    proxy: ProxySetting | None,
    *,
    timeout: float,
    attempts: int,
) -> dict[str, str]:
    """通过当前执行端代理读取出口 IP 和时区，不依赖 ruyipage 私有函数。"""

    import requests

    proxy_url = _ruyi_proxy_url(proxy)
    proxies = (
        {"http": proxy_url, "https": proxy_url}
        if proxy_url
        else None
    )
    last_error: Exception | None = None
    for _ in range(max(1, int(attempts))):
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.get(
                (
                    "http://ip-api.com/json/"
                    "?fields=status,message,query,timezone"
                ),
                proxies=proxies,
                timeout=max(0.5, float(timeout)),
                headers={
                    "User-Agent": "AutoDrissionPageAI-Ruyi/1.0",
                },
            )
            if response.status_code == 407:
                raise RuntimeError("代理账号或密码验证失败")
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                raise RuntimeError(
                    str(payload.get("message") or "网络信息查询失败")
                )
            ip_value = str(payload.get("query") or "").strip()
            timezone_value = str(payload.get("timezone") or "").strip()
            if not ip_value or not timezone_value:
                raise RuntimeError("网络信息没有返回出口 IP 或时区")
            return {
                "ip": ip_value,
                "timezone": timezone_value,
            }
        except Exception as exc:
            last_error = exc
    raise RuntimeError("ruyi 网络信息查询达到有限重试次数") from last_error


def _ruyi_proxy_url(proxy: ProxySetting | None) -> str:
    """把统一代理参数转换为 requests 使用的安全代理地址。"""

    if proxy is None:
        return ""
    protocol = str(proxy.protocol or "http").strip().lower()
    if protocol.startswith("socks5"):
        protocol = "socks5h"
    auth = ""
    if proxy.username:
        auth = quote(proxy.username, safe="")
        if proxy.password:
            auth += f":{quote(proxy.password, safe='')}"
        auth += "@"
    return f"{protocol}://{auth}{proxy.host}:{proxy.port}"


def _read_fingerprint(path: Path) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for source_line in path.read_text(encoding="utf-8").splitlines():
        line = source_line.strip()
        if not line or ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip()
        value: Any = raw_value.strip().strip("'").strip('"')
        if not key:
            continue
        if value == "None":
            value = None
        elif str(value).isdigit():
            value = int(value)
        config[key] = value

    missing = sorted(_REQUIRED_KEYS.difference(config))
    if missing:
        raise ValueError(
            f"{path.name} 缺少字段: {', '.join(missing)}"
        )
    return config


def _write_fingerprint(path: Path, config: dict[str, Any]) -> None:
    lines = [f"{key}:{value}" for key, value in config.items()]
    _atomic_write_text(path, "\n".join(lines) + "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_template_name(info_path: Path) -> str:
    if not info_path.is_file():
        return ""
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    return str(data.get("template") or "")


def _natural_name(path: Path) -> tuple[str, int, str]:
    match = re.search(r"(\d+)$", path.stem)
    number = int(match.group(1)) if match else 0
    prefix = path.stem[: match.start()] if match else path.stem
    return prefix.lower(), number, path.name.lower()


__all__ = [
    "RUYI_FINGERPRINT_DIRECTORY",
    "RUYI_PROFILE_FINGERPRINT",
    "assign_ruyi_fingerprint",
    "ensure_ruyi_fingerprint_library",
    "list_ruyi_fingerprint_templates",
    "prepare_ruyi_fingerprint",
]
