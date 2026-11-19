"""Hidemium Automation API 请求和响应检查。"""

from __future__ import annotations

from typing import Any, Mapping

from server.python.log_s import safe_client_text
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import (
    JsonHttpClient,
    JsonResponse,
    RequestsJsonHttpClient,
)
from .hidemium_browser_error import (
    protocol_error,
    response_error,
)
from .hidemium_browser_runtime_setting import HidemiumBrowserRuntimeSetting


class HidemiumBrowserApi:
    """只处理 HTTP 和 Hidemium 响应，不承担业务参数转换。"""

    def __init__(
        self,
        setting: HidemiumBrowserRuntimeSetting,
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
        allow_plain: bool = False,
    ) -> Any:
        try:
            response = self.http.request(
                method,
                f"{self.setting.local_api_url}{path}",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=timeout or self.setting.request_timeout,
            )
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(
                provider="hidemium",
                operation=operation,
                code="connection_failed",
                message=safe_client_text(f"Hidemium 的 {operation} 请求失败（{self.setting.local_api_url}{path}）；原因：{safe_client_text(exc)}。请检查浏览器软件及API地址"),
                retryable=True,
            ) from exc
        if allow_plain and 200 <= response.status_code < 300:
            return response.data
        return self.success_payload(response, operation)

    @staticmethod
    def success_payload(
        response: JsonResponse,
        operation: str,
    ) -> Mapping[str, Any]:
        if not isinstance(response.data, Mapping):
            raise protocol_error(operation, "API 响应不是 JSON 对象", response)
        payload = response.data
        nested = payload.get("data")
        candidate = (
            nested
            if isinstance(nested, Mapping)
            and (
                nested.get("type") is not None
                or nested.get("status") is not None
            )
            else payload
        )
        success = (
            str(candidate.get("type") or "").lower() == "success"
            or str(candidate.get("status") or "").lower()
            in {"success", "successfully"}
        )
        if not 200 <= response.status_code < 300 or not success:
            raise response_error(response, operation, candidate)
        return candidate


__all__ = ["HidemiumBrowserApi"]
