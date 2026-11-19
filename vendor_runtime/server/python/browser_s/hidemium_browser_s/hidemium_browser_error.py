"""Hidemium 错误翻译和脱敏。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import JsonResponse
from server.python.log_s.client_safe_log import REDACT_CLIENT_LOGS


_SENSITIVE_KEY = re.compile(
    r"(key|token|password|passwd|secret|authorization|cookie|username|proxy|account)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)(key|token|password|passwd|secret|authorization|cookie|username)"
    r"(\s*[:=]\s*)([^,\s;]+)"
)

_ERROR_EXPLANATIONS = {
    "invalid_parameters": "Hidemium 请求参数无效",
    "unauthorized": "Hidemium 登录信息不正确或已经失效",
    "forbidden": "Hidemium 账号没有执行该操作的权限",
    "not_found": "Hidemium 环境不存在",
    "rate_limited": "Hidemium API 已超过每分钟请求上限",
    "connection_failed": "无法连接 Hidemium 服务",
    "invalid_response": "Hidemium 服务返回的内容无法识别",
    "proxy_failed": "Hidemium 代理不可用或检测失败",
    "browser_busy": "Hidemium 环境正在启动、关闭或处理中",
    "provider_rejected": "Hidemium 拒绝了本次操作",
    "local_not_activated": "Hidemium 尚未激活本机的本地环境，请在厂商软件“设置 → 设备管理”中激活本机后重试",
}


def safe_text(value: Any, *, maximum: int = 500) -> str:
    text = str(value or "").strip()
    if not REDACT_CLIENT_LOGS:
        return text
    text = _INLINE_SECRET.sub(r"\1\2<已隐藏>", text)
    return text[:maximum]


def safe_data(value: Any) -> Any:
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


def normalized_code(raw_code: Any, raw_message: Any) -> str:
    code = str(raw_code or "").lower()
    text = str(raw_message or "").lower()
    if "please active local before create profile" in text:
        return "local_not_activated"
    if "not found" in text or "does not exist" in text:
        return "not_found"
    if any(word in text for word in ("too many", "rate limit", "temporarily blocked")):
        return "rate_limited"
    if any(word in text for word in ("proxy", "network", "ip check")):
        return "proxy_failed"
    if any(word in text for word in ("opening", "closing", "busy", "running")):
        return "browser_busy"
    if code in {"unauthorized", "401"}:
        return "unauthorized"
    if code in {"forbidden", "403"}:
        return "forbidden"
    return code or "provider_rejected"


def translated_message(code: str, raw_message: Any, *, fallback: str) -> str:
    explanation = _ERROR_EXPLANATIONS.get(code, fallback)
    original = safe_text(raw_message)
    if not REDACT_CLIENT_LOGS and original and original not in explanation:
        return f"{explanation}；厂商原文：{original}"
    return explanation


def protocol_error(operation: str, message: str, response: JsonResponse | None = None) -> BrowserError:
    return BrowserError(
        provider="hidemium",
        operation=operation,
        code="invalid_response",
        status_code=response.status_code if response else None,
        details=response.diagnostics() if response else None,
        message=translated_message(
            "invalid_response",
            message,
            fallback="Hidemium 服务返回的内容无法识别",
        ),
    )


def response_error(
    response: JsonResponse,
    operation: str,
    payload: Mapping[str, Any],
) -> BrowserError:
    raw_code = payload.get("type") or payload.get("status")
    raw_message = (
        payload.get("title")
        or payload.get("message")
        or f"HTTP {response.status_code}"
    )
    code = normalized_code(raw_code, raw_message)
    retryable = (
        response.status_code >= 500
        or code in {"rate_limited", "proxy_failed", "browser_busy"}
    )
    return BrowserError(
        provider="hidemium",
        operation=operation,
        code=code,
        message=translated_message(
            code,
            raw_message,
            fallback="Hidemium 拒绝了本次操作",
        ),
        status_code=response.status_code,
        retryable=retryable,
        retry_after=2.0 if retryable else None,
        details=response.diagnostics(raw=safe_data(response.data)),
    )


__all__ = [
    "normalized_code",
    "protocol_error",
    "response_error",
    "safe_data",
    "safe_text",
    "translated_message",
]
