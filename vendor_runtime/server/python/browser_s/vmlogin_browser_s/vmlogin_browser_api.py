"""VMLogin Local 和 Remote API 请求、响应检查。"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from server.python.log_s import safe_client_text
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import (
    JsonHttpClient,
    JsonResponse,
    RequestsJsonHttpClient,
)
from .vmlogin_browser_error import protocol_error, response_error
from .vmlogin_browser_runtime_setting import VMLoginBrowserRuntimeSetting


class VMLoginBrowserApi:
    """只处理 HTTP、token 和响应，不承担浏览器参数转换。"""

    def __init__(
        self,
        setting: VMLoginBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.http = http or RequestsJsonHttpClient()

    def require_token(self, operation: str) -> str:
        token = self.setting.api_token.strip()
        if not token:
            raise BrowserError(
                provider="vmlogin",
                operation=operation,
                code="missing_credentials",
                message="VMLogin 没有填写接口密钥",
                retryable=False,
            )
        return token

    def remote_query(self, path: str, **values: Any) -> str:
        token = self.require_token("remote")
        query = {"token": token, **values}
        text = "&".join(
            f"{quote(str(key), safe='')}={quote(str(value), safe='')}"
            for key, value in query.items()
            if value not in (None, "")
        )
        return f"{self.setting.remote_api_url}{path}?{text}"

    def request(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any] | None,
        *,
        operation: str,
        timeout: float | None = None,
        require_success_status: bool = True,
    ) -> Mapping[str, Any]:
        try:
            response = self.http.request(
                method,
                url,
                json=body,
                headers={
                    "Content-Type": "application/json;charset=utf-8",
                },
                timeout=timeout or self.setting.request_timeout,
            )
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(
                provider="vmlogin",
                operation=operation,
                code="connection_failed",
                message=safe_client_text(f"VMLogin 的 {operation} 请求失败（{url}）；原因：{safe_client_text(exc)}。请检查浏览器软件及API地址"),
                retryable=True,
            ) from exc
        if not isinstance(response.data, Mapping):
            raise protocol_error(operation, "API 响应不是 JSON 对象", response)
        payload = response.data
        status = str(payload.get("status") or "").lower()
        if (
            not 200 <= response.status_code < 300
            or (
                require_success_status
                and status not in {"ok", "success"}
            )
        ):
            raise response_error(response, operation, payload)
        return payload


__all__ = ["VMLoginBrowserApi"]
