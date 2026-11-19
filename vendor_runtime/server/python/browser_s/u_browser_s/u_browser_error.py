"""U（Undetectable）浏览器错误翻译和脱敏。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import JsonResponse
from server.python.log_s.client_safe_log import REDACT_CLIENT_LOGS


_SENSITIVE_KEY = re.compile(
    r"(token|password|passwd|secret|authorization|cookie|login|account|proxy)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)(token|password|passwd|secret|authorization|cookie|login)"
    r"(\s*[:=]\s*)([^,\s;]+)"
)

_ERROR_EXPLANATIONS = {
    "invalid_parameters": "U 浏览器请求参数无效",
    "unauthorized": "U 浏览器 登录信息不正确或已经失效",
    "forbidden": "U 浏览器没有执行该操作的权限",
    "not_found": "U 浏览器环境不存在",
    "rate_limited": "U 浏览器 请求过于频繁，请稍后重试",
    "connection_failed": "无法连接 U 浏览器 本机接口",
    "invalid_response": "U 浏览器 服务返回的内容无法识别",
    "proxy_failed": "U 浏览器代理不可用或检测失败",
    "browser_busy": "U 浏览器环境已启动、被锁定或正在处理中",
    "already_stopped": "U 浏览器环境已经关闭",
    "provider_rejected": "U 浏览器拒绝了本次操作",
}


def safe_text(value: Any, *, maximum: int = 500) -> str:
    text = str(value or "").strip()
    if not REDACT_CLIENT_LOGS:
        return text
    text = _INLINE_SECRET.sub(r"\1\2<已隐藏>", text)
    return text[:maximum]


def safe_data(value: Any) -> Any:
    """递归隐藏代理、账号、Cookie 和其他凭据字段。"""

    if isinstance(value, Mapping):
        return {
            str(key): (
                "<已隐藏>"
                if REDACT_CLIENT_LOGS and _SENSITIVE_KEY.search(str(key))
                else safe_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_data(item) for item in value]
    if isinstance(value, str):
        return safe_text(value)
    return value


def normalized_code(raw_message: Any) -> str:
    text = str(raw_message or "").strip().lower()
    if text == "invalid profile id!":
        return "not_found"
    if text == "profile not started":
        return "already_stopped"
    if any(word in text for word in ("not found", "does not exist", "не найден")):
        return "not_found"
    if any(word in text for word in ("proxy", "connection check", "ip check")):
        return "proxy_failed"
    if any(word in text for word in ("started", "locked", "busy")):
        return "browser_busy"
    return "provider_rejected"


def translated_message(code: str, raw_message: Any, *, fallback: str) -> str:
    explanation = _ERROR_EXPLANATIONS.get(code, fallback)
    original = safe_text(raw_message)
    if not REDACT_CLIENT_LOGS and original and original not in explanation:
        return f"{explanation}；厂商原文：{original}"
    return explanation


def protocol_error(operation: str, message: str, response: JsonResponse | None = None) -> BrowserError:
    return BrowserError(
        provider="undetectable",
        operation=operation,
        code="invalid_response",
        status_code=response.status_code if response else None,
        details=response.diagnostics() if response else None,
        message=translated_message(
            "invalid_response",
            message,
            fallback="U 浏览器 服务返回的内容无法识别",
        ),
        retryable=False,
    )


def http_error(response: JsonResponse, operation: str) -> BrowserError:
    payload = response.data if isinstance(response.data, Mapping) else {}
    code = {
        400: "invalid_parameters",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        429: "rate_limited",
    }.get(response.status_code, f"http_{response.status_code}")
    raw_message = (
        payload.get("status")
        or payload.get("message")
        or f"HTTP {response.status_code}"
    )
    return BrowserError(
        provider="undetectable",
        operation=operation,
        code=code,
        message=translated_message(
            code,
            raw_message,
            fallback=f"U 浏览器 API HTTP {response.status_code}",
        ),
        status_code=response.status_code,
        retryable=response.status_code == 429 or response.status_code >= 500,
        details=response.diagnostics(raw=safe_data(response.data)),
    )


def provider_error(
    response: JsonResponse,
    operation: str,
    payload: Mapping[str, Any],
) -> BrowserError:
    data = payload.get("data")
    raw_message = (
        data.get("error")
        if isinstance(data, Mapping) and data.get("error")
        else payload.get("status") or "request rejected"
    )
    code = normalized_code(raw_message)
    retryable = code in {"proxy_failed", "browser_busy", "rate_limited"}
    return BrowserError(
        provider="undetectable",
        operation=operation,
        code=code,
        message=translated_message(
            code,
            raw_message,
            fallback="U 浏览器拒绝了本次操作",
        ),
        status_code=response.status_code,
        retryable=retryable,
        retry_after=2.0 if retryable else None,
        details=response.diagnostics(raw=safe_data(response.data)),
    )


__all__ = [
    "http_error",
    "normalized_code",
    "protocol_error",
    "provider_error",
    "safe_data",
    "safe_text",
    "translated_message",
]
