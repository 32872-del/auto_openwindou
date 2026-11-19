"""Hidemium 统一浏览器实现。"""

from __future__ import annotations

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
from .hidemium_browser_api import HidemiumBrowserApi
from .hidemium_browser_default import (
    HIDEMIUM_BROWSER_KEY,
    HIDEMIUM_BROWSER_NAME,
    HIDEMIUM_BROWSER_PROCESS,
    HIDEMIUM_CREATE_PATH,
    HIDEMIUM_DELETE_PATH,
    HIDEMIUM_FOLDER_LIST_PATH,
    HIDEMIUM_GET_PATH,
    HIDEMIUM_LIST_PATH,
    HIDEMIUM_MAX_REQUESTS_PER_MINUTE,
    HIDEMIUM_OS_NAMES,
    HIDEMIUM_START_PATH,
    HIDEMIUM_STOP_PATH,
    HIDEMIUM_UPDATE_NOTE_PATH,
    HIDEMIUM_UPDATE_ONCE_PATH,
)
from .hidemium_browser_error import protocol_error, safe_data
from .hidemium_browser_runtime_setting import HidemiumBrowserRuntimeSetting


class HidemiumBrowser(BrowserBase):
    """Hidemium 的参数转换和固定公开操作。"""

    key = HIDEMIUM_BROWSER_KEY
    display_name = HIDEMIUM_BROWSER_NAME
    process_name = HIDEMIUM_BROWSER_PROCESS
    capabilities = BrowserCapabilities(
        android_fingerprint=True,
        groups=True,
    )

    def __init__(
        self,
        setting: HidemiumBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.api = HidemiumBrowserApi(setting, http)

    @property
    def _local_query(self) -> str:
        return "true" if self.setting.is_local else "false"

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        body = self._create_body(setting)
        payload = self.api.request(
            "POST",
            f"{HIDEMIUM_CREATE_PATH}?is_local={self._local_query}",
            body,
            operation="create",
        )
        content = payload.get("content")
        if isinstance(content, Mapping):
            browser_id = str(
                content.get("uuid")
                or content.get("profile_uuid")
                or content.get("id")
                or ""
            ).strip()
        else:
            browser_id = str(content or "").strip()
        if not browser_id:
            raise protocol_error("create", "成功响应缺少 profile uuid")
        return BrowserInfo(
            self.key,
            browser_id,
            setting.name,
            group=setting.group,
            raw=safe_data(content if isinstance(content, Mapping) else {}),
        )

    def start_browser(
        self,
        setting: StartBrowserSetting,
    ) -> StartedBrowserInfo:
        browser_id = self._browser_id(setting.browser_id)
        path = f"{HIDEMIUM_START_PATH}?uuid={quote(browser_id, safe='')}"
        flags = list(setting.flags)
        if setting.headless and not any(flag.startswith("--headless") for flag in flags):
            flags.append("--headless=new")
        if flags:
            path += f"&command={quote(' '.join(flags), safe='')}"
        payload = self.api.request(
            "GET",
            path,
            None,
            operation="start",
            timeout=max(
                self.setting.start_timeout,
                setting.timeout_seconds,
            ),
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise protocol_error("start", "成功响应缺少 data 对象")
        try:
            debug_port = int(data.get("remote_port"))
        except (TypeError, ValueError) as exc:
            raise protocol_error(
                "start",
                "成功响应缺少 remote_port",
            ) from exc
        return StartedBrowserInfo(
            self.key,
            browser_id,
            debug_port,
            f"http://127.0.0.1:{debug_port}",
            self.process_name,
            safe_data(data),
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        del force
        value = quote(self._browser_id(browser_id), safe="")
        self.api.request(
            "GET",
            f"{HIDEMIUM_STOP_PATH}?uuid={value}",
            None,
            operation="stop",
        )

    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        del permanent
        self.api.request(
            "DELETE",
            f"{HIDEMIUM_DELETE_PATH}?is_local={self._local_query}",
            {"uuid_browser": [self._browser_id(browser_id)]},
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
        unsupported = set(body) - {"name", "note"}
        if unsupported:
            raise ValueError(
                "Hidemium 当前官方简易更新接口仅支持 name、note；"
                "未支持参数: "
                + ", ".join(sorted(unsupported))
            )
        if "name" in body:
            self.api.request(
                "PUT",
                (
                    f"{HIDEMIUM_UPDATE_ONCE_PATH}"
                    f"?is_local={self._local_query}"
                ),
                {
                    "column": "name",
                    "profile_uuid": value,
                    "data": body["name"],
                },
                operation="update_name",
            )
        if "note" in body:
            self.api.request(
                "PUT",
                (
                    f"{HIDEMIUM_UPDATE_NOTE_PATH}"
                    f"?is_local={self._local_query}"
                ),
                {
                    "profile_uuid": value,
                    "note": body["note"],
                },
                operation="update_note",
            )
        return BrowserInfo(
            provider=self.key,
            browser_id=value,
            name=str(body.get("name") or value),
            raw=safe_data(body),
        )

    def get_browser(self, browser_id: str) -> BrowserInfo:
        value = self._browser_id(browser_id)
        path = HIDEMIUM_GET_PATH.format(
            browser_id=quote(value, safe=""),
        )
        payload = self.api.request(
            "GET",
            f"{path}?is_local={self._local_query}",
            None,
            operation="get",
        )
        content = payload.get("content")
        if not isinstance(content, Mapping):
            raise protocol_error("get", "成功响应缺少 content 对象")
        return self._browser_info(content, fallback_id=value)

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
        folder_ids: list[int] = []
        if group:
            group_id = self._group_id(group)
            if not group_id:
                return ()
            folder_ids = [int(group_id)]
        payload = self.api.request(
            "POST",
            f"{HIDEMIUM_LIST_PATH}?is_local={self._local_query}",
            {
                "orderName": 0,
                "orderLastOpen": 0,
                "page": page,
                "limit": page_size,
                "search": "",
                "status": "",
                "date_range": ["", ""],
                "folder_id": folder_ids,
            },
            operation="list",
        )
        content = payload.get("content")
        if not isinstance(content, list):
            raise protocol_error("list", "成功响应缺少 content 列表")
        return tuple(
            self._browser_info(item)
            for item in content
            if isinstance(item, Mapping) and item.get("uuid")
        )

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        data = self.api.request(
            "GET",
            (
                f"{HIDEMIUM_FOLDER_LIST_PATH}"
                f"?is_local={self._local_query}&page=1&limit=2000"
            ),
            None,
            operation="list_groups",
            allow_plain=True,
        )
        if not isinstance(data, list):
            raise protocol_error(
                "list_groups",
                "成功响应不是文件夹列表",
            )
        return tuple(
            BrowserGroup(
                group_id=str(item.get("id") or item.get("uuid") or ""),
                name=str(item.get("name") or ""),
                raw=safe_data(item),
            )
            for item in data
            if isinstance(item, Mapping)
            and (item.get("id") or item.get("uuid"))
            and item.get("name")
        )

    def check_health(self) -> BrowserHealth:
        try:
            self.api.request(
                "POST",
                f"{HIDEMIUM_LIST_PATH}?is_local={self._local_query}",
                {
                    "orderName": 0,
                    "orderLastOpen": 0,
                    "page": 1,
                    "limit": 1,
                    "search": "",
                    "status": "",
                    "date_range": ["", ""],
                    "folder_id": [],
                },
                operation="health",
                timeout=min(self.setting.request_timeout, 3),
            )
            return BrowserHealth(
                self.key,
                HealthState.ONLINE,
                "Hidemium 4 Automation API 在线",
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
            max_requests_per_minute=HIDEMIUM_MAX_REQUESTS_PER_MINUTE,
            source=LimitSource.OFFICIAL_DOCS,
            note=(
                "Hidemium 4 官方文档限制 API 每分钟 50 次；"
                "套餐创建总量和每日创建量因账号方案不同，本机接口 未提供读取接口"
            ),
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "Hidemium 没有返回当前使用次数或剩余可用次数"
        )

    def _group_id(self, name: str) -> str:
        return next(
            (
                item.group_id
                for item in self.list_groups()
                if item.name == name or item.group_id == name
            ),
            "",
        )

    @staticmethod
    def _create_body(setting: CreateBrowserSetting) -> dict[str, Any]:
        options = dict(setting.fingerprint.options)
        noise = bool(options.pop("noise", True))
        body: dict[str, Any] = {
            "os": HIDEMIUM_OS_NAMES[setting.fingerprint.os],
            "browser": "chrome",
            "name": setting.name,
            "canvas": "noise" if noise else "off",
            "webGLImage": str(noise).lower(),
            "audioContext": str(noise).lower(),
            "webGLMetadata": str(noise).lower(),
            "clientRectsEnable": str(noise).lower(),
            "noiseFont": str(noise).lower(),
            "StartURL": "about:blank",
        }
        os_version = HidemiumBrowser._os_version(setting)
        if os_version:
            body["osVersion"] = os_version
        field_map = {
            "core_version": "version",
            "cpu_cores": "hardwareConcurrency",
            "memory_gb": "deviceMemory",
            "resolution": "resolution",
            "language": "language",
        }
        for source, target in field_map.items():
            value = options.pop(source, None)
            if value not in (None, ""):
                body[target] = value
        if options:
            raise ValueError(
                "Hidemium 存在未支持的指纹参数: "
                + ", ".join(sorted(options))
            )
        if setting.group:
            # 官方创建接口会在文件夹不存在时自动创建。
            body["folder_name"] = setting.group
        if setting.proxy:
            proxy = setting.proxy
            body["proxy"] = "|".join((
                proxy.protocol.upper(),
                proxy.host,
                str(proxy.port),
                proxy.username,
                proxy.password,
            ))
        return body

    @staticmethod
    def _os_version(setting: CreateBrowserSetting) -> str:
        value = setting.fingerprint.os_version.strip()
        if setting.fingerprint.os == "win":
            return value if value in {"10", "11"} else "10"
        if setting.fingerprint.os == "mac":
            if not value:
                return "14.0.0"
            return value if "." in value else f"{value}.0.0"
        if setting.fingerprint.os == "android":
            return value or "13"
        return value

    @staticmethod
    def _browser_info(
        data: Mapping[str, Any],
        *,
        fallback_id: str = "",
    ) -> BrowserInfo:
        browser_id = str(data.get("uuid") or fallback_id).strip()
        if not browser_id:
            raise protocol_error("read", "浏览器数据缺少 uuid")
        config = data.get("config")
        config_data = config if isinstance(config, Mapping) else {}
        return BrowserInfo(
            provider=HIDEMIUM_BROWSER_KEY,
            browser_id=browser_id,
            name=str(
                data.get("name")
                or config_data.get("name")
                or browser_id
            ),
            group=str(
                data.get("folder_name")
                or data.get("folder_id")
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


__all__ = ["HidemiumBrowser"]
