"""O 浏览器连接配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .o_browser_default import (
    O_REMOTE_API_URL,
    O_REQUEST_TIMEOUT,
    O_START_TIMEOUT,
    O_STOP_TIMEOUT,
)


@dataclass(frozen=True, slots=True)
class OBrowserRuntimeSetting:
    """O 本地 API 与公共 API 配置。

    token 不参与对象输出，默认值也不包含任何用户凭据。
    """

    local_api_url: str
    api_token: str = field(default="", repr=False)
    remote_api_url: str = O_REMOTE_API_URL
    request_timeout: float = O_REQUEST_TIMEOUT
    start_timeout: float = O_START_TIMEOUT
    stop_timeout: float = O_STOP_TIMEOUT

    def __post_init__(self) -> None:
        # 从文本文件、剪贴板或旧配置导入的 token 可能携带 UTF-8 BOM。
        # BOM 不能编码进 requests 使用的 HTTP Header，会被外层误报为网络失败。
        normalized_token = str(self.api_token or "").strip().lstrip("\ufeff").strip()
        object.__setattr__(self, "api_token", normalized_token)
        for field_name in ("local_api_url", "remote_api_url"):
            value = getattr(self, field_name).strip().rstrip("/")
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{field_name} 不是有效 HTTP URL")
            object.__setattr__(self, field_name, value)
        if min(
            self.request_timeout,
            self.start_timeout,
            self.stop_timeout,
        ) <= 0:
            raise ValueError("O 浏览器 timeout 必须大于 0")


__all__ = ["OBrowserRuntimeSetting"]
