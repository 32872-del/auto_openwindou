"""VMLogin 连接配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .vmlogin_browser_default import (
    VMLOGIN_REMOTE_API_URL,
    VMLOGIN_REQUEST_TIMEOUT,
    VMLOGIN_START_TIMEOUT,
)


@dataclass(frozen=True, slots=True)
class VMLoginBrowserRuntimeSetting:
    """VMLogin Local REST API 和远程 Profile API 配置。"""

    local_api_url: str
    remote_api_url: str = VMLOGIN_REMOTE_API_URL
    api_token: str = field(default="", repr=False)
    request_timeout: float = VMLOGIN_REQUEST_TIMEOUT
    start_timeout: float = VMLOGIN_START_TIMEOUT

    def __post_init__(self) -> None:
        for name in ("local_api_url", "remote_api_url"):
            value = getattr(self, name).strip().rstrip("/")
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{name} 不是有效 HTTP URL")
            object.__setattr__(self, name, value)
        if min(self.request_timeout, self.start_timeout) <= 0:
            raise ValueError("VMLogin timeout 必须大于 0")


__all__ = ["VMLoginBrowserRuntimeSetting"]
