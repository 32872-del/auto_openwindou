"""BitBrowser 本机接口 请求和响应检查。"""

from __future__ import annotations

from typing import Any, Mapping

from server.python.log_s import safe_client_text
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import (
    JsonHttpClient,
    JsonResponse,
    RequestsJsonHttpClient,
)
from .bit_browser_error import protocol_error, response_error
from .bit_browser_runtime_setting import BitBrowserRuntimeSetting


class BitBrowserApi:
    """只处理 HTTP 和统一响应，不承担业务参数转换。"""

    def __init__(
        self,
        setting: BitBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.http = http or RequestsJsonHttpClient()

    def post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        operation: str,
        timeout: float | None = None,
        require_data: bool = True,
    ) -> Any:
        try:
            response = self.http.request(
                "POST",
                f"{self.setting.local_api_url}{path}",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=timeout or self.setting.request_timeout,
            )
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(
                provider="bitbrowser",
                operation=operation,
                code="connection_failed",
                message=safe_client_text(f"BitBrowser 的 {operation} 请求失败（{self.setting.local_api_url}{path}）；原因：{safe_client_text(exc)}。请检查浏览器软件及API地址"),
                retryable=True,
            ) from exc
        return self.success_data(response, operation, require_data=require_data)

    @staticmethod
    def success_data(
        response: JsonResponse,
        operation: str,
        *,
        require_data: bool = True,
    ) -> Any:
        if not isinstance(response.data, Mapping):
            raise protocol_error(operation, "API 响应不是 JSON 对象", response)
        payload = response.data
        code = payload.get("code")
        rejected = (
            not 200 <= response.status_code < 300
            or payload.get("success") is False
            or code not in (None, 0, "0")
        )
        if rejected:
            raise response_error(response, operation)
        if operation in {"stop", "delete"} and payload.get("success") is not True:
            raise protocol_error(operation, "清理响应缺少明确的 success=true，未确认操作成功", response)
        if require_data and "data" not in payload:
            raise protocol_error(operation, "成功响应缺少 data", response)
        return payload.get("data")


__all__ = ["BitBrowserApi"]
