"""Hubstudio 本机接口 请求和响应检查。"""

from __future__ import annotations

import threading
from typing import Any, Mapping

from server.python.log_s import log

from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import (
    JsonHttpClient,
    JsonResponse,
    RequestsJsonHttpClient,
)
from .hub_browser_default import HUB_LOGIN_PATH
from .hub_browser_error import (
    http_error,
    local_connection_message,
    protocol_error,
    provider_error,
)
from .hub_browser_runtime_setting import HubBrowserRuntimeSetting


class HubBrowserApi:
    """只负责登录、请求和错误归一化，不承担浏览器业务参数转换。"""

    def __init__(
        self,
        setting: HubBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.http = http or RequestsJsonHttpClient()
        self._login_lock = threading.Lock()
        self._logged_in = False

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept-Language": self.setting.accept_language,
            "Authorization": self.setting.authorization,
            "Content-Type": "application/json",
        }

    def post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        operation: str,
        timeout: float | None = None,
        allow_none: bool = False,
    ) -> Any:
        self.ensure_logged_in()
        response = self._post_raw(
            path,
            body,
            operation=operation,
            timeout=timeout,
        )
        return self.success_data(
            response,
            operation,
            allow_none=allow_none,
        )

    def ensure_logged_in(self) -> None:
        credentials = (
            self.setting.app_id.strip(),
            self.setting.app_secret.strip(),
            self.setting.group_code.strip(),
        )
        if not any(credentials):
            return
        if not all(credentials):
            raise BrowserError(
                provider="hub",
                operation="login",
                code="missing_credentials",
                message=(
                    "Hubstudio 登录需要同时配置 "
                    "app_id、app_secret、group_code"
                ),
                retryable=False,
            )
        if self._logged_in:
            return
        with self._login_lock:
            if self._logged_in:
                return
            response = self._post_raw(
                HUB_LOGIN_PATH,
                {
                    "appId": credentials[0],
                    "appSecret": credentials[1],
                    "groupCode": credentials[2],
                },
                operation="login",
            )
            self.success_data(response, "login", allow_none=True)
            self._logged_in = True

    def success_data(
        self,
        response: JsonResponse,
        operation: str,
        *,
        allow_none: bool = False,
    ) -> Any:
        if not 200 <= response.status_code < 300:
            raise http_error(response, operation)
        if not isinstance(response.data, Mapping):
            raise protocol_error(operation, "API 响应不是 JSON 对象", response)
        payload = response.data
        if str(payload.get("code")) != "0":
            raise provider_error(response, operation, payload)
        data = payload.get("data")
        if operation == "delete" and data is not True:
            raise protocol_error(operation, "删除响应未返回 data=true，未确认环境已删除", response)
        if isinstance(data, (Mapping, list)):
            return data
        if data is True:
            return {}
        if allow_none and data is None:
            return {}
        raise protocol_error(operation, "成功响应缺少有效 data", response)

    def _post_raw(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        operation: str,
        timeout: float | None = None,
    ) -> JsonResponse:
        try:
            return self.http.request(
                "POST",
                f"{self.setting.local_api_url}{path}",
                json=body,
                headers=self.headers,
                timeout=timeout or self.setting.request_timeout,
            )
        except BrowserError:
            raise
        except Exception as exc:
            message = local_connection_message(
                self.setting.local_api_url,
                exc,
            )
            log.warning(message)
            raise BrowserError(
                provider="hub",
                operation=operation,
                code="connection_failed",
                message=message,
                retryable=True,
            ) from exc


__all__ = ["HubBrowserApi"]
