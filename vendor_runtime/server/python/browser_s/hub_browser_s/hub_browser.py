"""Hubstudio 统一浏览器实现。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from ..browser_module_s.browser_base import BrowserBase
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import JsonHttpClient
from server.python.browser_s.browser_info import (
    BrowserDeviceModel,
    BrowserGroup,
    BrowserHealth,
    BrowserInfo,
    HealthState,
    BrowserCapabilities,
    StartedBrowserInfo,
)
from ..browser_module_s.browser_limit import BrowserLimits, BrowserUsage, LimitSource
from ..browser_task_setting import CreateBrowserSetting, StartBrowserSetting
from .hub_browser_api import HubBrowserApi
from .hub_browser_default import (
    HUB_ANDROID_PHONE_MODELS,
    HUB_BROWSER_KEY,
    HUB_BROWSER_NAME,
    HUB_BROWSER_PROCESS,
    HUB_CREATE_PATH,
    HUB_DELETE_PATH,
    HUB_GROUP_LIST_PATH,
    HUB_LIST_PATH,
    HUB_MAX_REQUESTS_PER_MINUTE,
    HUB_OS_TYPES,
    HUB_PROXY_TYPES,
    HUB_START_PATH,
    HUB_STOP_PATH,
    HUB_UPDATE_PATH,
)
from .hub_browser_error import protocol_error, safe_data
from .hub_browser_runtime_setting import HubBrowserRuntimeSetting


class HubBrowser(BrowserBase):
    """Hubstudio 的参数转换和固定公开操作。"""

    key = HUB_BROWSER_KEY
    display_name = HUB_BROWSER_NAME
    process_name = HUB_BROWSER_PROCESS
    capabilities = BrowserCapabilities(
        android_fingerprint=True,
        groups=True,
    )
    ANDROID_PHONE_MODELS = HUB_ANDROID_PHONE_MODELS

    def __init__(
        self,
        setting: HubBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.api = HubBrowserApi(setting, http)
        self._available_cores: tuple[int, ...] | None = None

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        body = self._create_body(setting)
        data = self.api.post(
            HUB_CREATE_PATH,
            body,
            operation="create",
        )
        if not isinstance(data, Mapping):
            raise protocol_error("create", "成功响应的 data 不是对象")
        browser_id = str(data.get("containerCode") or "").strip()
        if not browser_id:
            raise protocol_error(
                "create",
                "成功响应缺少 data.containerCode",
            )
        return BrowserInfo(
            provider=self.key,
            browser_id=browser_id,
            name=setting.name,
            group=setting.group,
            raw=safe_data(data),
        )

    def start_browser(
        self,
        setting: StartBrowserSetting,
    ) -> StartedBrowserInfo:
        self._require_browser_id(setting.browser_id)
        body: dict[str, Any] = {
            "containerCode": setting.browser_id,
            "isHeadless": setting.headless,
            "shouldCloseTabsOnOpen": True,
        }
        if setting.flags:
            body["args"] = list(setting.flags)
        data = self.api.post(
            HUB_START_PATH,
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
            debug_port = int(data.get("debuggingPort"))
        except (TypeError, ValueError) as exc:
            raise protocol_error(
                "start",
                "成功响应缺少有效 debuggingPort",
            ) from exc
        return StartedBrowserInfo(
            provider=self.key,
            browser_id=setting.browser_id,
            debug_port=debug_port,
            cdp_url=f"http://127.0.0.1:{debug_port}",
            process_name=self.process_name,
            raw=safe_data(data),
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        del force
        self._require_browser_id(browser_id)
        self.api.post(
            HUB_STOP_PATH,
            {"containerCode": browser_id},
            operation="stop",
            timeout=self.setting.stop_timeout,
        )

    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        del permanent
        self.api.post(
            HUB_DELETE_PATH,
            {"containerCodes": [self._numeric_id(browser_id)]},
            operation="delete",
        )

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, Any],
    ) -> BrowserInfo:
        numeric_id = self._numeric_id(browser_id)
        body = dict(values)
        if not body:
            raise ValueError("修改浏览器时 values 不能为空")
        if "name" in body and "containerName" not in body:
            body["containerName"] = body.pop("name")
        supplied_id = body.pop("containerCode", numeric_id)
        if self._numeric_id(str(supplied_id)) != numeric_id:
            raise ValueError("values.containerCode 与 browser_id 不一致")
        body["containerCode"] = numeric_id
        self.api.post(HUB_UPDATE_PATH, body, operation="update")
        return BrowserInfo(
            provider=self.key,
            browser_id=browser_id,
            name=str(body.get("containerName") or browser_id),
            group=str(body.get("tagName") or body.get("tagCode") or ""),
            raw=safe_data(body),
        )

    def get_browser(self, browser_id: str) -> BrowserInfo:
        numeric_id = self._numeric_id(browser_id)
        for browser in self._all_browsers():
            if self._numeric_id(browser.browser_id) == numeric_id:
                return browser
        raise BrowserError(
            provider=self.key,
            operation="get",
            code="not_found",
            message="Hubstudio 中没有找到这个浏览器环境",
            retryable=False,
        )

    def list_browsers(
        self,
        *,
        group: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[BrowserInfo, ...]:
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if page_size < 1:
            raise ValueError("page_size 必须大于等于 1")
        browsers = self._all_browsers()
        if group:
            browsers = tuple(
                browser
                for browser in browsers
                if browser.group == group
                or str(browser.raw.get("tagCode") or "") == group
            )
        start = (page - 1) * page_size
        return browsers[start:start + page_size]

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        data = self.api.post(
            HUB_GROUP_LIST_PATH,
            {},
            operation="list_groups",
        )
        if not isinstance(data, list):
            raise protocol_error(
                "list_groups",
                "成功响应的 data 不是列表",
            )
        return tuple(
            BrowserGroup(
                group_id=str(item.get("tagCode") or ""),
                name=str(item.get("tagName") or ""),
                raw=safe_data(item),
            )
            for item in data
            if isinstance(item, Mapping)
            and item.get("tagCode") not in (None, "")
            and item.get("tagName")
        )

    def list_device_models(
        self,
        *,
        refresh: bool = False,
    ) -> tuple[BrowserDeviceModel, ...]:
        """Hub 的官方机型表随适配器版本发布，不需要联网刷新。"""

        del refresh
        return tuple(
            BrowserDeviceModel(
                value=model,
                brand=model.split(" ", 1)[0],
            )
            for model in self.ANDROID_PHONE_MODELS
        )

    def check_health(self) -> BrowserHealth:
        try:
            data = self.api.post(
                HUB_LIST_PATH,
                {},
                operation="health",
                timeout=min(self.setting.request_timeout, 3),
            )
            total = (
                data.get("total")
                if isinstance(data, Mapping)
                else None
            )
            suffix = f"，可见环境 {total} 个" if isinstance(total, int) else ""
            return BrowserHealth(
                provider=self.key,
                state=HealthState.ONLINE,
                message=f"Hubstudio 本机接口 在线{suffix}",
                local_api_url=self.setting.local_api_url,
            )
        except BrowserError as exc:
            state = (
                HealthState.MISCONFIGURED
                if exc.code in {"unauthorized", "forbidden"}
                else HealthState.OFFLINE
            )
            return BrowserHealth(
                provider=self.key,
                state=state,
                message=exc.diagnostic_message(),
                local_api_url=self.setting.local_api_url,
            )

    def get_limits(self) -> BrowserLimits:
        return BrowserLimits(
            max_requests_per_minute=HUB_MAX_REQUESTS_PER_MINUTE,
            source=LimitSource.OFFICIAL_DOCS,
            note="Hubstudio 官方说明：每个接口每分钟最多请求 100 次",
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "Hubstudio 官方 本机接口 未提供当前剩余请求量接口"
        )

    def available_core_versions(self, *, refresh: bool = False) -> tuple[int, ...]:
        """读取 Hubstudio 本机真正下载的 Chrome 内核目录。"""

        if self._available_cores is not None and not refresh:
            return self._available_cores
        versions: set[int] = set()
        root = (
            Path(self.setting.core_directory)
            if self.setting.core_directory
            else None
        )
        search_roots = []
        if root and root.is_dir():
            search_roots.append(root)
            if root.name.casefold() == "core" and root.parent.is_dir():
                search_roots.append(root.parent)
        for search_root in search_roots:
            for child in search_root.iterdir():
                match = re.fullmatch(r"chrome_64_(\d+)(?:_.+)?", child.name)
                if match:
                    versions.add(int(match.group(1)))
        self._available_cores = tuple(sorted(versions))
        return self._available_cores

    def _all_browsers(self) -> tuple[BrowserInfo, ...]:
        data = self.api.post(
            HUB_LIST_PATH,
            {},
            operation="list",
        )
        if not isinstance(data, Mapping):
            raise protocol_error("list", "成功响应的 data 不是对象")
        items = data.get("list")
        if not isinstance(items, list):
            raise protocol_error("list", "成功响应缺少 data.list")
        return tuple(
            self._browser_info(item)
            for item in items
            if isinstance(item, Mapping)
            and item.get("containerCode") not in (None, "")
        )

    def _create_body(self, setting: CreateBrowserSetting) -> dict[str, Any]:
        os_type = HUB_OS_TYPES.get(setting.fingerprint.os)
        if not os_type:
            raise ValueError(
                f"Hubstudio 不支持 {setting.fingerprint.os} 指纹"
            )

        options = dict(setting.fingerprint.options)
        requested_core = options.pop("core_version", None)
        if requested_core in (None, ""):
            available_cores = self.available_core_versions()
            if not available_cores:
                raise ValueError("Hub 未找到已安装内核，请设置内核目录或明确选择已验证的内核版本")
            requested_core = available_cores[-1]
        core_version = self._positive_int(requested_core, "core_version")
        advanced = options.pop("advanced", {})
        if not isinstance(advanced, Mapping):
            raise ValueError("Hub advanced 必须是对象")
        if options:
            raise ValueError(
                "Hub 存在未支持的指纹参数: "
                + ", ".join(sorted(options))
            )

        body: dict[str, Any] = {
            "asDynamicType": "0",
            "containerName": setting.name,
            "proxyTypeName": "不使用代理",
            "coreVersion": core_version,
            "type": os_type,
            "browser": "chrome",
            "advancedBo": {
                "uaVersion": core_version,
                **dict(advanced),
            },
            **dict(setting.metadata),
        }
        if setting.fingerprint.os == "android":
            phone_model = setting.fingerprint.device_model.strip()
            if phone_model not in HUB_ANDROID_PHONE_MODELS:
                raise ValueError(
                    "Hubstudio Android 必须使用官方 phoneModel"
                )
            body["phoneModel"] = phone_model
        if setting.group:
            body["tagName"] = setting.group
        if setting.proxy:
            proxy_type = HUB_PROXY_TYPES.get(setting.proxy.protocol)
            if not proxy_type:
                raise ValueError(
                    f"Hubstudio 不支持 {setting.proxy.protocol} 代理"
                )
            body.update({
                "asDynamicType": "2",
                "proxyTypeName": proxy_type,
                "proxyServer": setting.proxy.host,
                "proxyPort": setting.proxy.port,
                "proxyAccount": setting.proxy.username,
                "proxyPassword": setting.proxy.password,
            })
        return body

    def _browser_info(self, data: Mapping[str, Any]) -> BrowserInfo:
        browser_id = str(data.get("containerCode") or "").strip()
        if not browser_id:
            raise protocol_error("read", "浏览器数据缺少 containerCode")
        return BrowserInfo(
            provider=self.key,
            browser_id=browser_id,
            name=str(data.get("containerName") or browser_id),
            group=str(data.get("tagName") or ""),
            status=str(data.get("status") or data.get("state") or ""),
            raw=safe_data(data),
        )

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是整数") from exc
        if result <= 0:
            raise ValueError(f"{name} 必须大于 0")
        return result

    @classmethod
    def _numeric_id(cls, browser_id: str) -> int:
        cls._require_browser_id(browser_id)
        try:
            return int(browser_id)
        except ValueError as exc:
            raise ValueError("Hubstudio browser_id 必须是数字") from exc

    @staticmethod
    def _require_browser_id(browser_id: str) -> None:
        if not browser_id.strip():
            raise ValueError("browser_id 不能为空")


__all__ = ["HubBrowser"]
