"""O 浏览器 本机接口 与 Remote API 调用。"""

from __future__ import annotations

from typing import Any, Mapping

from server.python.log_s import log

from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import (
    JsonHttpClient,
    JsonResponse,
    RequestsJsonHttpClient,
)
from .o_browser_error import (
    http_error,
    local_connection_message,
    protocol_error,
    retry_after_seconds,
    remote_connection_message,
    safe_data,
    translated_message,
)
from .o_browser_runtime_setting import OBrowserRuntimeSetting


class OBrowserApi:
    """只负责请求、响应检查和错误归一化，不承担业务调度。"""

    def __init__(
        self,
        setting: OBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.http = http or RequestsJsonHttpClient()
        self.last_rate_headers: dict[str, str] = {}

    @property
    def auth_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Octo-Api-Token": self.setting.api_token,
        }

    def request_local(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        operation: str,
        timeout: float,
    ) -> JsonResponse:
        try:
            response = self.http.request(
                method,
                f"{self.setting.local_api_url}{path}",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            self._remember_rate_headers(response.headers)
            return response
        except BrowserError as exc:
            if exc.status_code == 401:
                raise self.login_error(operation, 401) from exc
            raise
        except Exception as exc:
            message = local_connection_message(
                self.setting.local_api_url,
                exc,
            )
            log.warning(message)
            raise BrowserError(
                provider="octo",
                operation=operation,
                code="connection_failed",
                message=message,
                retryable=True,
            ) from exc

    def request_remote(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        operation: str,
    ) -> JsonResponse:
        if not self.setting.api_token.strip():
            raise BrowserError(
                provider="octo",
                operation=operation,
                code="missing_credentials",
                message="O 浏览器没有填写接口密钥",
                retryable=False,
            )
        try:
            response = self.http.request(
                method,
                f"{self.setting.remote_api_url}/{path.lstrip('/')}",
                json=body,
                headers=self.auth_headers,
                timeout=self.setting.request_timeout,
            )
            self._remember_rate_headers(response.headers)
            return response
        except BrowserError:
            raise
        except Exception as exc:
            message = remote_connection_message(exc)
            log.warning(message)
            raise BrowserError(
                provider="octo",
                operation=operation,
                code="connection_failed",
                message=message,
                retryable=True,
            ) from exc

    def remote_data(
        self,
        response: JsonResponse,
        operation: str,
        *,
        require_data: bool = True,
    ) -> Any:
        payload = self.response_mapping(response, operation)
        self.ensure_http_ok(response, operation)
        if payload.get("success") is not True:
            code = str(payload.get("code") or "provider_rejected")
            raw_message = (
                payload.get("msg")
                or payload.get("message")
                or "request rejected"
            )
            raise BrowserError(
                provider="octo",
                operation=operation,
                code=code,
                message=translated_message(
                    code,
                    raw_message,
                    fallback="O 浏览器拒绝了本次操作",
                ),
                status_code=response.status_code,
                retryable=code in {"rate_limited", "timeout", "busy"},
                retry_after=retry_after_seconds(response.headers),
                details=response.diagnostics(raw=safe_data(response.data)),
            )
        data = payload.get("data")
        if require_data and not isinstance(data, (Mapping, list)):
            raise protocol_error(operation, "成功响应缺少 data 对象或列表", response)
        return data

    def local_payload(
        self,
        response: JsonResponse,
        operation: str,
    ) -> Mapping[str, Any]:
        if response.status_code == 401:
            raise self.login_error(operation, response.status_code, response.context.get("requestId"))
        payload = self.response_mapping(response, operation)
        if payload.get("code") in ("unauthenticated", "accounts.no_active", "login_required"):
            raise self.login_error(operation, response.status_code, response.context.get("requestId"))
        self.ensure_http_ok(response, operation)
        state = str(payload.get("state") or "").upper()
        if not payload.get("error") and payload.get("success") is not False and state not in {
            "ERROR",
            "FAILED",
        }:
            return payload
        code = str(payload.get("code") or "provider_rejected")
        raw_message = (
            payload.get("msg")
            or payload.get("message")
            or payload.get("error")
            or "O 浏览器本机接口拒绝了本次操作"
        )
        raise BrowserError(
            provider="octo",
            operation=operation,
            code=code,
            message=translated_message(
                code,
                raw_message,
                fallback="O 浏览器操作失败，请稍后重试",
            ),
            status_code=response.status_code,
            retryable=code in {"rate_limited", "timeout", "busy"},
            retry_after=retry_after_seconds(response.headers),
            details=response.diagnostics(raw=safe_data(response.data)),
        )

    @staticmethod
    def login_error(operation: str, status_code: int, request_id: str | None = None) -> BrowserError:
        return BrowserError(provider="octo", operation=operation, code="login_required",
            message="O 浏览器桌面客户端未登录或登录已失效，请在客户端完成登录；仅填写云端 API Token 不会建立桌面登录会话",
            status_code=status_code, retryable=False,
            details={"requestId":request_id, "endpoint":"local"})

    @staticmethod
    def response_mapping(
        response: JsonResponse,
        operation: str,
    ) -> Mapping[str, Any]:
        if not isinstance(response.data, Mapping):
            raise protocol_error(operation, "API 响应不是 JSON 对象", response)
        return response.data

    @staticmethod
    def ensure_http_ok(response: JsonResponse, operation: str) -> None:
        if not 200 <= response.status_code < 300:
            raise http_error(response, operation)

    def _remember_rate_headers(self, headers: Mapping[str, str]) -> None:
        for key, value in headers.items():
            if key.lower().startswith("x-ratelimit") or key.lower() == "retry-after":
                self.last_rate_headers[str(key)] = str(value)


__all__ = ["OBrowserApi"]
