"""O（OctoBrowser）统一浏览器实现。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode

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
from .o_browser_api import OBrowserApi
from .o_browser_default import (
    O_BROWSER_KEY,
    O_BROWSER_NAME,
    O_BROWSER_PROCESS,
    O_ONE_TIME_REQUEST_COST,
    O_START_REQUEST_COST,
    O_STOP_REQUEST_COST,
)
from .o_browser_device import OBrowserDeviceCatalog
from .o_browser_error import header_int, protocol_error, safe_data
from .o_browser_runtime_setting import OBrowserRuntimeSetting


class OBrowser(BrowserBase):
    """O 浏览器的参数转换和固定公开操作。"""

    key = O_BROWSER_KEY
    display_name = O_BROWSER_NAME
    process_name = O_BROWSER_PROCESS
    capabilities = BrowserCapabilities(
        force_close_browser=True,
        one_time_browser=True,
        android_fingerprint=True,
        groups=True,
    )

    def __init__(
        self,
        setting: OBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
        device_catalog: OBrowserDeviceCatalog | None = None,
    ) -> None:
        self.setting = setting
        self.api = OBrowserApi(setting, http)
        self.device_catalog = device_catalog or OBrowserDeviceCatalog()

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        body = self._create_body(setting)
        response = self.api.request_remote(
            "POST",
            "profiles",
            body,
            operation="create",
        )
        data = self.api.remote_data(response, "create")
        if not isinstance(data, Mapping):
            raise protocol_error("create", "成功响应的 data 不是对象")
        browser_id = str(data.get("uuid") or "").strip()
        if not browser_id:
            raise protocol_error("create", "成功响应缺少 data.uuid")
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
            "uuid": setting.browser_id,
            "headless": setting.headless,
            "debug_port": True,
            "timeout": setting.timeout_seconds,
        }
        if setting.flags:
            body["flags"] = list(setting.flags)
        response = self.api.request_local(
            "POST",
            "/api/profiles/start",
            body,
            operation="start",
            timeout=max(self.setting.start_timeout, setting.timeout_seconds),
        )
        payload = self.api.local_payload(response, "start")
        return self._started_browser(setting.browser_id, payload, "start")

    def create_one_time_browser(
        self,
        setting: CreateBrowserSetting,
        *,
        headless: bool = False,
        timeout_seconds: int = 120,
        flags: tuple[str, ...] = (),
    ) -> StartedBrowserInfo:
        """用官方 one-time API 创建并启动临时环境，关闭后由 O 自动删除。"""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        body: dict[str, Any] = {
            "profile_data": self._create_body(setting, one_time=True),
            "headless": headless,
            "debug_port": True,
            "flags": list(flags),
            "timeout": timeout_seconds,
        }
        response = self.api.request_local(
            "POST",
            "/api/profiles/one_time/start",
            body,
            operation="one_time",
            timeout=max(self.setting.start_timeout, timeout_seconds),
        )
        payload = self.api.local_payload(response, "one_time")
        browser_id = str(payload.get("uuid") or "").strip()
        if not browser_id:
            raise protocol_error("one_time", "成功响应缺少 uuid")
        return self._started_browser(browser_id, payload, "one_time")

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        self._require_browser_id(browser_id)
        endpoint = (
            "/api/profiles/force_stop"
            if force
            else "/api/profiles/stop"
        )
        operation = "force_stop" if force else "stop"
        response = self.api.request_local(
            "POST",
            endpoint,
            {"uuid": browser_id},
            operation=operation,
            timeout=self.setting.stop_timeout,
        )
        self.api.local_payload(response, operation)

    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        self._require_browser_id(browser_id)
        response = self.api.request_remote(
            "DELETE",
            "profiles",
            {"uuids": [browser_id], "skip_trash_bin": permanent},
            operation="delete",
        )
        data = self.api.remote_data(response, "delete", require_data=False)
        # O 的 success=true 只表示批量请求已处理，active_uuids 中的环境并未删除。
        # 必须确认本次目标，不能把别的 UUID 或仅有成功外壳当成删除凭据。
        if isinstance(data, Mapping):
            active = data.get("active_uuids")
            if isinstance(active, list) and browser_id in active:
                raise BrowserError(
                    provider=self.key, operation="delete", code="browser_busy",
                    message=f"O 浏览器环境 {browser_id} 仍在运行或关闭中，尚未删除",
                    status_code=response.status_code, retryable=True,
                    details=response.diagnostics(),
                )
            deleted = data.get("deleted_uuids")
            if isinstance(deleted, list) and browser_id in deleted:
                return
        # 兼容旧客户端缺少逐项结果的响应：只查询这个环境确认，不扫整个账号。
        try:
            self.get_browser(browser_id)
        except BrowserError as exc:
            if exc.code == "not_found":
                return
            raise
        raise BrowserError(
            provider=self.key, operation="delete", code="delete_unconfirmed",
            message=f"O 浏览器返回删除响应，但环境 {browser_id} 仍然存在，已保留待清理记录",
            status_code=response.status_code, retryable=True,
            details=response.diagnostics(),
        )

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, Any],
    ) -> BrowserInfo:
        self._require_browser_id(browser_id)
        body = dict(values)
        # 统一浏览器接口使用 name；O 官方 PATCH 接口使用 title。
        # 两者同时出现时以显式的官方字段为准，并且绝不把未知的 name
        # 原样发给厂商，避免官方返回 invalid_parameters。
        if "name" in body:
            body.setdefault("title", body["name"])
            body.pop("name", None)
        if not body:
            raise ValueError("修改浏览器时 values 不能为空")
        response = self.api.request_remote(
            "PATCH",
            f"profiles/{browser_id}",
            body,
            operation="update",
        )
        data = self.api.remote_data(response, "update")
        result = data if isinstance(data, Mapping) else {}
        return BrowserInfo(
            self.key,
            str(result.get("uuid") or browser_id),
            str(body.get("title") or browser_id),
            group=self._group_name(body.get("folder")),
            raw=safe_data(result),
        )

    def get_browser(self, browser_id: str) -> BrowserInfo:
        self._require_browser_id(browser_id)
        response = self.api.request_remote(
            "GET",
            f"profiles/{browser_id}",
            None,
            operation="get",
        )
        data = self.api.remote_data(response, "get")
        if not isinstance(data, Mapping):
            raise protocol_error("get", "成功响应的 data 不是对象")
        return self._browser_info(data)

    def list_browsers(
        self,
        *,
        group: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[BrowserInfo, ...]:
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if not 10 <= page_size <= 100:
            raise ValueError("page_size 必须在 10 到 100 之间")
        query: dict[str, Any] = {
            "page_len": page_size,
            "page": page - 1,
            "fields": "title,folder,tags,status,created_at,updated_at",
        }
        if group:
            query["search"] = group
        response = self.api.request_remote(
            "GET",
            f"profiles?{urlencode(query)}",
            None,
            operation="list",
        )
        data = self.api.remote_data(response, "list")
        if not isinstance(data, list):
            raise protocol_error("list", "成功响应的 data 不是列表")
        return tuple(
            self._browser_info(item)
            for item in data
            if isinstance(item, Mapping) and item.get("uuid")
        )

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        response = self.api.request_remote(
            "GET",
            "folders",
            None,
            operation="list_groups",
        )
        data = self.api.remote_data(response, "list_groups")
        if not isinstance(data, list):
            raise protocol_error(
                "list_groups",
                "成功响应的 data 不是列表",
            )
        return tuple(
            BrowserGroup(
                group_id=str(item.get("uuid") or ""),
                name=str(item.get("name") or ""),
                raw=safe_data(item),
            )
            for item in data
            if isinstance(item, Mapping)
            and item.get("uuid")
            and item.get("name")
        )

    def list_device_models(
        self,
        *,
        refresh: bool = False,
    ) -> tuple[BrowserDeviceModel, ...]:
        """读取本地缓存；用户明确刷新时才调用 O 官方公共 API。"""

        try:
            grouped = (
                self.device_catalog.refresh(self.setting.api_token)
                if refresh
                else self.device_catalog.load()
            )
        except ValueError as exc:
            raise BrowserError(
                provider=self.key,
                operation="list_device_models",
                code="missing_credentials",
                message="O 浏览器缺少接口密钥，无法更新安卓机型",
                retryable=False,
            ) from exc
        except Exception as exc:
            raise BrowserError(
                provider=self.key,
                operation="list_device_models",
                code="device_models_failed",
                message=(
                    "O 浏览器安卓机型更新失败，"
                    "请检查网络、接口密钥和官方使用上限"
                ),
                retryable=True,
            ) from exc
        return tuple(
            BrowserDeviceModel(
                value=str(item.get("value") or ""),
                brand=str(brand),
                os_versions=tuple(
                    str(version)
                    for version in item.get("os_versions", ())
                    if str(version).strip()
                ),
            )
            for brand, items in grouped.items()
            for item in items
            if str(item.get("value") or "").strip()
        )

    def ensure_logged_in(self) -> None:
        """只读验证桌面会话；云 API Token 不能替代本机已登录会话。"""
        response = self.api.request_local(
            "GET", "/api/username", None, operation="preflight",
            timeout=min(self.setting.request_timeout, 5),
        )
        payload = self.api.local_payload(response, "preflight")
        if not str(payload.get("username") or "").strip():
            raise BrowserError(provider=self.key, operation="preflight", code="login_required",
                message="O 浏览器桌面客户端未登录，请在客户端完成登录后重试；本次尚未请求创建或启动环境")

    def check_health(self) -> BrowserHealth:
        try:
            self.ensure_logged_in()
            response = self.api.request_local(
                "GET",
                "/api/profiles/active",
                None,
                operation="health",
                timeout=min(self.setting.request_timeout, 3),
            )
            self.api.ensure_http_ok(response, "health")
            return BrowserHealth(
                provider=self.key,
                state=HealthState.ONLINE,
                message="O 浏览器本机接口可以正常连接",
                local_api_url=self.setting.local_api_url,
            )
        except BrowserError as exc:
            state = (
                HealthState.MISCONFIGURED
                if exc.code in {
                    "unauthorized",
                    "forbidden",
                    "login_required",
                    "missing_credentials",
                }
                else HealthState.OFFLINE
            )
            return BrowserHealth(
                provider=self.key,
                state=state,
                message=exc.diagnostic_message(),
                local_api_url=self.setting.local_api_url,
            )

    def get_limits(self) -> BrowserLimits:
        self._ensure_rate_headers()
        headers = self.api.last_rate_headers
        per_minute = header_int(headers, "X-Ratelimit-Limit")
        per_hour = header_int(headers, "X-Ratelimit-Limit-Hour")
        source = (
            LimitSource.OFFICIAL_API
            if per_minute is not None or per_hour is not None
            else LimitSource.OFFICIAL_DOCS
        )
        note = (
            "O 官方 API 返回了请求频率上限；创建上限未提供"
            if source is LimitSource.OFFICIAL_API
            else "O 官方未返回请求频率上限；创建上限未提供，只显示官方已确认的操作消耗"
        )
        return BrowserLimits(
            max_requests_per_minute=per_minute,
            max_requests_per_hour=per_hour,
            start_request_cost=O_START_REQUEST_COST,
            stop_request_cost=O_STOP_REQUEST_COST,
            one_time_request_cost=O_ONE_TIME_REQUEST_COST,
            source=source,
            note=note,
        )

    def get_usage(self) -> BrowserUsage:
        self._ensure_rate_headers()
        headers = self.api.last_rate_headers
        remaining_minute = header_int(
            headers,
            "X-Ratelimit-Remaining",
        )
        remaining_hour = header_int(
            headers,
            "X-Ratelimit-Remaining-Hour",
        )
        reset_timestamp = header_int(headers, "X-Ratelimit-Reset")
        if remaining_minute is None and remaining_hour is None:
            return BrowserUsage.unavailable(
                "O 官方未提供当前剩余可用次数"
            )
        reset_at = (
            datetime.fromtimestamp(reset_timestamp, tz=timezone.utc)
            if reset_timestamp is not None
            else None
        )
        return BrowserUsage(
            remaining_requests_minute=remaining_minute,
            remaining_requests_hour=remaining_hour,
            reset_at=reset_at,
            source=LimitSource.OFFICIAL_API,
            note="来自 O 官方 API 响应头",
        )

    @staticmethod
    def _create_body(
        setting: CreateBrowserSetting,
        *,
        one_time: bool = False,
    ) -> dict[str, Any]:
        fingerprint = {
            "os": setting.fingerprint.os,
            **(
                {"os_version": setting.fingerprint.os_version}
                if setting.fingerprint.os_version
                else {}
            ),
            **(
                {"os_arch": setting.fingerprint.architecture}
                if setting.fingerprint.architecture
                else {}
            ),
            **(
                {"device_type": setting.fingerprint.device_type}
                if setting.fingerprint.device_type
                else {}
            ),
            **(
                {"device_model": setting.fingerprint.device_model}
                if setting.fingerprint.device_model
                else {}
            ),
            **dict(setting.fingerprint.options),
        }
        # metadata 只给执行端内部跟踪线程、所有权和测试资源使用。
        # O 官方创建接口不接受这些内部字段；原样放到请求顶层会返回 HTTP 400。
        body: dict[str, Any] = {}
        if not one_time:
            body["title"] = setting.name
        body["fingerprint"] = fingerprint
        tags = list(dict.fromkeys((*setting.tags, setting.group)))
        if tags := [tag for tag in tags if tag]:
            body["tags"] = tags
        if setting.proxy:
            body["proxy"] = {
                "type": setting.proxy.protocol,
                "host": setting.proxy.host,
                "port": setting.proxy.port,
                "login": setting.proxy.username,
                "password": setting.proxy.password,
            }
        return body

    def _ensure_rate_headers(self) -> None:
        if self.api.last_rate_headers:
            return
        response = self.api.request_remote(
            "GET",
            "profiles?page_len=100&page=0&fields=title",
            None,
            operation="limits",
        )
        self.api.remote_data(response, "limits")

    def _started_browser(
        self,
        browser_id: str,
        payload: Mapping[str, Any],
        operation: str,
    ) -> StartedBrowserInfo:
        data = payload.get("data")
        sources = (
            payload,
            data if isinstance(data, Mapping) else {},
        )
        port_value = next(
            (
                source.get(key)
                for source in sources
                for key in ("debug_port", "remote_port", "port")
                if source.get(key) not in (None, "")
            ),
            None,
        )
        try:
            port = int(port_value)
        except (TypeError, ValueError) as exc:
            raise protocol_error(
                operation,
                "成功响应缺少有效调试端口",
            ) from exc
        cdp_url = next(
            (
                str(source.get(key))
                for source in sources
                for key in ("ws_endpoint", "cdp_url", "puppeteer")
                if source.get(key)
            ),
            f"http://127.0.0.1:{port}",
        )
        return StartedBrowserInfo(
            provider=self.key,
            browser_id=browser_id,
            debug_port=port,
            cdp_url=cdp_url,
            process_name=self.process_name,
            raw=safe_data(payload),
        )

    def _browser_info(self, data: Mapping[str, Any]) -> BrowserInfo:
        browser_id = str(data.get("uuid") or "").strip()
        if not browser_id:
            raise protocol_error("read", "浏览器数据缺少 uuid")
        return BrowserInfo(
            provider=self.key,
            browser_id=browser_id,
            name=str(data.get("title") or browser_id),
            group=self._group_name(data.get("folder")),
            status=str(data.get("status") or ""),
            raw=safe_data(data),
        )

    @staticmethod
    def _group_name(value: Any) -> str:
        if isinstance(value, Mapping):
            return str(value.get("name") or value.get("uuid") or "")
        return str(value or "")

    @staticmethod
    def _require_browser_id(browser_id: str) -> None:
        if not browser_id.strip():
            raise ValueError("browser_id 不能为空")


__all__ = ["OBrowser"]
