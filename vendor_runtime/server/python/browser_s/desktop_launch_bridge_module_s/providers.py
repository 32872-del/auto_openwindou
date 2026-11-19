"""各指纹浏览器对通用 Desktop 启动桥的识别规则。"""

from __future__ import annotations

from .model import DesktopBridgePolicy


_PROVIDER_EXECUTABLES: dict[str, tuple[str, ...]] = {
    "kameleo": ("chroma.exe", "chrome.exe"),
    "helix": ("helix.exe", "chrome.exe", "chromium.exe"),
    "octo": ("octium.exe", "chrome.exe", "chromium.exe"),
    "hubstudio": ("hubstudio.exe", "chrome.exe", "chromium.exe"),
    "adspower": ("sunbrowser.exe", "chrome.exe", "chromium.exe"),
    "bitbrowser": (
        "比特浏览器.exe",
        "bitbrowser.exe",
        "chrome.exe",
        "chromium.exe",
    ),
    "undetectable": ("undetectable.exe", "chrome.exe", "chromium.exe"),
    "morelogin": ("morelogin.exe", "chrome.exe", "chromium.exe"),
    "hidemium": ("chrome.exe", "chromium.exe"),
    "vmlogin": ("chrome.exe", "chromium.exe"),
}

_CHROMIUM_ROOT_MARKERS = (
    "--remote-debugging-port=",
    "--remote-debugging-pipe",
)

# 某些厂商由管理器目录内的常驻 SDK 启动器创建浏览器根进程。这里必须使用
# 固定相对路径，不能递归扫描或按文件名猜测，否则会把 Hook 注入无关程序。
_PROVIDER_LAUNCHER_RELATIVE_PATHS: dict[str, tuple[str, ...]] = {
    "kameleo": ("Kameleo.Engine.exe",),
    "hubstudio": ("resources/assets/sdk/env-kit.exe",),
    "morelogin": ("resources/assets/window/envkit/env-kit.exe",),
}

# Hub 的浏览器根进程不携带 remote-debugging 参数，该参数只出现在后续渲染
# 子进程；根进程稳定携带厂商环境标识。可执行文件名与标记仍需同时命中。
_PROVIDER_COMMAND_MARKERS: dict[str, tuple[str, ...]] = {
    "hubstudio": ("--browser_id=",),
}

_PROVIDER_ALIASES = {
    "hub": "hubstudio",
    "o": "octo",
}


def normalize_desktop_bridge_provider(provider: str) -> str:
    key = str(provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(key, key)


def supports_desktop_launch_bridge(provider: str) -> bool:
    return normalize_desktop_bridge_provider(provider) in _PROVIDER_EXECUTABLES


def provider_launcher_relative_paths(provider: str) -> tuple[str, ...]:
    """返回厂商审计过的、相对于已确认管理器目录的启动器路径。"""

    key = normalize_desktop_bridge_provider(provider)
    return _PROVIDER_LAUNCHER_RELATIVE_PATHS.get(key, ())


def provider_desktop_policy(
    provider: str,
    desktop_name: str,
) -> DesktopBridgePolicy:
    """为已审计的管理器型指纹浏览器生成严格路由规则。"""

    key = normalize_desktop_bridge_provider(provider)
    try:
        executables = _PROVIDER_EXECUTABLES[key]
    except KeyError as exc:
        raise ValueError("当前厂商未接入 Desktop 启动桥") from exc
    return DesktopBridgePolicy(
        provider=key,
        desktop_name=desktop_name,
        executable_names=executables,
        command_markers=_PROVIDER_COMMAND_MARKERS.get(key, _CHROMIUM_ROOT_MARKERS),
        fail_closed=True,
    )


__all__ = [
    "normalize_desktop_bridge_provider",
    "provider_desktop_policy",
    "provider_launcher_relative_paths",
    "supports_desktop_launch_bridge",
]
