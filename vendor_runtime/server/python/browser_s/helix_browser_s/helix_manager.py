"""Helix Browser 3.x 管理器：/api/v2 配置，/api 浏览器生命周期。

与 helixwright 服务是两套接口；识别完成后固定后端，写请求不跨后端重试。
"""
from __future__ import annotations

from collections.abc import Mapping
import time
from urllib.parse import quote, urlencode

from ..browser_module_s.browser_base import BrowserBase
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import RequestsJsonHttpClient
from ..browser_info import BrowserCapabilities, BrowserGroup, BrowserHealth, BrowserInfo, HealthState, StartedBrowserInfo


def create_helix_backend(setting):
    http = RequestsJsonHttpClient()
    response = http.request("GET", setting.local_api_url + "/api/v2/client/full_version",
                            timeout=setting.request_timeout)
    if response.status_code == 200 and isinstance(response.data, dict) and response.data.get("success") is True:
        return HelixManagerBrowser(setting, http=http)
    if response.status_code == 404:
        from .helix_browser import HelixBrowser
        return HelixBrowser(setting)
    raise BrowserError(provider="helix", operation="detect", code="api_not_recognized",
                       message="Helix 服务接口识别失败，请核对本地 API 地址和版本",
                       status_code=response.status_code, details=response.diagnostics())


class HelixManagerBrowser(BrowserBase):
    key = "helix"
    display_name = "Helix Browser"
    process_name = "Helix.exe"
    capabilities = BrowserCapabilities(groups=True, force_close_browser=True)

    def __init__(self, setting, *, http=None):
        self.setting = setting
        self.http = http or RequestsJsonHttpClient()

    def _request(self, method, path, body=None, *, operation, timeout=None):
        try:
            response = self.http.request(method, self.setting.local_api_url + path, json=body,
                                         timeout=timeout or self.setting.request_timeout)
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(provider=self.key, operation=operation, code="transport_error",
                               message=f"Helix 请求 {method} {path} 失败：{type(exc).__name__}: {exc}",
                               retryable=True) from exc
        value = response.data
        if response.status_code >= 400 or (isinstance(value, dict) and (
                value.get("success") is False or value.get("error") or value.get("error_code"))):
            error = value.get("error") if isinstance(value, dict) else None
            code = str(error.get("code") or error.get("code_id")) if isinstance(error, dict) else str(
                value.get("error_code") or error or response.status_code) if isinstance(value, dict) else str(response.status_code)
            raise BrowserError(provider=self.key, operation=operation, code=code,
                               message=f"Helix {method} {path} 失败：{value}",
                               status_code=response.status_code, details=response.diagnostics(),
                               retryable=response.status_code >= 500)
        if isinstance(value, dict) and "success" in value:
            if value["success"] is not True:
                raise self._invalid(operation, "响应未确认成功")
            return value.get("data")
        return value

    def _invalid(self, operation, reason):
        return BrowserError(provider=self.key, operation=operation, code="invalid_response", message="Helix " + reason)

    @staticmethod
    def _path(browser_id):
        if not str(browser_id).strip():
            raise ValueError("Helix 环境编号不能为空")
        return "/api/v2/profiles/" + quote(str(browser_id), safe="")

    def _info(self, data):
        if not isinstance(data, Mapping) or not data.get("uuid"):
            raise self._invalid("profile", "没有返回环境 uuid")
        folder = data.get("folder") or ""
        if isinstance(folder, Mapping):
            folder = folder.get("uuid") or ""
        return BrowserInfo(self.key, str(data["uuid"]), str(data.get("title") or data.get("name") or data["uuid"]),
                           group=str(folder), status=str(data.get("status", "")))

    def create_browser(self, setting):
        fp = setting.fingerprint
        requested = {"os": fp.os, "os_arch": fp.architecture or "x86"}
        for key in ("os_version", "device_type", "device_model"):
            if getattr(fp, key):
                requested[key] = getattr(fp, key)
        payload = self._request("POST", "/api/v2/profiles/boilerplate",
                                requested, operation="fingerprint")
        if not isinstance(payload, dict) or not isinstance(payload.get("fp"), dict):
            raise self._invalid("create", "没有返回环境配置模板")
        body = {key: payload[key] for key in ("fp", "bookmarks", "local_cache") if key in payload}
        images = self._request("GET", "/api/v2/images", operation="profile_logo")
        logo = next((row.get("uuid") for row in images if row.get("default") and row.get("uuid")), None) if isinstance(images, list) else None
        if not logo:
            raise self._invalid("create", "没有返回默认环境图标")
        body.update(name=setting.name, description="", start_pages=["about:blank"],
                    launch_args=[], tags=list(setting.tags), folder=setting.group or None, logo=logo,
                    proxy={"type": "direct", "data": None})
        if fp.os_version:
            body["fp"]["os_version"] = fp.os_version
        if fp.device_type:
            body["fp"]["device_type"] = fp.device_type
        if fp.device_model:
            body["fp"]["device_model"] = fp.device_model
        aliases = {"cpu_cores": "cpu", "memory_gb": "ram", "resolution": "screen"}
        options = {aliases.get(key, key): value for key, value in fp.options.items()}
        if isinstance(options.get("noise"), bool):
            options["noise"] = {key: options["noise"] for key in body["fp"].get("noise", {})}
        supported = set(body["fp"]) - {"os", "os_arch"}
        unknown = set(options) - supported
        if unknown:
            raise ValueError("Helix 不支持这些指纹字段：" + "、".join(sorted(unknown)))
        body["fp"].update(options)
        if setting.proxy:
            proxy = setting.proxy
            body["proxy"] = {"type": "new", "data": {"type": proxy.protocol, "ip": proxy.host, "port": proxy.port,
                                        "login": proxy.username, "password": proxy.password}}
        return self._info(self._request("POST", "/api/v2/profiles", body, operation="create"))

    def start_browser(self, setting):
        data = self._request("POST", "/api/profiles/start", {"uuid": setting.browser_id,
            "headless": setting.headless, "debug_port": True, "timeout": setting.timeout_seconds,
            "flags": list(setting.flags)}, operation="start", timeout=setting.timeout_seconds)
        if not isinstance(data, dict) or not data.get("debug_port"):
            raise self._invalid("start", "启动响应缺少 debug_port")
        return StartedBrowserInfo(self.key, setting.browser_id, int(data["debug_port"]),
                                  str(data.get("ws_endpoint") or ""), self.process_name, raw=data)

    def _active(self):
        data = self._request("GET", "/api/profiles/active", operation="active")
        if not isinstance(data, list) or any(not isinstance(row, dict) or not row.get("uuid") for row in data):
            raise self._invalid("active", "运行环境列表格式不正确，不能确认环境已关闭")
        return {str(row["uuid"]) for row in data}

    def close_browser(self, browser_id, *, force=False):
        self._path(browser_id)
        if browser_id not in self._active():
            return
        self._request("POST", "/api/profiles/force_stop" if force else "/api/profiles/stop",
                      {"uuid": browser_id}, operation="stop", timeout=max(30, self.setting.request_timeout))
        deadline = time.monotonic() + 15
        while browser_id in self._active():
            if time.monotonic() >= deadline:
                raise BrowserError(provider=self.key, operation="stop", code="stop_unconfirmed",
                                   message=f"Helix 环境 {browser_id} 仍未退出，保留待清理记录", retryable=True)
            time.sleep(.25)

    def delete_browser(self, browser_id, *, permanent=True):
        self.close_browser(browser_id)
        try:
            self._request("POST", "/api/v2/profiles/delete", {"uuids": [browser_id]}, operation="delete")
        except BrowserError as exc:
            if exc.code != "profiles.not_found":
                raise
        if permanent:
            try:
                self._request("DELETE", "/api/v2/profiles/trash_bin", {"uuids": [browser_id]}, operation="delete_permanent")
            except BrowserError as exc:
                if exc.code != "profiles.not_found":
                    raise
        # 3.0.9 查询已删除 UUID 会返回笼统 HTTP 400；不能把它吞掉当成功。
        # 分页精确比较 UUID，不能按缓存名称过滤（用户可能在管理器改名）。
        if self._listed(browser_id) or (permanent and self._listed(browser_id, trash=True)):
            raise BrowserError(provider=self.key, operation="delete", code="delete_unconfirmed",
                               message=f"Helix 环境 {browser_id} 删除后仍可查询，保留待清理记录", retryable=True)

    def _listed(self, browser_id, *, trash=False):
        start = 0
        while True:
            params = {"start": start, "length": 100}
            data = self._request("GET" if trash else "POST",
                "/api/v2/profiles/trash_bin?" + urlencode(params) if trash else "/api/v2/profiles/list",
                None if trash else params, operation="delete_verify")
            rows = data.get("items" if trash else "profiles") if isinstance(data, dict) else None
            total = data.get("total") if isinstance(data, dict) else None
            if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
                raise self._invalid("delete_verify", "删除确认列表格式不正确")
            if any(str(row.get("uuid")) == browser_id for row in rows):
                return True
            start += len(rows)
            if start >= total:
                return False
            if not rows:
                raise self._invalid("delete_verify", "删除确认列表未完整返回")

    def get_browser(self, browser_id):
        try:
            return self._info(self._request("GET", self._path(browser_id), operation="get"))
        except BrowserError as exc:
            # 3.0.9 对已删除 UUID 返回 CA05/400；必须另查列表，不能将笼统错误当作已删除。
            if exc.status_code == 400 and exc.code == 'something_went_wrong':
                if not self._listed(browser_id) and not self._listed(browser_id, trash=True):
                    raise BrowserError(provider=self.key, operation='get', code='not_found',
                                       message='已核对环境列表和回收站，该环境不存在') from exc
            raise

    def update_browser(self, browser_id, values):
        unknown = set(values) - {"name", "description", "group", "tags", "start_pages", "launch_args"}
        if unknown or not values:
            raise ValueError("Helix 修改字段为空或不支持：" + "、".join(sorted(unknown)))
        original = self._request("GET", self._path(browser_id), operation="get")
        body = {key: original[key] for key in ("description", "start_pages", "launch_args", "fp",
                "storage_options", "local_cache", "bookmarks", "cookies", "extensions", "pinned_tag") if key in original}
        body.update(name=original.get("title") or original.get("name"), logo=original.get("image"),
                    fp=original.get("fingerprint") or original.get("fp"),
                    tags=[row.get("uuid") if isinstance(row, dict) else row for row in original.get("tags", [])],
                    folder=original.get("folder"), proxy={"type": "direct", "data": None})
        if isinstance(body["fp"], dict) and isinstance(body["fp"].get("data"), dict):
            body["fp"] = body["fp"]["data"]
        if original.get("proxy"):
            proxy = original["proxy"]
            body["proxy"] = {"type": "list", "data": proxy["uuid"]} if proxy.get("uuid") and not proxy.get("temp") else {"type": "new", "data": proxy}
        body.update({("folder" if key == "group" else key): value for key, value in values.items()})
        self._request("PATCH", self._path(browser_id), body, operation="update")
        return self.get_browser(browser_id)

    def list_browsers(self, *, group="", page=1, page_size=100):
        if page < 1 or page_size < 1:
            raise ValueError("页码和每页数量必须大于 0")
        body = {"start": (page - 1) * page_size, "length": page_size}
        if group:
            body["folders"] = [group]
        data = self._request("POST", "/api/v2/profiles/list", body, operation="list")
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
            raise self._invalid("list", "环境列表响应格式不正确")
        return tuple(self._info(row) for row in data["profiles"])

    def list_groups(self):
        data = self._request("GET", "/api/v2/folders", operation="groups")
        rows = data.get("items") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise self._invalid("groups", "分组响应格式不正确")
        return tuple(BrowserGroup(str(row["uuid"]), str(row.get("title") or row.get("name") or row["uuid"])) for row in rows)

    def check_health(self):
        try:
            version = self._request("GET", "/api/v2/client/full_version", operation="health")
            logged_in = self._request("GET", "/api/v2/auth/is_logged_in", operation="health")
            if logged_in is not True:
                return BrowserHealth(self.key, HealthState.MISCONFIGURED, "请先在 Helix 管理器登录", self.setting.local_api_url)
            return BrowserHealth(self.key, HealthState.ONLINE, "Helix 管理器已连接（Win32 + DP）", self.setting.local_api_url, str(version))
        except Exception as exc:
            return BrowserHealth(self.key, HealthState.OFFLINE, str(exc), self.setting.local_api_url)
