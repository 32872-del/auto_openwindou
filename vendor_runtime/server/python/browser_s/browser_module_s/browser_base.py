"""浏览器公共实现：所有指纹浏览器共同遵守的基础接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from .browser_error import BrowserError
from server.python.browser_s.browser_info import (
    BrowserCapabilities,
    BrowserDeviceModel,
    BrowserGroup,
    BrowserHealth,
    BrowserInfo,
    StartedBrowserInfo,
)
from .browser_limit import BrowserLimits, BrowserUsage
from ..browser_task_setting import CreateBrowserSetting, StartBrowserSetting


class BrowserBase(ABC):
    """指纹浏览器统一接口；所有厂商只实现这一套固定方法。"""

    key: str
    display_name: str
    process_name: str
    capabilities: BrowserCapabilities

    #: 启动接口是否接受一次性命令行参数（``StartBrowserSetting.flags``）。
    #: MoreLogin 和 VMLogin 的启动接口没有这个字段，只能改环境的持久启动参数，
    #: 它们把 flags 视为明确错误。过检测层要求的启动参数据此按厂商下发，
    #: 不能对全部厂商一刀切。
    supports_start_flags: bool = True

    @abstractmethod
    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        """创建浏览器，但不启动自动化驱动。"""

        raise NotImplementedError

    @abstractmethod
    def start_browser(self, setting: StartBrowserSetting) -> StartedBrowserInfo:
        """启动浏览器并返回调试端口等连接信息。"""

        raise NotImplementedError

    def create_one_time_browser(
        self,
        setting: CreateBrowserSetting,
        *,
        headless: bool = False,
        timeout_seconds: int = 120,
        flags: tuple[str, ...] = (),
    ) -> StartedBrowserInfo:
        """创建并启动一次性浏览器；不支持时必须明确返回错误。"""

        del setting, headless, timeout_seconds, flags
        raise self._unsupported(
            "create_one_time_browser",
            "创建一次性浏览器",
        )

    @abstractmethod
    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        """关闭浏览器；是否删除由调用方另行决定。"""

        raise NotImplementedError

    @abstractmethod
    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        """删除指定浏览器。"""

        raise NotImplementedError

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, Any],
    ) -> BrowserInfo:
        """修改浏览器参数；尚未接入的厂商会返回明确错误。"""

        del browser_id, values
        raise self._unsupported("update_browser", "修改浏览器")

    def get_browser(self, browser_id: str) -> BrowserInfo:
        """查询一个浏览器；尚未接入的厂商会返回明确错误。"""

        del browser_id
        raise self._unsupported("get_browser", "查询浏览器")

    def list_browsers(
        self,
        *,
        group: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[BrowserInfo, ...]:
        """分页查询浏览器列表。"""

        del group, page, page_size
        raise self._unsupported("list_browsers", "查询浏览器列表")

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        """查询厂商分组；未接入时不能用空列表伪装成真实结果。"""

        raise self._unsupported("list_groups", "查询浏览器分组")

    def list_device_models(
        self,
        *,
        refresh: bool = False,
    ) -> tuple[BrowserDeviceModel, ...]:
        """读取 Android 机型；联网刷新只能由明确的 ``refresh`` 触发。"""

        del refresh
        raise self._unsupported("list_device_models", "查询 Android 机型")

    @abstractmethod
    def check_health(self) -> BrowserHealth:
        """检查本地客户端或厂商 API 是否可用。"""

        raise NotImplementedError

    def get_limits(self) -> BrowserLimits:
        """读取最大数量；厂商没有接口时明确标记为不可获取。"""

        return BrowserLimits.unavailable(
            f"{self.display_name} 尚未接入厂商额度查询"
        )

    def get_usage(self) -> BrowserUsage:
        """读取当前用量；厂商没有接口时明确标记为不可获取。"""

        return BrowserUsage.unavailable(
            f"{self.display_name} 尚未接入厂商用量查询"
        )

    def _unsupported(self, operation: str, label: str) -> BrowserError:
        return BrowserError(
            provider=self.key,
            operation=operation,
            code="unsupported_operation",
            message=f"{self.display_name} 暂不支持{label}",
            retryable=False,
        )

__all__ = ["BrowserBase"]
