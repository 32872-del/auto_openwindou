"""Helix 指纹浏览器的统一实现。"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from typing import Any

from ..browser_task_setting import CreateBrowserSetting, StartBrowserSetting
from ..browser_module_s.browser_base import BrowserBase
from ..browser_module_s.browser_error import BrowserError
from server.python.browser_s.browser_info import (
    BrowserCapabilities,
    BrowserGroup,
    BrowserHealth,
    BrowserInfo,
    HealthState,
    StartedBrowserInfo,
)
from ..browser_module_s.browser_limit import BrowserLimits, BrowserUsage
from .helix_browser_default import (
    HELIX_BROWSER_KEY,
    HELIX_BROWSER_NAME,
    HELIX_BROWSER_PROCESS,
)
from .helix_browser_runtime_setting import HelixBrowserRuntimeSetting


class HelixBrowser(BrowserBase):
    """把 Helix 的 Profile 和无 CDP 驱动接入统一接口。"""

    key = HELIX_BROWSER_KEY
    display_name = HELIX_BROWSER_NAME
    process_name = HELIX_BROWSER_PROCESS
    capabilities = BrowserCapabilities(groups=True)

    def __init__(
        self,
        setting: HelixBrowserRuntimeSetting,
        *,
        client: Any | None = None,
    ) -> None:
        self.setting = setting
        self._lock = threading.RLock()
        self._operations: dict[str, Any] = {}
        self._created: set[str] = set()
        self.client = client or self._new_client()

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        self._require_ready("create")
        try:
            profile = self.client.profiles.create(
                name=setting.name,
                group=setting.group or None,
                tags=list(setting.tags) or None,
                proxy=self._proxy(setting),
            )
        except Exception as exc:
            raise self._error("create", exc) from exc
        browser_id = str(profile.id)
        with self._lock:
            self._created.add(browser_id)
        return self._profile_info(profile, fallback_name=setting.name)

    def start_browser(self, setting: StartBrowserSetting) -> StartedBrowserInfo:
        self._require_ready("start")
        self._ensure_core()
        try:
            from helixwright import launch

            operation = launch(
                reuse_profile_id=setting.browser_id,
                client=self.client,
                headless=setting.headless,
                timeout_ms=max(
                    1_000,
                    int(setting.timeout_seconds * 1_000),
                ),
                motion="balanced" if self.setting.humanize else "fast",
            )
        except Exception as exc:
            raise self._error("start", exc) from exc
        with self._lock:
            self._operations[setting.browser_id] = operation
        return StartedBrowserInfo(
            provider=self.key,
            browser_id=setting.browser_id,
            debug_port=None,
            cdp_url="",
            process_name=self.process_name,
            raw={"driver_source": operation},
            driver_type="helix",
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        del force
        with self._lock:
            operation = self._operations.get(browser_id)
        try:
            if operation is not None:
                operation.close()
            else:
                self.client.browser.stop(browser_id)
        except Exception as exc:
            raise self._error("close", exc) from exc
        with self._lock:
            if self._operations.get(browser_id) is operation:
                self._operations.pop(browser_id, None)

    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        del permanent
        with self._lock:
            running = browser_id in self._operations
        if running:
            self.close_browser(browser_id)
        try:
            self.client.profiles.delete(browser_id)
        except Exception as exc:
            raise self._error("delete", exc) from exc
        with self._lock:
            self._created.discard(browser_id)

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, Any],
    ) -> BrowserInfo:
        """通过 Helix Profile API 修改名称、分组或标签。"""

        updates = dict(values)
        unknown = sorted(set(updates) - {"name", "group", "tags"})
        if unknown:
            raise ValueError("Helix 不支持修改这些字段：" + "、".join(unknown))
        if not updates:
            raise ValueError("修改浏览器时 values 不能为空")
        if "group" in updates:
            updates["group_name"] = str(updates.pop("group") or "")
        if "name" in updates:
            updates["name"] = str(updates["name"] or "").strip()
            if not updates["name"]:
                raise ValueError("浏览器名称不能为空")
        if "tags" in updates:
            tags = updates["tags"]
            if not isinstance(tags, (list, tuple)):
                raise ValueError("Helix 标签必须是列表")
            updates["tags"] = [str(item) for item in tags if str(item).strip()]
        try:
            profile = self.client.profiles.update(browser_id, **updates)
        except Exception as exc:
            raise self._error("update", exc) from exc
        return self._profile_info(profile)

    def get_browser(self, browser_id: str) -> BrowserInfo:
        try:
            profile = self.client.profiles.get(browser_id)
        except Exception as exc:
            raise self._error("get", exc) from exc
        return self._profile_info(profile)

    def list_browsers(
        self,
        *,
        group: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[BrowserInfo, ...]:
        if page < 1 or page_size < 1:
            raise ValueError("页码和每页数量必须大于 0")
        try:
            profiles = self.client.profiles.list(
                page=page,
                size=page_size,
                **({"group": group} if group else {}),
            )
        except Exception as exc:
            raise self._error("list", exc) from exc
        return tuple(self._profile_info(profile) for profile in profiles)

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        try:
            profiles = self.client.profiles.list()
        except Exception as exc:
            raise self._error("list_groups", exc) from exc
        names = {
            str(self._profile_data(profile).get("group") or "").strip()
            for profile in profiles
        }
        return tuple(
            BrowserGroup(group_id=name, name=name)
            for name in sorted(names)
            if name
        )

    def check_health(self) -> BrowserHealth:
        try:
            self.client.require_ready()
            cores = self.client.cores.list()
        except Exception as exc:
            error = self._error("health", exc)
            return BrowserHealth(
                provider=self.key,
                state=HealthState.OFFLINE,
                message=error.diagnostic_message(),
                local_api_url=self.setting.local_api_url,
            )
        if not self._has_core(cores):
            return BrowserHealth(
                provider=self.key,
                state=HealthState.MISCONFIGURED,
                message=(
                    f"Helix 服务已连接，但没有 {self.setting.core_version} 内核，"
                    "请先下载内核"
                ),
                local_api_url=self.setting.local_api_url,
            )
        return BrowserHealth(
            provider=self.key,
            state=HealthState.ONLINE,
            message="Helix 服务和浏览器内核都可以使用",
            local_api_url=self.setting.local_api_url,
            version=self.setting.core_version,
        )

    def get_limits(self) -> BrowserLimits:
        return BrowserLimits.unavailable(
            "Helix 当前没有返回使用上限"
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "Helix 当前没有返回账号用量"
        )

    def _new_client(self) -> Any:
        try:
            from helixwright.admin import Client
        except ImportError as exc:
            raise BrowserError(
                provider=self.key,
                operation="init",
                code="python_package_missing",
                message="服务端没有安装 Helix 组件，请先安装 helixwright",
                retryable=False,
            ) from exc
        return Client(
            self.setting.local_api_url,
            timeout=self.setting.request_timeout,
        )

    def _require_ready(self, operation: str) -> None:
        try:
            self.client.require_ready()
        except Exception as exc:
            raise self._error(operation, exc) from exc

    def _ensure_core(self) -> None:
        try:
            cores = self.client.cores.list()
            if self._has_core(cores):
                return
            if not self.setting.auto_download_core:
                raise BrowserError(
                    provider=self.key,
                    operation="start",
                    code="browser_core_missing",
                    message=(
                        f"Helix 缺少 {self.setting.core_version} 内核，"
                        "请先下载内核，或打开“缺少时自动下载内核”"
                    ),
                    retryable=False,
                )
            self.client.cores.ensure(
                self.setting.core_family,
                self.setting.core_version,
            )
        except BrowserError:
            raise
        except Exception as exc:
            raise self._error("download_core", exc) from exc

    def _has_core(self, cores: Any) -> bool:
        try:
            text = json.dumps(
                cores,
                ensure_ascii=False,
                default=lambda value: getattr(value, "__dict__", repr(value)),
            ).lower()
        except Exception:
            text = repr(cores).lower()
        return (
            self.setting.core_family.lower() in text
            and self.setting.core_version.lower() in text
        )

    @staticmethod
    def _profile_data(profile: Any) -> Mapping[str, Any]:
        data = getattr(profile, "data", None)
        return data if isinstance(data, Mapping) else {}

    def _profile_info(
        self,
        profile: Any,
        *,
        fallback_name: str = "",
    ) -> BrowserInfo:
        data = self._profile_data(profile)
        browser_id = str(getattr(profile, "id", "") or data.get("id") or "")
        if not browser_id:
            raise BrowserError(
                provider=self.key,
                operation="profile",
                code="invalid_response",
                message="Helix 返回的数据里没有浏览器编号",
                retryable=False,
            )
        return BrowserInfo(
            provider=self.key,
            browser_id=browser_id,
            name=str(
                getattr(profile, "name", "")
                or data.get("name")
                or fallback_name
                or browser_id
            ),
            group=str(data.get("group") or ""),
            status=str(data.get("status") or ""),
            raw={
                "code": str(getattr(profile, "code", "") or ""),
                "account_id": str(
                    getattr(profile, "account_id", "") or ""
                ),
            },
        )

    @staticmethod
    def _proxy(setting: CreateBrowserSetting) -> Any:
        proxy = setting.proxy
        if proxy is None:
            return None
        from helixwright.admin import Proxy

        return Proxy(
            scheme=proxy.protocol,
            host=proxy.host,
            port=proxy.port,
            username=proxy.username or None,
            password=proxy.password or None,
        )

    def _error(self, operation: str, exc: Exception) -> BrowserError:
        name = type(exc).__name__.lower()
        vendor_code = str(getattr(exc, "code", "") or "").lower()
        status = int(getattr(exc, "status", 0) or 0)
        marker = f"{name} {vendor_code}"
        if "notloggedin" in marker or "login" in marker or status in {401, 403}:
            message = "Helix 还没有登录，请先在服务端完成登录"
            code = "not_logged_in"
            retryable = False
        elif "automationendpoint" in marker or "automation" in vendor_code:
            message = "Helix 自动化功能不可用，请检查浏览器内核"
            code = "automation_unavailable"
            retryable = False
        elif (
            "unavailable" in marker
            or "transport" in marker
            or "connection" in marker
        ):
            message = "Helix 服务没有启动，请先打开 Helix 服务"
            code = "service_offline"
            retryable = True
        elif "timeout" in marker:
            message = "Helix 等待超时，请检查服务和网络"
            code = "timeout"
            retryable = True
        elif "notfound" in marker or status == 404:
            message = "Helix 中没有找到这个浏览器"
            code = "browser_not_found"
            retryable = False
        elif status >= 500:
            message = "Helix 云端服务或网络暂时不可用，请稍后重试"
            code = "vendor_service_unavailable"
            retryable = True
        else:
            message = "Helix 操作失败，请在服务端查看详细日志"
            code = "operation_failed"
            retryable = False
        return BrowserError(
            provider=self.key,
            operation=operation,
            code=code,
            message=message,
            retryable=retryable,
            status_code=status or None,
            details={"errorType": type(exc).__name__, "originalError": str(exc),
                     "providerCode": vendor_code, "raw": getattr(exc, "details", None)},
        )


__all__ = ["HelixBrowser"]
