"""Hubstudio 错误翻译、脱敏和重试信息解析。"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import JsonResponse
from server.python.log_s.client_safe_log import REDACT_CLIENT_LOGS


_SENSITIVE_KEY = re.compile(
    r"(token|password|passwd|secret|authorization|cookie|login|app[_-]?id)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)(token|password|passwd|secret|authorization|cookie|login|app[_-]?id)"
    r"(\s*[:=]\s*)([^,\s;]+)"
)

_PROVIDER_CODES = {
    "5": "proxy_initialization_failed",
    "7": "browser_start_failed",
    "17": "browser_busy",
    "21": "proxy_ip_timeout",
    "24": "open_detail_timeout",
    "26": "unsupported_plan",
    "27": "open_limit_reached",
    "-10004": "not_found",
    "-10005": "start_in_progress",
    "-10007": "core_unavailable",
    "-10008": "resource_exhausted",
    "-10011": "browser_unavailable",
    "-10014": "ipc_timeout",
    "-10015": "rate_limited",
    "-10018": "core_download_failed",
}

_RETRYABLE_PROVIDER_CODES = {
    "5",
    "7",
    "17",
    "21",
    "24",
    "-10005",
    "-10008",
    "-10014",
    "-10015",
    "-10018",
    "E010012",
    "E010013",
}

_ERROR_EXPLANATIONS = {
    "invalid_parameters": "Hubstudio 请求参数无效",
    "unauthorized": "Hubstudio 登录信息不正确或已经失效",
    "forbidden": "Hubstudio 账号没有执行该操作的权限",
    "not_found": "Hubstudio 环境不存在",
    "rate_limited": "Hubstudio 请求过于频繁，请稍后重试",
    "connection_failed": "无法连接 Hubstudio 本机接口",
    "invalid_response": "Hubstudio 服务返回的内容无法识别",
    "proxy_initialization_failed": "Hubstudio 代理初始化失败",
    "browser_start_failed": "Hubstudio 浏览器启动失败",
    "browser_busy": "Hubstudio 环境正在执行其他操作",
    "proxy_ip_timeout": "Hubstudio 代理 IP 检测超时",
    "open_detail_timeout": "Hubstudio 获取浏览器打开详情超时",
    "unsupported_plan": "当前 Hubstudio 套餐不支持该操作",
    "open_limit_reached": "Hubstudio 浏览器打开数量或次数已达上限",
    "start_in_progress": "Hubstudio 环境正在启动",
    "core_unavailable": "Hubstudio 所需浏览器内核不可用",
    "resource_exhausted": "Hubstudio 本机资源不足",
    "browser_unavailable": "Hubstudio 环境当前不可用",
    "ipc_timeout": "Hubstudio 客户端内部通信超时",
    "core_download_failed": "Hubstudio 浏览器内核下载失败",
    "provider_rejected": "Hubstudio 拒绝了本次操作",
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
    """把 Hub 本地 API 连接失败转换成包含端口的可操作提示。"""

    try:
        parsed = urlsplit(api_url)
        port: int | str = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        port = "未知"
    message = f"[Hub浏览器] 软件未打开、连接地址不可用或端口 {port} 未监听"
    return message if REDACT_CLIENT_LOGS else f"{message}；地址：{api_url}；原因：{type(error).__name__}: {error}"


def translated_message(code: str, raw_message: Any, *, fallback: str) -> str:
    """输出不包含请求细节的中文解释。"""

    explanation = _ERROR_EXPLANATIONS.get(code, fallback)
    original = safe_text(raw_message)
    if not REDACT_CLIENT_LOGS and original and original not in explanation:
        return f"{explanation}；厂商原文：{original}"
    return explanation


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value = next(
        (
            item
            for key, item in headers.items()
            if key.lower() == "retry-after"
        ),
        None,
    )
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def protocol_error(operation: str, message: str, response: JsonResponse | None = None) -> BrowserError:
    return BrowserError(
        provider="hub",
        operation=operation,
        code="invalid_response",
        status_code=response.status_code if response else None,
        details=response.diagnostics() if response else None,
        message=translated_message(
            "invalid_response",
            message,
            fallback="Hubstudio 服务返回的内容无法识别",
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
        payload.get("msg")
        or payload.get("message")
        or f"HTTP {response.status_code}"
    )
    return BrowserError(
        provider="hub",
        operation=operation,
        code=code,
        message=translated_message(
            code,
            raw_message,
            fallback=f"Hubstudio API HTTP {response.status_code}",
        ),
        status_code=response.status_code,
        retryable=response.status_code == 429 or response.status_code >= 500,
        retry_after=retry_after_seconds(response.headers),
        details=response.diagnostics(raw=safe_data(response.data)),
    )


def provider_error(
    response: JsonResponse,
    operation: str,
    payload: Mapping[str, Any],
) -> BrowserError:
    provider_code = str(payload.get("code") or "provider_rejected")
    raw_message = payload.get("msg") or payload.get("message") or "request rejected"
    code = _PROVIDER_CODES.get(provider_code, provider_code)
    if provider_code == "-1" and "版本内核" in str(raw_message):
        code = "core_unavailable"
    retryable = provider_code in _RETRYABLE_PROVIDER_CODES
    return BrowserError(
        provider="hub",
        operation=operation,
        code=code,
        message=translated_message(
            code,
            raw_message,
            fallback="Hubstudio 拒绝了本次操作",
        ),
        status_code=response.status_code,
        retryable=retryable,
        retry_after=(
            retry_after_seconds(response.headers)
            if retry_after_seconds(response.headers) is not None
            else (2.0 if retryable else None)
        ),
        details=response.diagnostics(provider_code=provider_code, raw=safe_data(payload)),
    )


__all__ = [
    "http_error",
    "local_connection_message",
    "protocol_error",
    "provider_error",
    "retry_after_seconds",
    "safe_data",
    "safe_text",
    "translated_message",
]
