"""本地普通浏览器配置。"""

from __future__ import annotations

from dataclasses import dataclass

from .chrome_browser_default import CHROME_INI_PATH, CHROME_PROFILE_ROOT


@dataclass(frozen=True, slots=True)
class ChromeBrowserRuntimeSetting:
    """只保留启动本地 Chrome 必需的参数。"""

    browser_path: str = ""
    profile_root: str = CHROME_PROFILE_ROOT
    ini_path: str = CHROME_INI_PATH

    def __post_init__(self) -> None:
        if not self.profile_root.strip():
            raise ValueError("浏览器数据保存位置不能为空")
        object.__setattr__(self, "browser_path", self.browser_path.strip())
        object.__setattr__(self, "profile_root", self.profile_root.strip())
        object.__setattr__(self, "ini_path", self.ini_path.strip())


__all__ = ["ChromeBrowserRuntimeSetting"]
