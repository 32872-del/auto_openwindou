"""VMLogin 安全默认值和官方固定信息。"""

VMLOGIN_BROWSER_KEY = "vmlogin"
VMLOGIN_BROWSER_NAME = "VMLogin"
VMLOGIN_BROWSER_PROCESS = "chrome"

VMLOGIN_REMOTE_API_URL = "https://api.vmlogin.com/v1"
VMLOGIN_REQUEST_TIMEOUT = 30.0
VMLOGIN_START_TIMEOUT = 120.0

VMLOGIN_CREATE_PATH = "/profile/create"
VMLOGIN_UPDATE_PATH = "/profile/update"
VMLOGIN_DETAIL_PATH = "/profile/detail"
VMLOGIN_LIST_PATH = "/profile/list"
VMLOGIN_DELETE_PATH = "/profile/remove"
VMLOGIN_GROUP_LIST_PATH = "/tag/list"

VMLOGIN_LOCAL_START_PATH = "/api/v1/profile/start"
VMLOGIN_LOCAL_STOP_PATH = "/api/v1/profile/stop"
VMLOGIN_LOCAL_LIST_PATH = "/api/v1/profile/list"

VMLOGIN_OS_NAMES = {
    "win": "Windows",
    "mac": "Mac OS",
    "android": "Android",
    "linux": "Linux",
}

__all__ = [
    "VMLOGIN_BROWSER_KEY",
    "VMLOGIN_BROWSER_NAME",
    "VMLOGIN_BROWSER_PROCESS",
    "VMLOGIN_CREATE_PATH",
    "VMLOGIN_DELETE_PATH",
    "VMLOGIN_DETAIL_PATH",
    "VMLOGIN_GROUP_LIST_PATH",
    "VMLOGIN_LIST_PATH",
    "VMLOGIN_LOCAL_LIST_PATH",
    "VMLOGIN_LOCAL_START_PATH",
    "VMLOGIN_LOCAL_STOP_PATH",
    "VMLOGIN_OS_NAMES",
    "VMLOGIN_REMOTE_API_URL",
    "VMLOGIN_REQUEST_TIMEOUT",
    "VMLOGIN_START_TIMEOUT",
    "VMLOGIN_UPDATE_PATH",
]
