"""Hubstudio 浏览器连接配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .hub_browser_default import (
    HUB_REQUEST_TIMEOUT,
    HUB_START_TIMEOUT,
    HUB_STOP_TIMEOUT,
)


@dataclass(frozen=True, slots=True)
class HubBrowserRuntimeSetting:
    """Hubstudio 本机接口 配置。

    登录凭据不参与对象输出；未配置登录三件套时，直接使用客户端中现有登录态。
    """

    local_api_url: str
    authorization: str = field(default="NULL", repr=False)
    app_id: str = field(default="", repr=False)
    app_secret: str = field(default="", repr=False)
    group_code: str = field(default="", repr=False)
    accept_language: str = "zh-CN"
    request_timeout: float = HUB_REQUEST_TIMEOUT
    start_timeout: float = HUB_START_TIMEOUT
    stop_timeout: float = HUB_STOP_TIMEOUT
    core_directory: str = ""

    def __post_init__(self) -> None:
        value = self.local_api_url.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("local_api_url 不是有效 HTTP URL")
        if min(
            self.request_timeout,
            self.start_timeout,
            self.stop_timeout,
        ) <= 0:
            raise ValueError("Hubstudio timeout 必须大于 0")
        object.__setattr__(self, "local_api_url", value)

        core_directory = self.core_directory.strip()
        if not core_directory:
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            if local_app_data:
                core_directory = str(Path(local_app_data) / "env-kit" / "Core")
        object.__setattr__(self, "core_directory", core_directory)


__all__ = ["HubBrowserRuntimeSetting"]
