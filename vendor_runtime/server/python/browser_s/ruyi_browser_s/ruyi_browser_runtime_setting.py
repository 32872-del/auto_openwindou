"""ruyi 浏览器内核与数据目录配置。"""

from __future__ import annotations

from dataclasses import dataclass

from .ruyi_browser_default import (
    RUYI_AUTO_DOWNLOAD_CORE,
    RUYI_BROWSER_PATH,
    RUYI_CORE_ROOT,
    RUYI_FINGERPRINT_ROOT,
    RUYI_PROFILE_ROOT,
)


@dataclass(frozen=True, slots=True)
class RuyiBrowserRuntimeSetting:
    """ruyi 启动所需的固定参数。"""

    browser_path: str = RUYI_BROWSER_PATH
    profile_root: str = RUYI_PROFILE_ROOT
    core_root: str = RUYI_CORE_ROOT
    fingerprint_directory: str = RUYI_FINGERPRINT_ROOT
    auto_download_core: bool = RUYI_AUTO_DOWNLOAD_CORE
    download_base_url: str = ""
    geo_timeout: float = 8.0
    geo_retries: int = 1

    def __post_init__(self) -> None:
        if not self.profile_root.strip():
            raise ValueError("ruyi 浏览器数据保存位置不能为空")
        if not self.core_root.strip():
            raise ValueError("ruyi 浏览器内核保存位置不能为空")
        if not self.fingerprint_directory.strip():
            raise ValueError("ruyi 指纹参数保存位置不能为空")
        if self.geo_timeout <= 0 or self.geo_retries < 0:
            raise ValueError("ruyi 网络检查次数或等待时间不正确")
        object.__setattr__(self, "browser_path", self.browser_path.strip())
        object.__setattr__(self, "profile_root", self.profile_root.strip())
        object.__setattr__(self, "core_root", self.core_root.strip())
        object.__setattr__(
            self,
            "fingerprint_directory",
            self.fingerprint_directory.strip(),
        )
        object.__setattr__(
            self,
            "download_base_url",
            self.download_base_url.strip(),
        )


__all__ = ["RuyiBrowserRuntimeSetting"]
