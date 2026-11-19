"""Kameleo 5.x 生命周期和本机 Chroma 连接；不把 Engine API 端口当作 CDP。"""
from __future__ import annotations
from pathlib import PureWindowsPath
import random
import time
from urllib.parse import urlencode
from uuid import UUID

from ..browser_module_s.browser_base import BrowserBase
from ..browser_module_s.browser_error import BrowserError
from ..browser_info import BrowserCapabilities, BrowserGroup, BrowserHealth, BrowserInfo, HealthState, StartedBrowserInfo
from .kameleo_browser_api import KameleoBrowserApi


_UPDATE_FIELDS = frozenset({"name", "folderId", "tags", "canvas", "webgl", "webglMeta", "audio", "timezone", "geolocation", "proxy", "webRtc", "fonts", "screen", "hardwareConcurrency", "deviceMemory", "language", "startPage", "passwordManager", "extensions", "notes", "storage"})


def profile_connection(browser_id, processes=None):
    """精确匹配根进程的 --kpp 环境目录；拒绝 renderer、相似 UUID 和多个候选。"""
    if processes is None:
        import psutil
        processes = psutil.process_iter(["pid", "name", "exe", "cmdline"])
    matches = []
    for process in processes:
        try:
            info = process if isinstance(process, dict) else process.info
            args = info.get("cmdline") or []
            if str(info.get("name", "")).casefold() not in {"chrome.exe", "chroma.exe"} or any(a.startswith("--type=") for a in args):
                continue
            profiles = [a.split("=", 1)[1].strip('"') for a in args if a.startswith("--kpp=")]
            if not any(PureWindowsPath(p).name.casefold() == browser_id.casefold() for p in profiles):
                continue
            ports = [a.split("=", 1)[1] for a in args if a.startswith("--remote-debugging-port=")]
            if len(ports) == 1 and ports[0].isdigit() and 1 <= int(ports[0]) <= 65535:
                matches.append({"debug_port": int(ports[0]), "process_id": info["pid"], "execute_path": info.get("exe", ""), "profile_path": profiles[0]})
        except (OSError, KeyError, TypeError, ValueError):
            continue
    if len(matches) > 1:
        raise BrowserError("kameleo", "start", "ambiguous_process", "同一 Kameleo 环境匹配到多个 Chromium 根进程，拒绝连接错误窗口")
    return matches[0] if matches else None


class KameleoBrowser(BrowserBase):
    key = "kameleo"
    display_name = "Kameleo"
    process_name = "chrome"
    capabilities = BrowserCapabilities(android_fingerprint=True, groups=True)

    def __init__(self, setting, http=None, *, connection_resolver=profile_connection):
        self.setting = setting
        self.api = KameleoBrowserApi(setting, http)
        self._connection_resolver = connection_resolver

    @staticmethod
    def _id(value):
        return str(UUID(str(value)))

    def _info(self, data):
        if not isinstance(data, dict) or not data.get("id"):
            raise BrowserError(self.key, "decode", "invalid_response", "Kameleo 环境响应缺少 id", details={"raw": data})
        status = data.get("status") or {}
        return BrowserInfo(self.key, str(data["id"]), str(data.get("name") or ""),
            str(data.get("folderId") or ""), str(status.get("lifetimeState") or "created") if isinstance(status, dict) else str(status), raw=data)

    def available_core_versions(self):
        kernels = self.api.request("GET", "/kernels", operation="kernels")
        return tuple(sorted({int(str(k["version"]).split(".")[0]) for k in kernels if k.get("browser") == "chroma" and not k.get("isMarkedForRemoval")}, reverse=True))

    def create_browser(self, setting):
        fp = setting.fingerprint
        options = dict(fp.options)
        metadata = dict(setting.metadata)
        metadata.pop("thread_index", None)  # 调度内部字段，不发送给厂商。
        fingerprint_id = str(options.pop("fingerprint_id", "") or "").strip()
        core = options.pop("core_version", None)
        product = options.pop("browser_product", "chrome")
        if product not in {"chrome", "edge"}:
            raise ValueError("当前 Kameleo 查询与 Win32 模式支持 Chroma（Chrome / Edge）；Junglefox 尚未接入")
        if not fingerprint_id:
            if core is None:
                versions = self.available_core_versions()[:3]
                if not versions:
                    raise BrowserError(self.key, "create", "no_kernel", "Kameleo 没有返回可用 Chroma 内核版本")
                core = random.choice(versions)
            query = urlencode({"deviceType":"mobile" if fp.os == "android" else "desktop", "osFamily":{"win":"windows", "mac":"macos"}.get(fp.os, fp.os), "browserProduct":product, "browserVersion":str(core)})
            candidates = self.api.request("GET", "/fingerprints?" + query, operation="fingerprints")
            if not isinstance(candidates, list) or not candidates:
                raise BrowserError(self.key, "create", "no_fingerprint", f"Kameleo 没有符合条件的指纹：{query}")
            # Windows 10/11 是用户明确选项；不能静默替换系统。
            if fp.os == "win" and fp.os_version:
                candidates = [v for v in candidates if str(v.get("os", {}).get("version", "")).split(".")[0] == fp.os_version.split(".")[0]]
                if not candidates:
                    raise BrowserError(self.key, "create", "no_fingerprint", f"本次 Kameleo 指纹列表没有 Windows {fp.os_version}，请调整系统或内核版本")
            fingerprint_id = random.choice(candidates)["id"]
        body = {"name":setting.name, "fingerprintId":fingerprint_id, "passwordManager":"disabled", "startPage":"about:blank"}
        tags = list(setting.tags)
        if setting.group and metadata.get("storage") == "cloud":
            groups = self.list_groups()
            matches = [g for g in groups if setting.group in {g.group_id, g.name}]
            if len(matches) != 1:
                raise ValueError(f"Kameleo 文件夹不存在或名称不唯一：{setting.group}；请先在浏览器软件创建文件夹")
            body["folderId"] = matches[0].group_id
        elif setting.group:
            # 官方只允许云端 profile 加文件夹；本地环境用同名 tag 归组。
            tags.insert(0, setting.group)
        if tags:
            body["tags"] = list(dict.fromkeys(tags))
        if setting.proxy:
            p = setting.proxy
            if p.protocol not in {"http", "socks5", "ssh"}:
                raise ValueError(f"Kameleo 不支持此代理类型：{p.protocol}")
            body["proxy"] = {"value":p.protocol, "extra":{"host":p.host, "port":p.port, "id":p.username, "secret":p.password}}
        for source, target in [("cpu_cores", "hardwareConcurrency"), ("memory_gb", "deviceMemory"), ("resolution", "screen")]:
            value = options.pop(source, None)
            if value is not None:
                body[target] = {"value":"manual", "extra":value}
        noise = options.pop("noise", None)
        if noise is not None:
            body.update(canvas="noise" if noise else "off", webgl="noise" if noise else "off", audio="noise" if noise else "off")
        allowed = _UPDATE_FIELDS - {"name", "folderId", "proxy", "tags"}
        if set(options) - allowed or set(metadata) - allowed:
            raise ValueError("Kameleo 不支持的创建参数：" + ", ".join(sorted((set(options) | set(metadata)) - allowed)))
        body.update(options)
        body.update(metadata)
        created = self._info(self.api.request("POST", "/profiles/new", body, operation="create", timeout=self.setting.start_timeout))
        return created

    def start_browser(self, setting):
        browser_id = self._id(setting.browser_id)
        profile = self.get_browser(browser_id)
        product = profile.raw.get("fingerprint", {}).get("browser", {}).get("product", "")
        if product not in {"chrome", "edge"}:
            raise BrowserError(self.key, "start", "unsupported_kernel", f"Kameleo 环境使用 {product or '未知'} 内核；当前只接入 Chroma（Chrome / Edge）")
        flags = list(setting.flags)
        if setting.headless and not any(flag.startswith("--headless") for flag in flags):
            flags.append("--headless=new")
        result = self.api.request("POST", f"/profiles/{browser_id}/start", {"arguments":flags}, operation="start", timeout=setting.timeout_seconds)
        deadline = time.monotonic() + 10
        connection = self._connection_resolver(browser_id)
        while connection is None and time.monotonic() < deadline:
            time.sleep(.2)
            connection = self._connection_resolver(browser_id)
        if connection is None:
            raise BrowserError(self.key, "start", "debug_connection_missing", "Kameleo 返回启动成功，但没有找到该环境的本机 Chroma 调试端口。请检查 Engine 与内核版本；没有连接其他窗口。", details={"profileId":browser_id, "response":result})
        port = connection["debug_port"]
        return StartedBrowserInfo(self.key, browser_id, port, f"http://127.0.0.1:{port}", self.process_name, raw={**connection,"response":result})

    def close_browser(self, browser_id, *, force=False):
        del force
        browser_id = self._id(browser_id)
        try:
            self.api.request("POST", f"/profiles/{browser_id}/stop", operation="stop", timeout=self.setting.start_timeout)
        except BrowserError as exc:
            if exc.status_code == 404:
                return
            if exc.status_code != 409:
                raise
            status = self.api.request("GET", f"/profiles/{browser_id}/status", operation="status")
            if status.get("lifetimeState") not in {"created", "terminated"}:
                raise

    def delete_browser(self, browser_id, *, permanent=True):
        del permanent  # 官方接口仅提供永久删除；保留由上层 delete_on_close 控制。
        try:
            self.api.request("DELETE", f"/profiles/{self._id(browser_id)}", operation="delete", timeout=self.setting.start_timeout)
        except BrowserError as exc:
            if exc.status_code != 404:
                raise

    def get_browser(self, browser_id):
        return self._info(self.api.request("GET", f"/profiles/{self._id(browser_id)}", operation="get"))

    def update_browser(self, browser_id, values):
        values = dict(values)
        if "note" in values:
            values["notes"] = values.pop("note")
        if not values or set(values) - _UPDATE_FIELDS:
            raise ValueError("Kameleo 修改参数为空或包含不支持字段：" + ", ".join(sorted(set(values) - _UPDATE_FIELDS)))
        self.api.request("PUT", f"/profiles/{self._id(browser_id)}", values, operation="update")
        return self.get_browser(browser_id)

    def list_browsers(self, *, group="", page=1, page_size=100):
        if page < 1 or page_size < 1:
            raise ValueError("页码和每页数量必须大于 0")
        rows = self.api.request("GET", "/profiles", operation="list")
        rows = [self._info(row) for row in rows]
        if group:
            ids = {g.group_id for g in self.list_groups() if group in {g.group_id, g.name}}
            rows = [r for r in rows if r.group in ids or group in r.raw.get("tags", [])]
        return tuple(rows[(page - 1)*page_size:page*page_size])

    def list_groups(self):
        payload = self.api.request("GET", "/folders", operation="groups")
        rows = payload.get("folders", []) if isinstance(payload, dict) else payload
        return tuple(BrowserGroup(str(row["id"]), str(row["name"]), raw=row) for row in rows)

    def check_health(self):
        try:
            self.api.request("GET", "/general/healthcheck", operation="health", timeout=3)
            return BrowserHealth(self.key, HealthState.ONLINE, "Kameleo Engine 已连接", self.setting.local_api_url)
        except BrowserError as exc:
            state = HealthState.MISCONFIGURED if exc.status_code in {401, 402, 403} else HealthState.OFFLINE
            return BrowserHealth(self.key, state, exc.diagnostic_message(), self.setting.local_api_url)
