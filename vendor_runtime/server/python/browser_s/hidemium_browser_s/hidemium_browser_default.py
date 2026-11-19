"""Hidemium 安全默认值和官方固定信息。"""

HIDEMIUM_BROWSER_KEY = "hidemium"
HIDEMIUM_BROWSER_NAME = "Hidemium"
HIDEMIUM_BROWSER_PROCESS = "chrome"

HIDEMIUM_REQUEST_TIMEOUT = 30.0
HIDEMIUM_START_TIMEOUT = 120.0

# Hidemium 4 创建接口文档明确标注每分钟最多 50 次 API 请求。
HIDEMIUM_MAX_REQUESTS_PER_MINUTE = 50

HIDEMIUM_CREATE_PATH = "/create-profile-custom"
HIDEMIUM_GET_PATH = "/v2/browser/get-profile-by-uuid/{browser_id}"
HIDEMIUM_LIST_PATH = "/v1/browser/list"
HIDEMIUM_UPDATE_ONCE_PATH = "/v2/browser/update-once"
HIDEMIUM_UPDATE_NOTE_PATH = "/v2/browser/update-note"
HIDEMIUM_START_PATH = "/openProfile"
HIDEMIUM_STOP_PATH = "/closeProfile"
HIDEMIUM_DELETE_PATH = "/v1/browser/destroy"
HIDEMIUM_FOLDER_LIST_PATH = "/v1/folder/list"

HIDEMIUM_OS_NAMES = {
    "win": "win",
    "mac": "mac",
    "android": "android",
    "linux": "lin",
}

__all__ = [
    "HIDEMIUM_BROWSER_KEY",
    "HIDEMIUM_BROWSER_NAME",
    "HIDEMIUM_BROWSER_PROCESS",
    "HIDEMIUM_CREATE_PATH",
    "HIDEMIUM_DELETE_PATH",
    "HIDEMIUM_FOLDER_LIST_PATH",
    "HIDEMIUM_GET_PATH",
    "HIDEMIUM_LIST_PATH",
    "HIDEMIUM_MAX_REQUESTS_PER_MINUTE",
    "HIDEMIUM_OS_NAMES",
    "HIDEMIUM_REQUEST_TIMEOUT",
    "HIDEMIUM_START_PATH",
    "HIDEMIUM_START_TIMEOUT",
    "HIDEMIUM_STOP_PATH",
    "HIDEMIUM_UPDATE_NOTE_PATH",
    "HIDEMIUM_UPDATE_ONCE_PATH",
]
