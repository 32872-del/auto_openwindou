"""AdsPower 本机接口 请求和响应检查。"""

from __future__ import annotations

from threading import Lock
import time
from typing import Any, Mapping

from server.python.log_s import safe_client_text
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import (
    JsonHttpClient,
    JsonResponse,
    RequestsJsonHttpClient,
)
from .adspower_browser_error import protocol_error, response_error
from .adspower_browser_runtime_setting import AdsPowerBrowserRuntimeSetting


_RATE_LOCK = Lock()
_LAST_REQUEST_AT: dict[str, float] = {}
_LAST_SLOW_REQUEST_AT: dict[tuple[str, str], float] = {}
_GENERAL_INTERVAL_SECONDS = 0.55
_SLOW_INTERVAL_SECONDS = 1.05
_SLOW_PATHS = {
    "/api/v1/group/list",
    "/api/v2/browser-profile/list",
    "/api/v2/browser-profile/cookies",
    "/api/v2/browser-profile/ua",
}


class AdsPowerBrowserApi:
    """只处理 HTTP 和 AdsPower 响应，不承担业务参数转换。"""

    def __init__(
        self,
        setting: AdsPowerBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        # 真实 AdsPower 本机接口有明确的每秒请求限制；测试注入的 HTTP
        # 客户端不等待，避免拖慢离线契约测试。
        self._rate_limit_enabled = http is None
        self.http = http or RequestsJsonHttpClient()

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.setting.api_key:
            # 新版官方安全验证使用 Bearer；保留 api-key 兼容旧版无界面服务。
            headers["Authorization"] = f"Bearer {self.setting.api_key}"
            headers["api-key"] = self.setting.api_key
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        operation: str,
        timeout: float | None = None,
    ) -> Any:
        self._wait_for_rate_limit(path)
        try:
            response = self.http.request(
                method,
                f"{self.setting.local_api_url}{path}",
                json=body,
                headers=self.headers,
                timeout=timeout or self.setting.request_timeout,
            )
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(
                provider="adspower",
                operation=operation,
                code="connection_failed",
                message=safe_client_text(f"AdsPower 的 {operation} 请求失败（{self.setting.local_api_url}{path}）；原因：{safe_client_text(exc)}。请检查浏览器软件及API地址"),
                retryable=True,
            ) from exc
        return self.success_data(response, operation)

    def _wait_for_rate_limit(self, path: str) -> None:
        """按 AdsPower 官方频率限制串行节流，避免误报拒绝操作。"""

        if not self._rate_limit_enabled:
            return
        base = self.setting.local_api_url.rstrip("/")
        clean_path = "/" + str(path or "").split("?", 1)[0].lstrip("/")
        slow_key = (base, clean_path)
        with _RATE_LOCK:
            now = time.monotonic()
            wait_seconds = max(
                0.0,
                _LAST_REQUEST_AT.get(base, 0.0)
                + _GENERAL_INTERVAL_SECONDS
                - now,
            )
            if clean_path in _SLOW_PATHS:
                wait_seconds = max(
                    wait_seconds,
                    _LAST_SLOW_REQUEST_AT.get(slow_key, 0.0)
                    + _SLOW_INTERVAL_SECONDS
                    - now,
                )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            requested_at = time.monotonic()
            _LAST_REQUEST_AT[base] = requested_at
            if clean_path in _SLOW_PATHS:
                _LAST_SLOW_REQUEST_AT[slow_key] = requested_at

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
        # AdsPower 的关闭接口成功时只返回 code/msg，没有 data；需要数据的
        # 创建、查询和启动调用方会继续校验返回值的具体结构。
        return payload.get("data")


__all__ = ["AdsPowerBrowserApi"]
