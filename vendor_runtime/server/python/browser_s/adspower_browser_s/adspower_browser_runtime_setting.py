"""AdsPower 连接配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .adspower_browser_default import (
    ADSPOWER_REQUEST_TIMEOUT,
    ADSPOWER_START_TIMEOUT,
)


@dataclass(frozen=True, slots=True)
class AdsPowerBrowserRuntimeSetting:
    """AdsPower 本机接口 v2 配置。"""

    local_api_url: str
    api_key: str = field(default="", repr=False)
    request_timeout: float = ADSPOWER_REQUEST_TIMEOUT
    start_timeout: float = ADSPOWER_START_TIMEOUT

    def __post_init__(self) -> None:
        value = self.local_api_url.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("local_api_url 不是有效 HTTP URL")
        if min(self.request_timeout, self.start_timeout) <= 0:
            raise ValueError("AdsPower timeout 必须大于 0")
        object.__setattr__(self, "local_api_url", value)


__all__ = ["AdsPowerBrowserRuntimeSetting"]
