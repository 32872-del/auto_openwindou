"""O 浏览器错误翻译、脱敏和限流响应头解析。"""

from __future__ import annotations

import re
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import JsonResponse
from server.python.log_s.client_safe_log import REDACT_CLIENT_LOGS


_SENSITIVE_KEY = re.compile(
    r"(token|password|passwd|secret|authorization|cookie|login)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)(token|password|passwd|secret|authorization|cookie|login)"
    r"(\s*[:=]\s*)([^,\s;]+)"
)

_ERROR_EXPLANATIONS = {
    "invalid_parameters": "O 浏览器请求参数无效",
    "login_required": "O 浏览器当前没有登录账号，请先在 O 浏览器内登录",
    "subscription_inactive": "O 浏览器当前账号没有有效订阅，请核对本机登录账号及订阅状态",
    "unauthorized": "O 浏览器 登录信息不正确或已经失效",
    "forbidden": "O 浏览器账号没有执行该操作的权限",
    "not_found": "O 浏览器环境不存在",
    "rate_limited": "O 浏览器 API 请求额度已用完",
    "connection_failed": "无法连接 O 浏览器 API",
    "invalid_response": "O 浏览器 服务返回的内容无法识别",
    "timeout": "O 浏览器操作超时",
    "busy": "O 浏览器当前繁忙",
    "provider_rejected": "O 浏览器拒绝了本次操作",
}


def safe_text(value: Any, *, maximum: int = 500) -> str:
    """只给服务端内部诊断保留脱敏后的厂商信息。"""

    text = str(value or "").strip()
    if not REDACT_CLIENT_LOGS:
        return text
    text = _INLINE_SECRET.sub(r"\1\2<已隐藏>", text)
    return text[:maximum]


def safe_data(value: Any) -> Any:
    """递归脱敏厂商返回值，供错误详情和对象 raw 使用。"""

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


def local_connection_message(api_url: str, error: BaseException) -> str:
    """把本地 API 连接异常转换成可直接操作的中文提示。"""

    try:
        parsed = urlsplit(api_url)
        port: int | str = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        port = "未知"
    message = f"[O浏览器] 软件未打开、连接地址不可用或端口 {port} 未监听"
    return message if REDACT_CLIENT_LOGS else f"{message}；地址：{api_url}；原因：{type(error).__name__}: {error}"


def remote_connection_message(error: BaseException) -> str:
    """说明官方服务不可达，不把请求细节发给客户端。"""

    message = "[O浏览器] 无法连接官方服务，请检查网络后重试"
    return message if REDACT_CLIENT_LOGS else f"{message}；原因：{type(error).__name__}: {error}"


def translated_message(
    code: str,
    raw_message: Any,
    *,
    fallback: str,
) -> str:
    """输出不包含请求细节的中文解释。"""

    explanation = _ERROR_EXPLANATIONS.get(code, fallback)
    original = safe_text(raw_message)
    if not REDACT_CLIENT_LOGS and original and original not in explanation:
        return f"{explanation}；厂商原文：{original}"
    return explanation


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    return next(
        (
            str(value)
            for key, value in headers.items()
            if key.lower() == name.lower()
        ),
        None,
    )


def header_int(headers: Mapping[str, str], name: str) -> int | None:
    value = header_value(headers, name)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """优先使用 Retry-After，否则把官方 reset 时间戳换算成秒。"""

    try:
        retry_after = header_value(headers, "Retry-After")
        if retry_after is not None:
            return max(0.0, float(retry_after))
        reset = header_value(headers, "X-Ratelimit-Reset")
        if reset is None:
            return None
        return max(0.0, float(reset) - time.time())
    except (TypeError, ValueError):
        return None


def protocol_error(operation: str, message: str, response: JsonResponse | None = None) -> BrowserError:
    return BrowserError(
        provider="octo",
        operation=operation,
        code="invalid_response",
        status_code=response.status_code if response else None,
        details=response.diagnostics() if response else None,
        message=translated_message(
            "invalid_response",
            message,
            fallback="O 浏览器 服务返回的内容无法识别",
        ),
        retryable=False,
    )


def http_error(response: JsonResponse, operation: str) -> BrowserError:
    payload = response.data if isinstance(response.data, Mapping) else {}
    provider_code = str(payload.get("code") or "").strip()
    code = {
        "accounts.no_active": "login_required",
    }.get(provider_code) or {
        400: "invalid_parameters",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        429: "rate_limited",
    }.get(response.status_code, f"http_{response.status_code}")
    raw_message = (
        payload.get("msg")
        or payload.get("message")
        or payload.get("error")
        or f"HTTP {response.status_code}"
    )
    if (operation == "start" and response.status_code == 400 and
            str(raw_message).strip().rstrip(".").casefold() == "no active subscription"):
        code = "subscription_inactive"
    return BrowserError(
        provider="octo",
        operation=operation,
        code=code,
        message=translated_message(
            code,
            raw_message,
            fallback=f"O 浏览器 API HTTP {response.status_code}",
        ),
        status_code=response.status_code,
        retryable=response.status_code == 429 or response.status_code >= 500,
        retry_after=retry_after_seconds(response.headers),
        details=response.diagnostics(raw=safe_data(response.data)),
    )


__all__ = [
    "header_int",
    "header_value",
    "http_error",
    "local_connection_message",
    "protocol_error",
    "retry_after_seconds",
    "remote_connection_message",
    "safe_data",
    "safe_text",
    "translated_message",
]
