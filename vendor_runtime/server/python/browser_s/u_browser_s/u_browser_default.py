"""U（Undetectable）浏览器安全默认值和官方固定信息。"""

U_BROWSER_KEY = "undetectable"
U_BROWSER_NAME = "U指纹浏览器"
U_BROWSER_PROCESS = "Undetectable"

U_REQUEST_TIMEOUT = 30.0
U_START_TIMEOUT = 60.0

U_STATUS_PATH = "/status"
U_LIST_PATH = "/list"
U_CREATE_PATH = "/profile/create"
U_UPDATE_PATH = "/profile/update/{browser_id}"
U_GET_PATH = "/profile/getinfo/{browser_id}"
U_START_PATH = "/profile/start/{browser_id}"
U_STOP_PATH = "/profile/stop/{browser_id}"
U_DELETE_PATH = "/profile/delete/{browser_id}"
U_CONFIG_LIST_PATH = "/configslist"
U_GROUP_LIST_PATH = "/groupslist"

U_OS_NAMES = {
    "win": "Windows 10",
    "mac": "Mac",
    "android": "Android",
    "linux": "Linux",
}

__all__ = [
    "U_BROWSER_KEY",
    "U_BROWSER_NAME",
    "U_BROWSER_PROCESS",
    "U_CONFIG_LIST_PATH",
    "U_CREATE_PATH",
    "U_DELETE_PATH",
    "U_GET_PATH",
    "U_GROUP_LIST_PATH",
    "U_LIST_PATH",
    "U_OS_NAMES",
    "U_REQUEST_TIMEOUT",
    "U_START_PATH",
    "U_START_TIMEOUT",
    "U_STATUS_PATH",
    "U_STOP_PATH",
    "U_UPDATE_PATH",
]
