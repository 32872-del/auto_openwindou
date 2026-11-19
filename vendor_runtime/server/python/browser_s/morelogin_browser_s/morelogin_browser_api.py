"""MoreLogin 本机接口 请求和响应检查。"""

from __future__ import annotations

from typing import Any, Mapping

from server.python.log_s import safe_client_text
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import (
    JsonHttpClient,
    JsonResponse,
    RequestsJsonHttpClient,
)
from .morelogin_browser_error import protocol_error, response_error
from .morelogin_browser_runtime_setting import MoreLoginBrowserRuntimeSetting


class MoreLoginBrowserApi:
    """只处理 HTTP 和 MoreLogin 响应，不承担业务参数转换。"""

    def __init__(
        self,
        setting: MoreLoginBrowserRuntimeSetting,
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
                headers={"Content-Type": "application/json"},
                timeout=timeout or self.setting.request_timeout,
            )
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(
                provider="morelogin",
                operation=operation,
                code="connection_failed",
                message=safe_client_text(f"MoreLogin 的 {operation} 请求失败（{self.setting.local_api_url}{path}）；原因：{safe_client_text(exc)}。请检查浏览器软件及API地址"),
                retryable=True,
            ) from exc
        return self.success_data(response, operation)

    @staticmethod
    def success_data(response: JsonResponse, operation: str) -> Any:
        if not isinstance(response.data, Mapping):
            raise protocol_error(operation, "API 响应不是 JSON 对象", response)
        payload = response.data
        if (
            not 200 <= response.status_code < 300
            or str(payload.get("code")) != "0"
        ):
            raise response_error(response, operation)
        if "data" not in payload:
            raise protocol_error(operation, "成功响应缺少 data", response)
        if operation == "delete" and payload["data"] is not True:
            raise protocol_error(operation, "删除响应未返回 data=true，未确认环境已移入回收站", response)
        return payload.get("data")


__all__ = ["MoreLoginBrowserApi"]
