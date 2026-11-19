"""浏览器状态、能力和启动返回数据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class HealthState(StrEnum):
    """浏览器客户端或本机接口的健康状态。"""

    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    MISCONFIGURED = "misconfigured"


@dataclass(frozen=True, slots=True)
class BrowserCapabilities:
    """一个浏览器厂商当前支持的统一操作。"""

    create_browser: bool = True
    start_browser: bool = True
    close_browser: bool = True
    force_close_browser: bool = False
    delete_browser: bool = True
    one_time_browser: bool = False
    android_fingerprint: bool = False
    groups: bool = False


@dataclass(frozen=True, slots=True)
class BrowserDeviceModel:
    """一家浏览器公开的一条移动设备机型。"""

    value: str
    brand: str = ""
    os: str = "android"
    device_type: str = "phone"
    os_versions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrowserInfo:
    """创建成功但尚未连接驱动的浏览器环境。"""

    provider: str
    browser_id: str
    name: str
    group: str = ""
    status: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


@dataclass(frozen=True, slots=True)
class BrowserGroup:
    """指纹浏览器中的分组、文件夹或同类归档对象。"""

    group_id: str
    name: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


@dataclass(frozen=True, slots=True)
class StartedBrowserInfo:
    """浏览器启动后连接统一驱动所需的信息。"""

    provider: str
    browser_id: str
    debug_port: int | None
    cdp_url: str
    process_name: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)
    driver_type: str = "dp"

    def __post_init__(self) -> None:
        if self.debug_port is not None:
            if not 1 <= int(self.debug_port) <= 65535:
                raise ValueError(f"调试端口无效: {self.debug_port}")
            object.__setattr__(self, "debug_port", int(self.debug_port))
        driver_type = self.driver_type.strip().lower()
        if driver_type in {"drission", "drissionpage", "chrome", "selenium"}:
            driver_type = "dp"
        if driver_type not in {"dp", "win32_dp", "helix", "ruyi"}:
            raise ValueError(f"浏览器驱动类型无效: {self.driver_type}")
        object.__setattr__(self, "driver_type", driver_type)
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


@dataclass(frozen=True, slots=True)
class BrowserHealth:
    """一次浏览器本机接口健康检查结果。"""

    provider: str
    state: HealthState
    message: str
    local_api_url: str = ""
    version: str = ""


__all__ = [
    "BrowserCapabilities",
    "BrowserDeviceModel",
    "BrowserHealth",
    "BrowserGroup",
    "BrowserInfo",
    "HealthState",
    "StartedBrowserInfo",
]
