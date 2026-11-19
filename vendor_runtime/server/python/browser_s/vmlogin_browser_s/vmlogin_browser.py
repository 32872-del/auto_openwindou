"""VMLogin 统一浏览器实现。"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote, urlencode

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
from .vmlogin_browser_api import VMLoginBrowserApi
from .vmlogin_browser_default import (
    VMLOGIN_BROWSER_KEY,
    VMLOGIN_BROWSER_NAME,
    VMLOGIN_BROWSER_PROCESS,
    VMLOGIN_CREATE_PATH,
    VMLOGIN_DELETE_PATH,
    VMLOGIN_DETAIL_PATH,
    VMLOGIN_GROUP_LIST_PATH,
    VMLOGIN_LIST_PATH,
    VMLOGIN_LOCAL_LIST_PATH,
    VMLOGIN_LOCAL_START_PATH,
    VMLOGIN_LOCAL_STOP_PATH,
    VMLOGIN_OS_NAMES,
    VMLOGIN_UPDATE_PATH,
)
from .vmlogin_browser_error import protocol_error, safe_data
from .vmlogin_browser_runtime_setting import VMLoginBrowserRuntimeSetting


class VMLoginBrowser(BrowserBase):
    """VMLogin 的参数转换和固定公开操作。"""

    key = VMLOGIN_BROWSER_KEY
    display_name = VMLOGIN_BROWSER_NAME
    process_name = VMLOGIN_BROWSER_PROCESS
    capabilities = BrowserCapabilities(
        force_close_browser=True,
        android_fingerprint=True,
        groups=True,
    )
    # 启动接口没有一次性命令行参数字段，只能改环境的 browserParams。
    supports_start_flags = False

    def __init__(
        self,
        setting: VMLoginBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.api = VMLoginBrowserApi(setting, http)

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        token = self.api.require_token("create")
        body = self._create_body(setting)
        payload = self.api.request(
            "POST",
            f"{self.setting.remote_api_url}{VMLOGIN_CREATE_PATH}",
            {"token": token, "Body": body},
            operation="create",
        )
        browser_id = str(payload.get("value") or "").strip()
        if not browser_id:
            raise protocol_error("create", "成功响应缺少 value")
        return BrowserInfo(
            self.key,
            browser_id,
            setting.name,
            group=setting.group,
            raw=safe_data(payload),
        )

    def start_browser(
        self,
        setting: StartBrowserSetting,
    ) -> StartedBrowserInfo:
        if setting.flags:
            raise ValueError(
                "VMLogin 启动接口不支持临时 flags，"
                "请先用 update_browser 修改 browserParams"
            )
        browser_id = quote(self._browser_id(setting.browser_id), safe="")
        query = urlencode({
            "skiplock": "true",
            "block": "true",
            "profileId": browser_id,
        })
        # profileId 已先按路径值转义，避免 urlencode 再次编码百分号。
        query = query.replace(
            f"profileId={quote(browser_id, safe='')}",
            f"profileId={browser_id}",
        )
        payload = self.api.request(
            "GET",
            (
                f"{self.setting.local_api_url}"
                f"{VMLOGIN_LOCAL_START_PATH}?{query}"
            ),
            None,
            operation="start",
            timeout=max(
                self.setting.start_timeout,
                setting.timeout_seconds,
            ),
        )
        value = str(payload.get("value") or "")
        try:
            debug_port = int(
                value.replace("https://", "")
                .replace("http://", "")
                .rsplit(":", 1)[-1]
            )
        except ValueError as exc:
            raise protocol_error(
                "start",
                "成功响应缺少有效 value 端口",
            ) from exc
        return StartedBrowserInfo(
            self.key,
            setting.browser_id,
            debug_port,
            f"http://127.0.0.1:{debug_port}",
            self.process_name,
            safe_data(payload),
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        value = quote(self._browser_id(browser_id), safe="")
        self.api.request(
            "GET",
            (
                f"{self.setting.local_api_url}{VMLOGIN_LOCAL_STOP_PATH}"
                f"?profileId={value}"
                f"&force={'true' if force else 'false'}&block=true"
            ),
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
        url = self.api.remote_query(
            VMLOGIN_DELETE_PATH,
            profileId=self._browser_id(browser_id),
        )
        self.api.request("GET", url, None, operation="delete")

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, Any],
    ) -> BrowserInfo:
        value = self._browser_id(browser_id)
        body = dict(values)
        if not body:
            raise ValueError("修改浏览器时 values 不能为空")
        supplied_id = str(body.pop("profileId", value))
        if supplied_id != value:
            raise ValueError("values.profileId 与 browser_id 不一致")
        token = self.api.require_token("update")
        self.api.request(
            "POST",
            f"{self.setting.remote_api_url}{VMLOGIN_UPDATE_PATH}",
            {
                "token": token,
                "profileId": value,
                "Body": body,
            },
            operation="update",
        )
        return BrowserInfo(
            provider=self.key,
            browser_id=value,
            name=str(body.get("name") or value),
            group=str(body.get("tag") or ""),
            raw=safe_data(body),
        )

    def get_browser(self, browser_id: str) -> BrowserInfo:
        value = self._browser_id(browser_id)
        url = self.api.remote_query(
            VMLOGIN_DETAIL_PATH,
            profileId=value,
        )
        payload = self.api.request("GET", url, None, operation="get")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise protocol_error("get", "成功响应缺少 data 对象")
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
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size 必须在 1 到 1000 之间")
        url = self.api.remote_query(
            VMLOGIN_LIST_PATH,
            tag=group,
            page=page,
            limit=page_size,
            format=1,
        )
        payload = self.api.request("GET", url, None, operation="list")
        data = payload.get("data")
        items = (
            data.get("list")
            if isinstance(data, Mapping)
            else data
        )
        if not isinstance(items, list):
            raise protocol_error("list", "成功响应缺少 data 列表")
        return tuple(
            self._browser_info(item)
            for item in items
            if isinstance(item, Mapping)
            and (
                item.get("profileId")
                or item.get("profile_id")
                or item.get("id")
            )
        )

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        url = self.api.remote_query(VMLOGIN_GROUP_LIST_PATH)
        payload = self.api.request(
            "GET",
            url,
            None,
            operation="list_groups",
        )
        data = payload.get("data")
        items = (
            data.get("list")
            if isinstance(data, Mapping)
            else data
        )
        if not isinstance(items, list):
            raise protocol_error(
                "list_groups",
                "成功响应缺少 data 分组列表",
            )
        return tuple(
            BrowserGroup(
                group_id=str(
                    item.get("tagId")
                    or item.get("id")
                    or ""
                ),
                name=str(
                    item.get("tag")
                    or item.get("name")
                    or ""
                ),
                raw=safe_data(item),
            )
            for item in items
            if isinstance(item, Mapping)
            and (item.get("tagId") or item.get("id"))
            and (item.get("tag") or item.get("name"))
        )

    def check_health(self) -> BrowserHealth:
        try:
            self.api.request(
                "GET",
                (
                    f"{self.setting.local_api_url}"
                    f"{VMLOGIN_LOCAL_LIST_PATH}"
                ),
                None,
                operation="health",
                timeout=min(self.setting.request_timeout, 3),
            )
            state = (
                HealthState.ONLINE
                if self.setting.api_token
                else HealthState.MISCONFIGURED
            )
            message = (
                "VMLogin 本机接口可以正常连接"
                if self.setting.api_token
                else "VMLogin 本机接口可以连接，但没有填写接口密钥"
            )
            return BrowserHealth(
                self.key,
                state,
                message,
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
            "VMLogin 官方 API 未提供账号的创建、打开和请求上限"
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "VMLogin 官方 API 未提供账号当前用量"
        )

    @staticmethod
    def _create_body(setting: CreateBrowserSetting) -> dict[str, Any]:
        options = dict(setting.fingerprint.options)
        body: dict[str, Any] = {
            "name": setting.name,
            "tag": setting.group,
            "os": VMLOGIN_OS_NAMES[setting.fingerprint.os],
            "startUrl": "about:blank",
            "langBasedOnIp": True,
            "timeZoneFillOnStart": True,
            "webRtc": {"type": "FAKE"},
            "maskFonts": True,
            "canvasDefType": "NOISEB",
            "audio": {"noise": True},
            "platform": (
                "MacIntel"
                if setting.fingerprint.os == "mac"
                else "Linux armv8l"
                if setting.fingerprint.os == "android"
                else "Win32"
            ),
        }
        field_map = {
            "core_version": "kernelVer",
            "cpu_cores": "hardwareConcurrency",
            "memory_gb": "deviceMemory",
        }
        for source, target in field_map.items():
            value = options.pop(source, None)
            if value not in (None, ""):
                body[target] = value
        resolution = options.pop("resolution", None)
        if resolution:
            try:
                width, height = str(resolution).lower().split("x", 1)
                body["screenWidth"] = int(width)
                body["screenHeight"] = int(height)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "VMLogin resolution 必须是 宽x高"
                ) from exc
        noise = bool(options.pop("noise", True))
        body["canvasDefType"] = "NOISEB" if noise else "OFF"
        body["audio"] = {"noise": noise}
        body["webgl"] = {"noise": noise}
        if options:
            raise ValueError(
                "VMLogin 存在未支持的指纹参数: "
                + ", ".join(sorted(options))
            )
        if setting.proxy:
            proxy = setting.proxy
            body["proxyServer"] = {
                "setProxyServer": True,
                "type": proxy.protocol.upper(),
                "host": proxy.host,
                "port": str(proxy.port),
                "username": proxy.username,
                "password": proxy.password,
            }
        return body

    @staticmethod
    def _browser_info(
        data: Mapping[str, Any],
        *,
        fallback_id: str = "",
    ) -> BrowserInfo:
        browser_id = str(
            data.get("profileId")
            or data.get("profile_id")
            or data.get("id")
            or fallback_id
        ).strip()
        if not browser_id:
            raise protocol_error("read", "浏览器数据缺少 profileId")
        return BrowserInfo(
            provider=VMLOGIN_BROWSER_KEY,
            browser_id=browser_id,
            name=str(data.get("name") or browser_id),
            group=str(
                data.get("tag")
                or data.get("tagName")
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


__all__ = ["VMLoginBrowser"]
