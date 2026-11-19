"""AdsPower 安全默认值和官方固定信息。"""

ADSPOWER_BROWSER_KEY = "adspower"
ADSPOWER_BROWSER_NAME = "AdsPower"
ADSPOWER_BROWSER_PROCESS = "SunBrowser"

ADSPOWER_REQUEST_TIMEOUT = 30.0
ADSPOWER_START_TIMEOUT = 120.0

ADSPOWER_CREATE_PATH = "/api/v2/browser-profile/create"
ADSPOWER_UPDATE_PATH = "/api/v2/browser-profile/update"
ADSPOWER_LIST_PATH = "/api/v2/browser-profile/list"
ADSPOWER_START_PATH = "/api/v2/browser-profile/start"
ADSPOWER_STOP_PATH = "/api/v2/browser-profile/stop"
ADSPOWER_DELETE_PATH = "/api/v2/browser-profile/delete"
ADSPOWER_GROUP_LIST_PATH = "/api/v1/group/list"
ADSPOWER_GROUP_CREATE_PATH = "/api/v1/group/create"

ADSPOWER_SYSTEMS = {
    "win": ["Windows 10", "Windows 11"],
    "mac": ["Mac OS X 12", "Mac OS X 13"],
    "android": ["Android 12", "Android 13"],
}

__all__ = [
    "ADSPOWER_BROWSER_KEY",
    "ADSPOWER_BROWSER_NAME",
    "ADSPOWER_BROWSER_PROCESS",
    "ADSPOWER_CREATE_PATH",
    "ADSPOWER_DELETE_PATH",
    "ADSPOWER_GROUP_CREATE_PATH",
    "ADSPOWER_GROUP_LIST_PATH",
    "ADSPOWER_LIST_PATH",
    "ADSPOWER_REQUEST_TIMEOUT",
    "ADSPOWER_START_PATH",
    "ADSPOWER_START_TIMEOUT",
    "ADSPOWER_STOP_PATH",
    "ADSPOWER_SYSTEMS",
    "ADSPOWER_UPDATE_PATH",
]
