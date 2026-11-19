"""Helix 服务与内核配置。"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .helix_browser_default import (
    HELIX_CORE_FAMILY,
    HELIX_CORE_VERSION,
    HELIX_LOCAL_API_URL,
)


@dataclass(frozen=True, slots=True)
class HelixBrowserRuntimeSetting:
    """启动 Helix 所需的少量固定参数。"""

    local_api_url: str = HELIX_LOCAL_API_URL
    core_family: str = HELIX_CORE_FAMILY
    core_version: str = HELIX_CORE_VERSION
    auto_download_core: bool = False
    humanize: bool = True
    request_timeout: float = 120.0

    def __post_init__(self) -> None:
        url = self.local_api_url.strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Helix 服务地址格式不正确")
        if self.request_timeout <= 0:
            raise ValueError("Helix 最多等待时间必须大于 0")
        if not self.core_family.strip() or not self.core_version.strip():
            raise ValueError("Helix 内核名称和版本不能为空")
        object.__setattr__(self, "local_api_url", url)
        object.__setattr__(self, "core_family", self.core_family.strip())
        object.__setattr__(self, "core_version", self.core_version.strip())


__all__ = ["HelixBrowserRuntimeSetting"]
