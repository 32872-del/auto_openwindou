"""浏览器公共实现：可注入 JSON HTTP 客户端。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json as json_module
import time
from typing import Any, Mapping, Protocol
from uuid import uuid4

import requests
from server.python.log_s import log, safe_client_text

from .browser_error import BrowserError


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """保留厂商响应状态、JSON 数据和限流响应头。"""

    status_code: int
    data: Any
    headers: Mapping[str, str]
    context: Mapping[str, Any] = field(default_factory=dict)

    def diagnostics(self, **extra: Any) -> dict[str, Any]:
        return {
            **self.context, "httpStatus": self.status_code,
            "responseHeaders": dict(self.headers), "raw": self.data, **extra,
        }


class JsonHttpClient(Protocol):
    """厂商 API 使用的最小 HTTP 接口，测试时可直接替换。"""

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30,
    ) -> JsonResponse: ...


class RequestsJsonHttpClient:
    """默认 HTTP 实现。

    本地指纹浏览器 API 不读取系统代理，避免 localhost 被 Clash 或其他
    环境代理劫持。业务代理只传给浏览器环境，不参与厂商 API 请求。
    """

    def __init__(self, *, allow_empty: bool = False) -> None:
        self._session = requests.Session()
        self._session.trust_env = False
        self._allow_empty = allow_empty

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30,
    ) -> JsonResponse:
        context = {
            "requestId": uuid4().hex, "method": method, "url": url,
            "requestBody": json, "timeoutSeconds": timeout,
        }
        started = time.monotonic()
        try:
            response = self._session.request(
                method=method, url=url, json=json,
                headers=dict(headers or {}), timeout=timeout,
            )
        except Exception as exc:
            context["elapsedSeconds"] = round(time.monotonic() - started, 3)
            log.error(
                "浏览器 API 请求失败：" + json_module.dumps(context, ensure_ascii=False, default=str),
                exc_info=(type(exc), exc, exc.__traceback__), module="浏览器", operation="API",
            )
            raise
        context["elapsedSeconds"] = round(time.monotonic() - started, 3)
        if response.status_code >= 400:
            log.error(
                "浏览器 API 错误响应：" + json_module.dumps({
                    **context, "httpStatus": response.status_code,
                    "responseHeaders": dict(response.headers), "responseBody": response.text,
                }, ensure_ascii=False, default=str), module="浏览器", operation="API",
            )
        try:
            data = None if self._allow_empty and not response.content and 200 <= response.status_code < 300 else response.json()
        except ValueError as exc:
            details = {**context, "httpStatus": response.status_code, "responseBody": response.text}
            log.error(
                "浏览器 API 响应解析失败：" + json_module.dumps(details, ensure_ascii=False, default=str),
                exc_info=(type(exc), exc, exc.__traceback__), module="浏览器", operation="API",
            )
            raise BrowserError(
                provider="http",
                operation="decode",
                code="invalid_json",
                message=safe_client_text(
                    f"浏览器API {method} {url} 返回非JSON内容（HTTP {response.status_code}）；"
                    f"原因：{safe_client_text(exc)}；响应：{response.text[:2000]}"
                ),
                details=details,
                status_code=response.status_code,
                retryable=response.status_code >= 500,
            ) from exc
        return JsonResponse(
            status_code=response.status_code,
            data=data,
            headers=dict(response.headers),
            context=context,
        )


__all__ = ["JsonHttpClient", "JsonResponse", "RequestsJsonHttpClient"]
