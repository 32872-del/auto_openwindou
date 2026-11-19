"""BitBrowser 安全默认值和官方固定信息。"""

BIT_BROWSER_KEY = "bitbrowser"
BIT_BROWSER_NAME = "BitBrowser"
BIT_BROWSER_PROCESS = "BitBrowser"

BIT_REQUEST_TIMEOUT = 30.0
BIT_START_TIMEOUT = 120.0

BIT_CREATE_PATH = "/browser/update"
BIT_UPDATE_PATH = "/browser/update/partial"
BIT_DETAIL_PATH = "/browser/detail"
BIT_LIST_PATH = "/browser/list"
BIT_START_PATH = "/browser/open"
BIT_STOP_PATH = "/browser/close"
BIT_DELETE_PATH = "/browser/delete"
BIT_GROUP_LIST_PATH = "/group/list"
BIT_GROUP_ADD_PATH = "/group/add"

__all__ = [
    "BIT_BROWSER_KEY",
    "BIT_BROWSER_NAME",
    "BIT_BROWSER_PROCESS",
    "BIT_CREATE_PATH",
    "BIT_DELETE_PATH",
    "BIT_DETAIL_PATH",
    "BIT_GROUP_ADD_PATH",
    "BIT_GROUP_LIST_PATH",
    "BIT_LIST_PATH",
    "BIT_REQUEST_TIMEOUT",
    "BIT_START_PATH",
    "BIT_START_TIMEOUT",
    "BIT_STOP_PATH",
    "BIT_UPDATE_PATH",
]
