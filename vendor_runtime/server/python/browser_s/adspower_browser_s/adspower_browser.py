"""AdsPower 统一浏览器实现。"""

from __future__ import annotations

import threading
from typing import Any, Mapping
from urllib.parse import quote

from ..browser_module_s.browser_base import BrowserBase
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import JsonHttpClient
from server.python.browser_s.browser_info import (
    BrowserGroup,
    BrowserHealth,
    BrowserInfo,
    HealthState,
    BrowserCapabilities,
    StartedBrowserInfo,
)
from ..browser_module_s.browser_limit import BrowserLimits, BrowserUsage, LimitSource
from ..browser_task_setting import CreateBrowserSetting, StartBrowserSetting
from .adspower_browser_api import AdsPowerBrowserApi
from .adspower_browser_default import (
    ADSPOWER_BROWSER_KEY,
    ADSPOWER_BROWSER_NAME,
    ADSPOWER_BROWSER_PROCESS,
    ADSPOWER_CREATE_PATH,
    ADSPOWER_DELETE_PATH,
    ADSPOWER_GROUP_CREATE_PATH,
    ADSPOWER_GROUP_LIST_PATH,
    ADSPOWER_LIST_PATH,
    ADSPOWER_START_PATH,
    ADSPOWER_STOP_PATH,
    ADSPOWER_SYSTEMS,
    ADSPOWER_UPDATE_PATH,
)
from .adspower_browser_error import protocol_error, safe_data
from .adspower_browser_runtime_setting import AdsPowerBrowserRuntimeSetting


class AdsPowerBrowser(BrowserBase):
    """AdsPower 的参数转换和固定公开操作。"""

    key = ADSPOWER_BROWSER_KEY
    display_name = ADSPOWER_BROWSER_NAME
    process_name = ADSPOWER_BROWSER_PROCESS
    capabilities = BrowserCapabilities(
        android_fingerprint=True,
        groups=True,
    )

    def __init__(
        self,
        setting: AdsPowerBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.api = AdsPowerBrowserApi(setting, http)
        self._groups: dict[str, str] = {}
        self._group_lock = threading.Lock()

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        systems = ADSPOWER_SYSTEMS.get(setting.fingerprint.os)
        if not systems:
            raise ValueError(
                f"AdsPower 不支持 {setting.fingerprint.os} 指纹"
            )
        fingerprint = self._fingerprint(setting, systems)
        body: dict[str, Any] = {
            "name": setting.name,
            "group_id": self._group_id(setting.group, create=True),
            "user_proxy_config": self._proxy(setting),
            "fingerprint_config": fingerprint,
        }
        if remark := setting.metadata.get("remark"):
            body["remark"] = str(remark)
        data = self.api.request(
            "POST",
            ADSPOWER_CREATE_PATH,
            body,
            operation="create",
        )
        if not isinstance(data, Mapping):
            raise protocol_error("create", "成功响应的 data 不是对象")
        browser_id = str(data.get("profile_id") or "").strip()
        if not browser_id:
            raise protocol_error(
                "create",
                "成功响应缺少 data.browser_id",
            )
        return BrowserInfo(
            self.key,
            browser_id,
            setting.name,
            group=setting.group,
            raw=safe_data(data),
        )

    def start_browser(
        self,
        setting: StartBrowserSetting,
    ) -> StartedBrowserInfo:
        body: dict[str, Any] = {
            "profile_id": self._browser_id(setting.browser_id),
            "headless": "1" if setting.headless else "0",
            "cdp_mask": "1",
        }
        if setting.flags:
            body["launch_args"] = list(setting.flags)
        data = self.api.request(
            "POST",
            ADSPOWER_START_PATH,
            body,
            operation="start",
            timeout=max(
                self.setting.start_timeout,
                setting.timeout_seconds,
            ),
        )
        if not isinstance(data, Mapping):
            raise protocol_error("start", "成功响应的 data 不是对象")
        try:
            debug_port = int(data.get("debug_port"))
        except (TypeError, ValueError) as exc:
            raise protocol_error(
                "start",
                "成功响应缺少有效 debug_port",
            ) from exc
        ws = data.get("ws")
        cdp_url = (
            str(ws.get("puppeteer"))
            if isinstance(ws, Mapping) and ws.get("puppeteer")
            else f"http://127.0.0.1:{debug_port}"
        )
        return StartedBrowserInfo(
            self.key,
            setting.browser_id,
            debug_port,
            cdp_url,
            self.process_name,
            safe_data(data),
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        del force
        try:
            self.api.request(
                "POST",
                ADSPOWER_STOP_PATH,
                {"profile_id": self._browser_id(browser_id)},
                operation="stop",
            )
        except BrowserError as exc:
            # 厂商明确确认没有打开的窗口，停止即已完成；保留环境记录，
            # 是否删除仍由调用方的delete_on_close策略决定。
            if exc.code != "already_stopped" or exc.status_code != 200:
                raise

    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        del permanent
        self.api.request(
            "POST",
            ADSPOWER_DELETE_PATH,
            {"profile_id": [self._browser_id(browser_id)]},
            operation="delete",
        )

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, Any],
    ) -> BrowserInfo:
        value = self._browser_id(browser_id)
        body = dict(values)
        if not body:
            raise ValueError("修改浏览器时 values 不能为空")
        supplied_id = str(body.pop("profile_id", value))
        if supplied_id != value:
            raise ValueError("values.browser_id 与 browser_id 不一致")
        body["profile_id"] = value
        # V2 更新字段均按需传入。空 fingerprint_config 会被当前 AdsPower
        # 版本判定为格式错误，因此仅在调用方确实提供指纹修改时发送。
        self.api.request(
            "POST",
            ADSPOWER_UPDATE_PATH,
            body,
            operation="update",
        )
        return BrowserInfo(
            provider=self.key,
            browser_id=value,
            name=str(body.get("name") or value),
            group=str(body.get("group_id") or ""),
            raw=safe_data(body),
        )

    def get_browser(self, browser_id: str) -> BrowserInfo:
        value = self._browser_id(browser_id)
        data = self.api.request(
            "POST",
            ADSPOWER_LIST_PATH,
            {"profile_id": [value], "page": 1, "limit": 1},
            operation="get",
        )
        items = self._browser_list(data, "get")
        if not items:
            raise BrowserError(
                provider=self.key,
                operation="get",
                code="not_found",
                message="AdsPower 中没有找到这个浏览器环境",
            )
        return self._browser_info(items[0])

    def list_browsers(
        self,
        *,
        group: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[BrowserInfo, ...]:
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在 1 到 100 之间")
        body: dict[str, Any] = {"page": page, "limit": page_size}
        if group:
            group_id = self._group_id(group, create=False)
            if not group_id:
                return ()
            body["group_id"] = group_id
        data = self.api.request(
            "POST",
            ADSPOWER_LIST_PATH,
            body,
            operation="list",
        )
        return tuple(
            self._browser_info(item)
            for item in self._browser_list(data, "list")
            if item.get("profile_id")
        )

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        path = f"{ADSPOWER_GROUP_LIST_PATH}?page=1&page_size=2000"
        data = self.api.request(
            "GET",
            path,
            None,
            operation="list_groups",
        )
        if not isinstance(data, Mapping):
            raise protocol_error(
                "list_groups",
                "成功响应的 data 不是对象",
            )
        items = data.get("list")
        if not isinstance(items, list):
            raise protocol_error(
                "list_groups",
                "成功响应缺少 data.list",
            )
        groups = tuple(
            BrowserGroup(
                group_id=str(item.get("group_id") or ""),
                name=str(item.get("group_name") or ""),
                raw=safe_data(item),
            )
            for item in items
            if isinstance(item, Mapping)
            and item.get("group_id") not in (None, "")
            and item.get("group_name")
        )
        with self._group_lock:
            self._groups.update({
                item.name: item.group_id
                for item in groups
            })
        return groups

    def check_health(self) -> BrowserHealth:
        try:
            self.api.request(
                "POST",
                ADSPOWER_LIST_PATH,
                {"page": 1, "limit": 1},
                operation="health",
                timeout=min(self.setting.request_timeout, 3),
            )
            return BrowserHealth(
                self.key,
                HealthState.ONLINE,
                "AdsPower 本机接口 v2 在线",
                self.setting.local_api_url,
            )
        except BrowserError as exc:
            return BrowserHealth(
                self.key,
                HealthState.OFFLINE,
                exc.diagnostic_message(),
                self.setting.local_api_url,
            )

    def get_limits(self) -> BrowserLimits:
        return BrowserLimits(
            source=LimitSource.OFFICIAL_DOCS,
            note=(
                "AdsPower 官方按环境数量限制 API 请求频率：0~200 个环境为每秒 2 次，"
                "200~5000 个环境为每秒 5 次，超过 5000 个环境为每秒 10 次；"
                "部分接口固定每秒 1 次。创建、打开和当前剩余用量未提供"
            ),
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "AdsPower 官方 本机接口 未提供账号当前用量"
        )

    def _group_id(self, name: str, *, create: bool) -> str:
        if not name:
            return "0"
        with self._group_lock:
            cached = self._groups.get(name)
        if cached:
            return cached
        groups = self.list_groups()
        found = next(
            (
                item.group_id
                for item in groups
                if item.name == name or item.group_id == name
            ),
            "",
        )
        if found or not create:
            return found
        data = self.api.request(
            "POST",
            ADSPOWER_GROUP_CREATE_PATH,
            {"group_name": name},
            operation="create_group",
        )
        if not isinstance(data, Mapping):
            raise protocol_error(
                "create_group",
                "成功响应的 data 不是对象",
            )
        group_id = str(data.get("group_id") or "")
        if not group_id:
            raise protocol_error(
                "create_group",
                "成功响应缺少 data.group_id",
            )
        with self._group_lock:
            self._groups[name] = group_id
        return group_id

    @staticmethod
    def _proxy(setting: CreateBrowserSetting) -> dict[str, Any]:
        if not setting.proxy:
            return {
                "proxy_soft": "no_proxy",
                "proxy_type": "http",
                "proxy_host": "",
                "proxy_port": "",
                "proxy_user": "",
                "proxy_password": "",
            }
        proxy = setting.proxy
        return {
            "proxy_soft": "other",
            "proxy_type": proxy.protocol,
            "proxy_host": proxy.host,
            "proxy_port": str(proxy.port),
            "proxy_user": proxy.username,
            "proxy_password": proxy.password,
        }

    @staticmethod
    def _fingerprint(
        setting: CreateBrowserSetting,
        systems: list[str],
    ) -> dict[str, Any]:
        requested_system = setting.fingerprint.os_version.strip().casefold()
        if requested_system:
            systems = [name for name in systems if requested_system in
                       (name.casefold(), name.rsplit(" ", 1)[-1].casefold())]
            if not systems:
                raise ValueError(f"AdsPower 当前适配器不支持指定系统版本：{setting.fingerprint.os_version}")
        options = dict(setting.fingerprint.options)
        core = str(options.pop("core_version", "ua_auto"))
        cpu = str(options.pop("cpu_cores", "4"))
        memory = options.pop("memory_gb", None)
        resolution = str(
            options.pop("resolution", "random")
        ).replace("x", "_")
        noise = bool(options.pop("noise", True))
        if options:
            raise ValueError(
                "AdsPower 存在未支持的指纹参数: "
                + ", ".join(sorted(options))
            )
        # AdsPower 只接受 chrome / firefox 作为内核类型；自动匹配属于
        # version 的取值，不是 type。这里固定创建当前实现支持的 Chrome
        # 环境，避免把 ``ua_auto`` 误传成内核类型。
        kernel = {"type": "chrome", "version": core}
        random_ua: dict[str, Any] = {
            "ua_browser": ["chrome"],
            "ua_system_version": systems,
        }
        if core != "ua_auto":
            random_ua["ua_version"] = [core]
        flag = 1 if noise else 0
        fingerprint: dict[str, Any] = {
            "browser_kernel_config": kernel,
            "random_ua": random_ua,
            "screen_resolution": resolution,
            "hardware_concurrency": cpu,
            "automatic_timezone": "1",
            "language_switch": "1",
            "page_language_switch": "1",
            "location_switch": "1",
            "webrtc": "proxy",
            "canvas": flag,
            "webgl_image": flag,
            "audio": flag,
            "client_rects": flag,
            "media_devices": flag,
            "webgl": "3",
        }
        if memory not in (None, ""):
            fingerprint["device_memory"] = str(memory)
        return fingerprint

    @staticmethod
    def _browser_list(
        data: Any,
        operation: str,
    ) -> list[Mapping[str, Any]]:
        if not isinstance(data, Mapping):
            raise protocol_error(operation, "成功响应的 data 不是对象")
        items = data.get("list")
        if not isinstance(items, list):
            raise protocol_error(operation, "成功响应缺少 data.list")
        return [item for item in items if isinstance(item, Mapping)]

    @staticmethod
    def _browser_info(data: Mapping[str, Any]) -> BrowserInfo:
        browser_id = str(data.get("profile_id") or "").strip()
        if not browser_id:
            raise protocol_error("read", "浏览器数据缺少 profile_id")
        return BrowserInfo(
            provider=ADSPOWER_BROWSER_KEY,
            browser_id=browser_id,
            name=str(data.get("name") or browser_id),
            group=str(
                data.get("group_name")
                or data.get("group_id")
                or ""
            ),
            status=str(data.get("status") or ""),
            raw=safe_data(data),
        )

    @staticmethod
    def _browser_id(browser_id: str) -> str:
        value = browser_id.strip()
        if not value:
            raise ValueError("browser_id 不能为空")
        return value


__all__ = ["AdsPowerBrowser"]
