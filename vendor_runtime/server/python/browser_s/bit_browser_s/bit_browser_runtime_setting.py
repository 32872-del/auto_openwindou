"""BitBrowser 连接配置。"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .bit_browser_default import BIT_REQUEST_TIMEOUT, BIT_START_TIMEOUT


@dataclass(frozen=True, slots=True)
class BitBrowserRuntimeSetting:
    """BitBrowser 本机接口 配置。"""

    local_api_url: str
    request_timeout: float = BIT_REQUEST_TIMEOUT
    start_timeout: float = BIT_START_TIMEOUT

    def __post_init__(self) -> None:
        value = self.local_api_url.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("local_api_url 不是有效 HTTP URL")
        if min(self.request_timeout, self.start_timeout) <= 0:
            raise ValueError("BitBrowser timeout 必须大于 0")
        object.__setattr__(self, "local_api_url", value)


__all__ = ["BitBrowserRuntimeSetting"]
