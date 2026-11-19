"""Hubstudio 浏览器安全默认值和官方固定信息。"""

HUB_BROWSER_KEY = "hub"
HUB_BROWSER_NAME = "Hubstudio"
HUB_BROWSER_PROCESS = "hubstudio"

HUB_REQUEST_TIMEOUT = 30.0
HUB_START_TIMEOUT = 120.0
HUB_STOP_TIMEOUT = 15.0

# Hubstudio 官方 API 使用说明明确写明：每个接口每分钟最多请求 100 次。
HUB_MAX_REQUESTS_PER_MINUTE = 100

HUB_CREATE_PATH = "/api/v1/env/create"
HUB_UPDATE_PATH = "/api/v1/env/update"
HUB_LIST_PATH = "/api/v1/env/list"
HUB_DELETE_PATH = "/api/v1/env/del"
HUB_START_PATH = "/api/v1/browser/start"
HUB_STOP_PATH = "/api/v1/browser/stop"
HUB_GROUP_LIST_PATH = "/api/v1/group/list"
HUB_LOGIN_PATH = "/login"

HUB_OS_TYPES = {
    "win": "windows",
    "mac": "macos",
    "android": "android",
}

HUB_PROXY_TYPES = {
    "http": "HTTP",
    "https": "HTTPS",
    "socks5": "Socks5",
}

# 创建环境文档列出的 Android 机型展示名。Hub 不接受 SM-/Pixel 设备代码。
HUB_ANDROID_PHONE_MODELS = (
    "google Pixel 4",
    "google Pixel 5a",
    "google Pixel 6",
    "google Pixel 6a",
    "google Pixel 6 Pro",
    "google Pixel 7",
    "google Pixel 7 Pro",
    "google Pixel 7a",
    "google Pixel 8",
    "google Pixel 8 Pro",
    "google Pixel 8a",
    "Samsung Galaxy S20",
    "Samsung Galaxy S20 +",
    "Samsung Galaxy S21",
    "Samsung Galaxy S21 +",
    "Samsung Galaxy S21 Ultra",
    "Samsung Galaxy S22",
    "Samsung Galaxy S22 +",
    "Samsung Galaxy S22 Ultra",
    "三星Galaxy Note8",
    "三星Galaxy S9+",
    "红米7",
    "红米8",
    "小米9",
    "小米10",
)

__all__ = [
    "HUB_ANDROID_PHONE_MODELS",
    "HUB_BROWSER_KEY",
    "HUB_BROWSER_NAME",
    "HUB_BROWSER_PROCESS",
    "HUB_CREATE_PATH",
    "HUB_DELETE_PATH",
    "HUB_GROUP_LIST_PATH",
    "HUB_LIST_PATH",
    "HUB_LOGIN_PATH",
    "HUB_MAX_REQUESTS_PER_MINUTE",
    "HUB_OS_TYPES",
    "HUB_PROXY_TYPES",
    "HUB_REQUEST_TIMEOUT",
    "HUB_START_PATH",
    "HUB_START_TIMEOUT",
    "HUB_STOP_PATH",
    "HUB_STOP_TIMEOUT",
    "HUB_UPDATE_PATH",
]
