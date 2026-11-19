"""MoreLogin 错误翻译和脱敏。"""

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
    "invalid_parameters": "MoreLogin 请求参数无效",
    "unauthorized": "MoreLogin 客户端未登录或 API 未授权",
    "forbidden": "MoreLogin 账号没有执行该操作的权限",
    "not_found": "MoreLogin 环境不存在",
    "rate_limited": "MoreLogin 请求过于频繁，请稍后重试",
    "connection_failed": "无法连接 MoreLogin 本机接口",
    "invalid_response": "MoreLogin 服务返回的内容无法识别",
    "proxy_failed": "MoreLogin 代理不可用或检测失败",
    "browser_busy": "MoreLogin 环境正在启动、关闭或处理中",
    "open_limit_reached": "MoreLogin 浏览器打开数量已达上限",
    "environment_limit_reached": "MoreLogin 浏览器环境数量已达上限，请先清理不用的环境或扩容",
    "provider_rejected": "MoreLogin 拒绝了本次操作",
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
    if code == "19151":
        return "browser_busy"
    if code == '19039':
        return 'not_found'
    if code == "19063" or (
        "上限" in text
        and "环境" in text
        and any(word in text for word in ("创建", "个数", "数量"))
    ):
        return "environment_limit_reached"
    if any(word in text for word in ("not logged in", "login", "unauthorized")):
        return "unauthorized"
    if "not found" in text or "does not exist" in text:
        return "not_found"
    if "permission" in text or "forbidden" in text:
        return "forbidden"
    if any(word in text for word in ("too frequent", "rate limit")):
        return "rate_limited"
    if any(word in text for word in ("proxy", "network", "ip check")):
        return "proxy_failed"
    if any(word in text for word in ("opening", "closing", "busy", "running")):
        return "browser_busy"
    if (
        ("limit" in text and any(word in text for word in ("open", "browser")))
        or ("上限" in text and "打开" in text)
    ):
        return "open_limit_reached"
    return code or "provider_rejected"


def translated_message(code: str, raw_message: Any, *, fallback: str) -> str:
    explanation = _ERROR_EXPLANATIONS.get(code, fallback)
    original = safe_text(raw_message)
    if not REDACT_CLIENT_LOGS and original and original not in explanation:
        return f"{explanation}；厂商原文：{original}"
    return explanation


def protocol_error(operation: str, message: str, response: JsonResponse | None = None) -> BrowserError:
    return BrowserError(
        provider="morelogin",
        operation=operation,
        code="invalid_response",
        status_code=response.status_code if response else None,
        details=response.diagnostics() if response else None,
        message=translated_message(
            "invalid_response",
            message,
            fallback="MoreLogin 服务返回的内容无法识别",
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
    retryable = (
        response.status_code >= 500
        or code in {"rate_limited", "proxy_failed", "browser_busy"}
    )
    return BrowserError(
        provider="morelogin",
        operation=operation,
        code=code,
        message=translated_message(
            code,
            raw_message,
            fallback="MoreLogin 拒绝了本次操作",
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
