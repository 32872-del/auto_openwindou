"""指纹浏览器管理器的隔离 Desktop 启动桥公开入口。"""

from .desktop_launch_bridge_module_s import (
    DesktopBridgeError,
    DesktopBridgeLease,
    DesktopBridgePolicy,
    DesktopBridgeProgram,
    DesktopBridgeStatus,
    DesktopLaunchBridge,
    DesktopLaunchCoordinator,
    desktop_launch_coordinator,
    normalize_desktop_bridge_provider,
    provider_desktop_policy,
    supports_desktop_launch_bridge,
)

__all__ = [
    "DesktopBridgeError",
    "DesktopBridgeLease",
    "DesktopBridgePolicy",
    "DesktopBridgeProgram",
    "DesktopBridgeStatus",
    "DesktopLaunchBridge",
    "DesktopLaunchCoordinator",
    "desktop_launch_coordinator",
    "normalize_desktop_bridge_provider",
    "provider_desktop_policy",
    "supports_desktop_launch_bridge",
]
