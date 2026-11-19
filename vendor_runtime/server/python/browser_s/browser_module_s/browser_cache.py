"""本地浏览器缓存目录与认证代理准备工具。

Chrome 和 Ruyi 都运行在执行端，因此浏览器数据目录、配置文件和本地代理
转发也必须在执行端创建。上层业务只传浏览器名称、代理和运行窗口编号。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from ..browser_task_setting import ProxySetting
from .browser_error import BrowserError


_INVALID_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def browser_profile_directory(
    root: Path,
    *,
    browser_name: str,
    browser_id: str,
) -> Path:
    """按可读名称创建独立目录，同时用短编号避免同名运行互相覆盖。"""

    safe_name = safe_browser_name(browser_name)
    short_id = browser_id.rsplit("-", 1)[-1][:8]
    return root / f"{safe_name}_{short_id}"


def safe_browser_name(value: str) -> str:
    """把用户填写的浏览器名称转换成 Windows/Linux 都可用的目录名。"""

    name = _INVALID_FILE_NAME.sub("_", str(value).strip())
    name = name.rstrip(" ._")[:80] or "未命名浏览器"
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"{name}_浏览器"
    return name


def prepare_local_browser_proxy(
    proxy: ProxySetting | None,
    *,
    metadata: Mapping[str, Any] | None,
    provider: str,
    display_name: str,
) -> ProxySetting | None:
    """把带账号密码的远程代理转换成执行端本机无认证代理。

    本地 Chrome 和 Ruyi 统一调用本方法；厂商指纹浏览器仍使用各自 API。
    """

    if proxy is None:
        return None
    if not proxy.username and not proxy.password:
        return proxy
    if not proxy.username or not proxy.password:
        raise BrowserError(
            provider=provider,
            operation="prepare_proxy",
            code="proxy_credentials_incomplete",
            message=f"{display_name} 的代理账号或密码不完整",
            retryable=False,
        )

    from server.python.proxy_s import get_or_create_local_forward

    raw_thread_index = (metadata or {}).get("thread_index", 0)
    try:
        thread_index = max(0, int(raw_thread_index))
    except (TypeError, ValueError):
        thread_index = 0
    forwarded = get_or_create_local_forward(
        thread_index,
        proxy.host,
        proxy.port,
        proxy.username,
        proxy.password,
        upstream_type=proxy.protocol,
    )
    if forwarded is None:
        raise BrowserError(
            provider=provider,
            operation="prepare_proxy",
            code="local_proxy_forward_failed",
            message=f"{display_name} 无法启动本机代理转发，请检查代理设置",
            retryable=True,
        )
    return ProxySetting(
        protocol="http",
        host=forwarded.host,
        port=int(forwarded.port),
    )


__all__ = [
    "browser_profile_directory",
    "prepare_local_browser_proxy",
    "safe_browser_name",
]
