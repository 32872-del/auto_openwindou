"""BitBrowser 错误翻译和脱敏。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import JsonResponse
from server.python.log_s.client_safe_log import REDACT_CLIENT_LOGS


_SENSITIVE_KEY = re.compile(
    r"(token|password|passwd|secret|authorization|cookie|username|proxy)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)(token|password|passwd|secret|authorization|cookie|username)"
    r"(\s*[:=]\s*)([^,\s;]+)"
)

_ERROR_EXPLANATIONS = {
    "invalid_parameters": "BitBrowser 请求参数无效",
    "unauthorized": "BitBrowser 登录信息不正确或已经失效",
    "forbidden": "BitBrowser 没有执行该操作的权限",
    "not_found": "BitBrowser 环境不存在",
    "rate_limited": "BitBrowser 请求过于频繁，请稍后重试",
    "connection_failed": "无法连接 BitBrowser 本机接口",
    "invalid_response": "BitBrowser 服务返回的内容无法识别",
    "proxy_failed": "BitBrowser 代理不可用或检测失败",
    "browser_busy": "BitBrowser 环境正在打开、关闭或处理中",
    "provider_rejected": "BitBrowser 拒绝了本次操作",
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
    code = str(raw_code or "")
    text = str(raw_message or "").lower()
    if code in {"401", "unauthorized"}:
        return "unauthorized"
    if code in {"403", "forbidden"}:
        return "forbidden"
    if code in {"404", "not_found"} or "not found" in text:
        return "not_found"
    if code in {"429", "rate_limited"} or "too frequent" in text:
        return "rate_limited"
    if any(word in text for word in ("proxy", "ip check", "network")):
        return "proxy_failed"
    if any(
        word in text
        for word in (
            "opening",
            "closing",
            "busy",
            "正在打开",
            "正在关闭",
            "处理中",
        )
    ):
        return "browser_busy"
    return code or "provider_rejected"


def translated_message(code: str, raw_message: Any, *, fallback: str) -> str:
    explanation = _ERROR_EXPLANATIONS.get(code, fallback)
    original = safe_text(raw_message)
    if not REDACT_CLIENT_LOGS and original and original not in explanation:
        return f"{explanation}；厂商原文：{original}"
    return explanation


def protocol_error(operation: str, message: str, response: JsonResponse | None = None) -> BrowserError:
    return BrowserError(
        provider="bitbrowser",
        operation=operation,
        code="invalid_response",
        status_code=response.status_code if response else None,
        details=response.diagnostics() if response else None,
        message=translated_message(
            "invalid_response",
            message,
            fallback="BitBrowser 服务返回的内容无法识别",
        ),
    )


def response_error(response: JsonResponse, operation: str) -> BrowserError:
    payload = response.data if isinstance(response.data, Mapping) else {}
    raw_code = payload.get("code") or f"http_{response.status_code}"
    raw_message = (
        payload.get("msg")
        or payload.get("message")
        or f"HTTP {response.status_code}"
    )
    code = normalized_code(raw_code, raw_message)
    # /browser/detail 删除后的实际响应没有错误码，仍返回 HTTP 200。
    # 仅精确详情的明确“无数据”可视为不存在，不吞掉连接或鉴权错误。
    if operation == 'get' and payload.get('success') is False and str(raw_message).strip() == '服务调用成功，但没有找到相应数据！':
        code = 'not_found'
    retryable = (
        response.status_code >= 500
        or code in {"rate_limited", "proxy_failed", "browser_busy"}
    )
    return BrowserError(
        provider="bitbrowser",
        operation=operation,
        code=code,
        message=translated_message(
            code,
            raw_message,
            fallback="BitBrowser 拒绝了本次操作",
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
