"""U（Undetectable）本机接口 请求和响应检查。"""

from __future__ import annotations

from typing import Any, Mapping

from server.python.log_s import safe_client_text
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import (
    JsonHttpClient,
    JsonResponse,
    RequestsJsonHttpClient,
)
from .u_browser_error import http_error, protocol_error, provider_error
from .u_browser_runtime_setting import UBrowserRuntimeSetting


class UBrowserApi:
    """只处理 HTTP 与 U 官方统一响应，不承担浏览器参数转换。"""

    def __init__(
        self,
        setting: UBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.http = http or RequestsJsonHttpClient()

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        operation: str,
        timeout: float | None = None,
    ) -> Any:
        try:
            response = self.http.request(
                method,
                f"{self.setting.local_api_url}{path}",
                json=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=timeout or self.setting.request_timeout,
            )
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(
                provider="undetectable",
                operation=operation,
                code="connection_failed",
                message=safe_client_text(f"U 浏览器 的 {operation} 请求失败（{self.setting.local_api_url}{path}）；原因：{safe_client_text(exc)}。请检查浏览器软件及API地址"),
                retryable=True,
            ) from exc
        return self.success_data(response, operation)

    @staticmethod
    def success_data(response: JsonResponse, operation: str) -> Any:
        if not 200 <= response.status_code < 300:
            raise http_error(response, operation)
        if not isinstance(response.data, Mapping):
            raise protocol_error(operation, "API 响应不是 JSON 对象", response)
        payload = response.data
        if str(payload.get("code")) != "0":
            raise provider_error(response, operation, payload)
        if "data" not in payload:
            raise protocol_error(operation, "成功响应缺少 data", response)
        return payload.get("data")


__all__ = ["UBrowserApi"]
