"""BitBrowser 统一浏览器实现。"""

from __future__ import annotations

import threading
from typing import Any, Mapping

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
from ..browser_module_s.browser_limit import BrowserLimits, BrowserUsage
from ..browser_task_setting import CreateBrowserSetting, StartBrowserSetting
from .bit_browser_api import BitBrowserApi
from .bit_browser_default import (
    BIT_BROWSER_KEY,
    BIT_BROWSER_NAME,
    BIT_BROWSER_PROCESS,
    BIT_CREATE_PATH,
    BIT_DELETE_PATH,
    BIT_DETAIL_PATH,
    BIT_GROUP_ADD_PATH,
    BIT_GROUP_LIST_PATH,
    BIT_LIST_PATH,
    BIT_START_PATH,
    BIT_STOP_PATH,
    BIT_UPDATE_PATH,
)
from .bit_browser_error import protocol_error, safe_data
from .bit_browser_runtime_setting import BitBrowserRuntimeSetting


class BitBrowser(BrowserBase):
    """BitBrowser 的参数转换和固定公开操作。"""

    key = BIT_BROWSER_KEY
    display_name = BIT_BROWSER_NAME
    process_name = BIT_BROWSER_PROCESS
    capabilities = BrowserCapabilities(
        android_fingerprint=True,
        groups=True,
    )

    def __init__(
        self,
        setting: BitBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.api = BitBrowserApi(setting, http)
        self._groups: dict[str, str] = {}
        self._group_lock = threading.Lock()

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        body: dict[str, Any] = {
            "name": setting.name,
            "remark": str(setting.metadata.get("remark") or ""),
            "proxyMethod": 2,
            "proxyType": "noproxy",
            "browserFingerPrint": self._fingerprint(setting),
            "disableTranslatePopup": True,
            "disableNotifications": True,
        }
        if setting.group:
            body["groupId"] = self._group_id(setting.group, create=True)
        if setting.proxy:
            proxy = setting.proxy
            body.update({
                "proxyType": proxy.protocol,
                "host": proxy.host,
                "port": proxy.port,
                "proxyUserName": proxy.username,
                "proxyPassword": proxy.password,
            })
        data = self.api.post(BIT_CREATE_PATH, body, operation="create")
        if not isinstance(data, Mapping):
            raise protocol_error("create", "成功响应的 data 不是对象")
        browser_id = str(data.get("id") or "").strip()
        if not browser_id:
            raise protocol_error("create", "成功响应缺少 data.id")
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
            "id": self._browser_id(setting.browser_id),
            "queue": True,
        }
        if setting.flags:
            body["args"] = list(setting.flags)
        data = self.api.post(
            BIT_START_PATH,
            body,
            operation="start",
            timeout=max(
                self.setting.start_timeout,
                setting.timeout_seconds,
            ),
        )
        if not isinstance(data, Mapping):
            raise protocol_error("start", "成功响应的 data 不是对象")
        address = data.get("http")
        try:
            port = int(str(address).rsplit(":", 1)[-1])
        except (TypeError, ValueError) as exc:
            raise protocol_error(
                "start",
                "成功响应缺少有效 data.http",
            ) from exc
        return StartedBrowserInfo(
            provider=self.key,
            browser_id=setting.browser_id,
            debug_port=port,
            cdp_url=str(address),
            process_name=self.process_name,
            raw=safe_data(data),
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        del force
        self.api.post(
            BIT_STOP_PATH,
            {"id": self._browser_id(browser_id)},
            operation="stop",
            require_data=False,
        )

    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        del permanent
        self.api.post(
            BIT_DELETE_PATH,
            {"id": self._browser_id(browser_id)},
            operation="delete",
            require_data=False,
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
        if "group" in body and "groupId" not in body:
            group_name = str(body.pop("group") or "").strip()
            if group_name:
                body["groupId"] = self._group_id(group_name, create=True)
        supplied_ids = body.pop("ids", [value])
        if list(supplied_ids) != [value]:
            raise ValueError("values.ids 与 browser_id 不一致")
        body["ids"] = [value]
        self.api.post(
            BIT_UPDATE_PATH,
            body,
            operation="update",
            require_data=False,
        )
        return BrowserInfo(
            provider=self.key,
            browser_id=value,
            name=str(body.get("name") or value),
            group=str(body.get("groupId") or ""),
            raw=safe_data(body),
        )

    def get_browser(self, browser_id: str) -> BrowserInfo:
        value = self._browser_id(browser_id)
        data = self.api.post(
            BIT_DETAIL_PATH,
            {"id": value},
            operation="get",
        )
        if not isinstance(data, Mapping):
            raise protocol_error("get", "成功响应的 data 不是对象")
        return self._browser_info(data, fallback_id=value)

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
        body: dict[str, Any] = {
            "page": page - 1,
            "pageSize": page_size,
        }
        if group:
            group_id = self._group_id(group, create=False)
            if not group_id:
                return ()
            body["groupId"] = group_id
        data = self.api.post(BIT_LIST_PATH, body, operation="list")
        if not isinstance(data, Mapping):
            raise protocol_error("list", "成功响应的 data 不是对象")
        items = data.get("list")
        if not isinstance(items, list):
            raise protocol_error("list", "成功响应缺少 data.list")
        return tuple(
            self._browser_info(item)
            for item in items
            if isinstance(item, Mapping) and item.get("id")
        )

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        data = self.api.post(
            BIT_GROUP_LIST_PATH,
            {"page": 0, "pageSize": 100, "all": True},
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
                group_id=str(item.get("id") or ""),
                name=str(item.get("groupName") or ""),
                raw=safe_data(item),
            )
            for item in items
            if isinstance(item, Mapping)
            and item.get("id")
            and item.get("groupName")
        )
        with self._group_lock:
            self._groups.update({
                item.name: item.group_id
                for item in groups
            })
        return groups

    def check_health(self) -> BrowserHealth:
        try:
            self.api.post(
                BIT_LIST_PATH,
                {"page": 0, "pageSize": 1},
                operation="health",
                timeout=min(self.setting.request_timeout, 3),
            )
            return BrowserHealth(
                self.key,
                HealthState.ONLINE,
                "BitBrowser 本机接口 在线",
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
        return BrowserLimits.unavailable(
            "BitBrowser 官方 本机接口 未提供账号的创建、打开和请求上限"
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "BitBrowser 官方 本机接口 未提供账号当前用量"
        )

    def _group_id(self, name: str, *, create: bool) -> str:
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
        sort_number = max(
            (
                int(item.raw.get("sortNum") or 0)
                for item in groups
                if str(item.raw.get("sortNum") or "").isdigit()
            ),
            default=len(groups),
        ) + 1
        data = self.api.post(
            BIT_GROUP_ADD_PATH,
            {"groupName": name, "sortNum": sort_number},
            operation="create_group",
        )
        if not isinstance(data, Mapping):
            raise protocol_error(
                "create_group",
                "成功响应的 data 不是对象",
            )
        group_id = str(data.get("id") or "")
        if not group_id:
            raise protocol_error(
                "create_group",
                "成功响应缺少 data.id",
            )
        with self._group_lock:
            self._groups[name] = group_id
        return group_id

    @staticmethod
    def _fingerprint(setting: CreateBrowserSetting) -> dict[str, Any]:
        fingerprint: dict[str, Any] = {
            "webRTC": "0",
            "isIpCreateTimeZone": True,
            "isIpCreateLanguage": True,
            "isIpCreateDisplayLanguage": True,
            "isIpCreatePosition": True,
            "position": "1",
        }
        if setting.fingerprint.os == "win":
            fingerprint.update({
                "ostype": "PC",
                "os": "Win32",
                "osVersion": setting.fingerprint.os_version or "10",
            })
        elif setting.fingerprint.os == "mac":
            fingerprint.update({"ostype": "PC", "os": "MacIntel"})
        elif setting.fingerprint.os == "android":
            fingerprint.update({
                "ostype": "Android",
                "os": "Linux armv81",
            })
        else:
            raise ValueError(
                f"BitBrowser 不支持 {setting.fingerprint.os} 指纹"
            )
        options = dict(setting.fingerprint.options)
        field_map = {
            "core_version": "coreVersion",
            "cpu_cores": "hardwareConcurrency",
            "memory_gb": "deviceMemory",
            "resolution": "resolution",
        }
        for source, target in field_map.items():
            value = options.pop(source, None)
            if value not in (None, ""):
                fingerprint[target] = str(value)
        if "resolution" in fingerprint:
            fingerprint["resolutionType"] = "1"
            fingerprint["resolution"] = fingerprint["resolution"].replace(
                "x",
                " x ",
            )
        noise_value = "0" if bool(options.pop("noise", True)) else "1"
        fingerprint.update({
            "canvas": noise_value,
            "webGL": noise_value,
            "audioContext": noise_value,
        })
        if options:
            raise ValueError(
                "BitBrowser 存在未支持的指纹参数: "
                + ", ".join(sorted(options))
            )
        return fingerprint

    @staticmethod
    def _browser_info(
        data: Mapping[str, Any],
        *,
        fallback_id: str = "",
    ) -> BrowserInfo:
        browser_id = str(data.get("id") or fallback_id).strip()
        if not browser_id:
            raise protocol_error("read", "浏览器数据缺少 id")
        return BrowserInfo(
            provider=BIT_BROWSER_KEY,
            browser_id=browser_id,
            name=str(data.get("name") or browser_id),
            group=str(
                data.get("groupName")
                or data.get("groupId")
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


__all__ = ["BitBrowser"]
