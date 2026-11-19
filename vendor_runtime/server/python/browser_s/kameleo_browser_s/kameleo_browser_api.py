"""Kameleo 5.x REST：直接返回对象/数组，健康检查允许空响应。"""
from ..browser_module_s.browser_http import RequestsJsonHttpClient
from ..browser_module_s.browser_error import BrowserError


class KameleoBrowserApi:
    def __init__(self, setting, http=None):
        self.setting = setting
        self.http = http or RequestsJsonHttpClient(allow_empty=True)

    def request(self, method, path, body=None, *, operation, timeout=None):
        try:
            response = self.http.request(method, self.setting.local_api_url + path,
                json=body, headers={"Accept": "application/json"},
                timeout=timeout or self.setting.request_timeout)
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError("kameleo", operation, "connection_failed",
                f"Kameleo 请求失败：{method} {self.setting.local_api_url}{path}；{type(exc).__name__}: {exc}", retryable=True) from exc
        if not 200 <= response.status_code < 300:
            data = response.data if isinstance(response.data, dict) else {}
            code = {401:"unauthorized", 402:"subscription_required", 403:"forbidden", 404:"not_found", 409:"state_conflict", 429:"rate_limited"}.get(response.status_code, "provider_rejected")
            raise BrowserError("kameleo", operation, code,
                str(data.get("detail") or data.get("title") or data or response.data),
                status_code=response.status_code, retryable=response.status_code in {429, 502, 503, 504},
                details=response.diagnostics())
        return response.data
