"""U（Undetectable）统一浏览器实现。"""

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
from ..browser_module_s.browser_limit import BrowserLimits, BrowserUsage
from ..browser_task_setting import CreateBrowserSetting, StartBrowserSetting
from .u_browser_api import UBrowserApi
from .u_browser_default import (
    U_BROWSER_KEY,
    U_BROWSER_NAME,
    U_BROWSER_PROCESS,
    U_CONFIG_LIST_PATH,
    U_CREATE_PATH,
    U_DELETE_PATH,
    U_GET_PATH,
    U_GROUP_LIST_PATH,
    U_LIST_PATH,
    U_OS_NAMES,
    U_START_PATH,
    U_STATUS_PATH,
    U_STOP_PATH,
    U_UPDATE_PATH,
)
from .u_browser_error import protocol_error, safe_data
from .u_browser_runtime_setting import UBrowserRuntimeSetting


class UBrowser(BrowserBase):
    """U 浏览器的参数转换和固定公开操作。"""

    key = U_BROWSER_KEY
    display_name = U_BROWSER_NAME
    process_name = U_BROWSER_PROCESS
    capabilities = BrowserCapabilities(
        android_fingerprint=True,
        groups=True,
    )

    def __init__(
        self,
        setting: UBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.api = UBrowserApi(setting, http)
        self._fingerprint_configs: dict[str, Any] | None = None
        self._fingerprint_lock = threading.Lock()

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        body = self._create_body(setting)
        data = self.api.request(
            "POST",
            U_CREATE_PATH,
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
            provider=self.key,
            browser_id=browser_id,
            name=str(data.get("name") or setting.name),
            group=setting.group,
            raw=safe_data(data),
        )

    def start_browser(
        self,
        setting: StartBrowserSetting,
    ) -> StartedBrowserInfo:
        browser_id = self._browser_id(setting.browser_id)
        path = U_START_PATH.format(browser_id=quote(browser_id, safe=""))
        flags = list(setting.flags)
        if setting.headless and not any(flag.startswith("--headless") for flag in flags):
            flags.append("--headless=new")
        if flags:
            encoded = quote(" ".join(flags), safe="")
            path += f"?chrome_flags={encoded}"
        data = self.api.request(
            "GET",
            path,
            None,
            operation="start",
            timeout=max(
                self.setting.start_timeout,
                setting.timeout_seconds,
            ),
        )
        if not isinstance(data, Mapping):
            raise protocol_error("start", "成功响应的 data 不是对象")
        debug_port = self._extract_port(data)
        return StartedBrowserInfo(
            provider=self.key,
            browser_id=browser_id,
            debug_port=debug_port,
            cdp_url=str(
                data.get("websocket_link")
                or f"http://127.0.0.1:{debug_port}"
            ),
            process_name=self.process_name,
            raw=safe_data(data),
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        del force
        value = quote(self._browser_id(browser_id), safe="")
        try:
            self.api.request(
                "GET",
                U_STOP_PATH.format(browser_id=value),
                None,
                operation="stop",
            )
        except BrowserError as exc:
            # 厂商明确表示该环境没有运行，重复关闭已达到目标；其它错误仍阻断删除。
            if exc.operation != "stop" or exc.status_code != 200 or exc.code != "already_stopped":
                raise

    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        del permanent
        import time
        value = quote(self._browser_id(browser_id), safe="")
        # 当前 U 管理器在关窗后可能先返回删除成功，但环境仍保留。
        # 仅对同一明确ID有界重试；每次都确认未运行，最后必须查到不存在。
        for attempt in range(3):
            try:
                current=self.get_browser(browser_id)
            except BrowserError as exc:
                if exc.code=='not_found':return
                raise
            if str(current.raw.get('status') or '').casefold() in ('started','starting','stopping','closing','locked'):
                raise BrowserError(provider=self.key,operation='delete',code='browser_busy',
                                   message='U 环境仍在关闭或运行中，请等待状态变为可用后删除',retryable=True)
            self.api.request('GET',U_DELETE_PATH.format(browser_id=value),None,operation='delete')
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                try:self.get_browser(browser_id)
                except BrowserError as exc:
                    if exc.code=='not_found':return
                    raise
                time.sleep(.25)
        raise BrowserError(provider=self.key,operation='delete',code='delete_not_confirmed',
                           message='U 已接收删除请求，但3次核对后环境仍存在；请在管理器核实或重试清理',retryable=True)

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, Any],
    ) -> BrowserInfo:
        value = self._browser_id(browser_id)
        body = dict(values)
        if not body:
            raise ValueError("修改浏览器时 values 不能为空")
        self.api.request(
            "POST",
            U_UPDATE_PATH.format(browser_id=quote(value, safe="")),
            body,
            operation="update",
        )
        return BrowserInfo(
            provider=self.key,
            browser_id=value,
            name=str(body.get("name") or value),
            group=str(body.get("group") or body.get("folder") or ""),
            raw=safe_data(body),
        )

    def get_browser(self, browser_id: str) -> BrowserInfo:
        value = self._browser_id(browser_id)
        data = self.api.request(
            "GET",
            U_GET_PATH.format(browser_id=quote(value, safe="")),
            None,
            operation="get",
        )
        if not isinstance(data, Mapping):
            raise protocol_error("get", "成功响应的 data 不是对象")
        return self._browser_info(value, data)

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
        data = self.api.request(
            "GET",
            U_LIST_PATH,
            None,
            operation="list",
        )
        if not isinstance(data, Mapping):
            raise protocol_error("list", "成功响应的 data 不是对象")
        browsers = tuple(
            self._browser_info(str(browser_id), item)
            for browser_id, item in data.items()
            if str(browser_id).strip() and isinstance(item, Mapping)
        )
        if group:
            browsers = tuple(
                browser for browser in browsers if browser.group == group
            )
        start = (page - 1) * page_size
        return browsers[start:start + page_size]

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        data = self.api.request(
            "GET",
            U_GROUP_LIST_PATH,
            None,
            operation="list_groups",
        )
        names: list[str] = []
        if isinstance(data, (list, tuple)):
            names = [str(item) for item in data if str(item).strip()]
        elif isinstance(data, Mapping):
            nested_groups = data.get("groups")
            if isinstance(nested_groups, (list, tuple)):
                names.extend(
                    str(item) for item in nested_groups if str(item).strip()
                )
            else:
                for key, item in data.items():
                    if isinstance(item, str) and item.strip():
                        names.append(item)
                    elif str(key).strip():
                        names.append(str(key))
        else:
            raise protocol_error(
                "list_groups",
                "成功响应的 data 不是分组列表",
            )
        return tuple(
            BrowserGroup(group_id=name, name=name)
            for name in dict.fromkeys(names)
        )

    def check_health(self) -> BrowserHealth:
        try:
            self.api.request(
                "GET",
                U_STATUS_PATH,
                None,
                operation="health",
                timeout=min(self.setting.request_timeout, 3),
            )
            return BrowserHealth(
                provider=self.key,
                state=HealthState.ONLINE,
                message="U 浏览器 本机接口 在线",
                local_api_url=self.setting.local_api_url,
            )
        except BrowserError as exc:
            return BrowserHealth(
                provider=self.key,
                state=HealthState.OFFLINE,
                message=exc.diagnostic_message(),
                local_api_url=self.setting.local_api_url,
            )

    def get_limits(self) -> BrowserLimits:
        return BrowserLimits.unavailable(
            "U 浏览器官方 本机接口 未提供账号的创建、打开和请求上限"
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "U 浏览器官方 本机接口 未提供账号当前用量"
        )

    def list_fingerprint_configs(
        self,
        *,
        refresh: bool = False,
    ) -> Mapping[str, Any]:
        """读取当前 U 客户端可用的真实指纹配置池。"""

        with self._fingerprint_lock:
            if self._fingerprint_configs is None or refresh:
                data = self.api.request(
                    "GET",
                    U_CONFIG_LIST_PATH,
                    None,
                    operation="list_fingerprint_configs",
                )
                if not isinstance(data, Mapping):
                    raise protocol_error(
                        "list_fingerprint_configs",
                        "成功响应的 data 不是对象",
                    )
                self._fingerprint_configs = dict(data)
            return dict(self._fingerprint_configs)

    def _create_body(self, setting: CreateBrowserSetting) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": setting.name,
            "os": self._os_name(setting),
            "browser": "Chrome",
            "type": str(setting.metadata.get("profile_type") or "local"),
        }
        if setting.group:
            body["group"] = setting.group
        if setting.tags:
            body["tags"] = list(setting.tags)
        if setting.proxy:
            proxy = setting.proxy
            body["proxy"] = f"{proxy.protocol}://{proxy.host}:{proxy.port}"
            if proxy.username or proxy.password:
                body["proxy"] += f":{proxy.username}:{proxy.password}"

        options = dict(setting.fingerprint.options)
        field_map = {
            "cpu_cores": "cpu",
            "memory_gb": "memory",
            "resolution": "resolution",
            "config_id": "configid",
            "language": "language",
            "timezone": "timezone",
            "webrtc": "webrtc",
        }
        for source, target in field_map.items():
            value = options.pop(source, None)
            if value not in (None, ""):
                body[target] = value
        if options:
            raise ValueError(
                "U 浏览器存在未支持的指纹参数: "
                + ", ".join(sorted(options))
            )
        return body

    @staticmethod
    def _browser_info(
        browser_id: str,
        data: Mapping[str, Any],
    ) -> BrowserInfo:
        return BrowserInfo(
            provider=U_BROWSER_KEY,
            browser_id=browser_id,
            name=str(data.get("name") or browser_id),
            group=str(data.get("group") or data.get("folder") or ""),
            status=str(data.get("status") or ""),
            raw=safe_data(data),
        )

    @staticmethod
    def _extract_port(data: Mapping[str, Any]) -> int:
        for key in (
            "remote_debugging_address",
            "debug_port",
            "port",
            "http",
        ):
            value = data.get(key)
            if value in (None, ""):
                continue
            try:
                return int(str(value).rsplit(":", 1)[-1])
            except ValueError:
                continue
        raise protocol_error("start", "成功响应缺少有效调试端口")

    @staticmethod
    def _os_name(setting: CreateBrowserSetting) -> str:
        if setting.fingerprint.os == "win":
            return (
                "Windows 11"
                if setting.fingerprint.os_version.strip() == "11"
                else "Windows 10"
            )
        return U_OS_NAMES[setting.fingerprint.os]

    @staticmethod
    def _browser_id(browser_id: str) -> str:
        value = browser_id.strip()
        if not value:
            raise ValueError("browser_id 不能为空")
        return value


__all__ = ["UBrowser"]
