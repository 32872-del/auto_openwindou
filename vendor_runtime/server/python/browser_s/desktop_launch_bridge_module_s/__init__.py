"""隔离 Desktop 启动桥内部实现。"""

from .model import DesktopBridgeError, DesktopBridgePolicy, DesktopBridgeStatus
from .native_bridge import DesktopLaunchBridge
from .coordinator import (
    DesktopBridgeLease,
    DesktopBridgeProgram,
    DesktopLaunchCoordinator,
    desktop_launch_coordinator,
)
from .providers import (
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
