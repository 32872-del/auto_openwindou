"""ruyi 浏览器的固定名称和默认路径。"""

RUYI_BROWSER_KEY = "ruyi"
RUYI_BROWSER_NAME = "ruyi"
RUYI_BROWSER_PROCESS = "firefox.exe"
RUYI_BROWSER_PATH = ""
RUYI_PROFILE_ROOT = "浏览器缓存/ruyi_缓存"
RUYI_CORE_ROOT = "浏览器缓存/ruyi_缓存/内核"
RUYI_FINGERPRINT_ROOT = "如意指纹参数"
# 探针必须保持只读，缺少内核时由执行端维护人员明确允许安装。
RUYI_AUTO_DOWNLOAD_CORE = False

__all__ = [
    "RUYI_AUTO_DOWNLOAD_CORE",
    "RUYI_BROWSER_KEY",
    "RUYI_BROWSER_NAME",
    "RUYI_BROWSER_PATH",
    "RUYI_BROWSER_PROCESS",
    "RUYI_CORE_ROOT",
    "RUYI_FINGERPRINT_ROOT",
    "RUYI_PROFILE_ROOT",
]
