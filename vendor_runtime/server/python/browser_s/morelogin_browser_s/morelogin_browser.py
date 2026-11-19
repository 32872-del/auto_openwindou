"""MoreLogin 统一浏览器实现。"""

from __future__ import annotations

from copy import deepcopy
import threading
import time
from typing import Any, Mapping

from ..browser_module_s.browser_base import BrowserBase
from ..browser_module_s.browser_error import BrowserError
from ..browser_module_s.browser_http import JsonHttpClient
from server.python.browser_s.browser_info import (
    BrowserGroup,
    BrowserHealth,
    BrowserInfo,
    HealthState,
    BrowserCapabilities,
    StartedBrowserInfo,
)
from ..browser_module_s.browser_limit import BrowserLimits, BrowserUsage
from ..browser_task_setting import CreateBrowserSetting, StartBrowserSetting
from .morelogin_browser_api import MoreLoginBrowserApi
from .morelogin_browser_default import (
    MORELOGIN_BROWSER_KEY,
    MORELOGIN_BROWSER_NAME,
    MORELOGIN_BROWSER_PROCESS,
    MORELOGIN_CREATE_PATH,
    MORELOGIN_DELETE_PATH,
    MORELOGIN_DETAIL_PATH,
    MORELOGIN_GROUP_CREATE_PATH,
    MORELOGIN_GROUP_LIST_PATH,
    MORELOGIN_HEALTH_PATH,
    MORELOGIN_LIST_PATH,
    MORELOGIN_OS_IDS,
    MORELOGIN_START_PATH,
    MORELOGIN_STOP_PATH,
    MORELOGIN_UPDATE_PATH,
)
from .morelogin_browser_error import protocol_error, safe_data
from .morelogin_browser_runtime_setting import MoreLoginBrowserRuntimeSetting


class MoreLoginBrowser(BrowserBase):
    """MoreLogin 的参数转换和固定公开操作。"""

    key = MORELOGIN_BROWSER_KEY
    display_name = MORELOGIN_BROWSER_NAME
    process_name = MORELOGIN_BROWSER_PROCESS
    capabilities = BrowserCapabilities(
        android_fingerprint=True,
        groups=True,
    )
    # 启动接口没有一次性命令行参数字段，只能改环境的 startupParams。
    supports_start_flags = False

    def __init__(
        self,
        setting: MoreLoginBrowserRuntimeSetting,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.setting = setting
        self.api = MoreLoginBrowserApi(setting, http)
        self._groups: dict[str, str] = {}
        self._group_lock = threading.Lock()
        self._versions_lock = threading.Lock()
        self._versions_cache: tuple[int, ...] = ()
        self._versions_expiry = 0.0

    def available_core_versions(self) -> tuple[int, ...]:
        """使用本机客户端支持的 Chrome 内核；短缓存避免多线程重复查询。"""
        with self._versions_lock:
            if self._versions_cache and time.monotonic() < self._versions_expiry:
                return self._versions_cache
            data = self.api.request(
                "GET", MORELOGIN_HEALTH_PATH, None, operation="core_versions",
            )
            versions = set()
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, Mapping) or str(item.get("browserType")) != "1":
                        continue
                    values = item.get("versions")
                    if not isinstance(values, list):
                        continue
                    versions.update(
                        int(value) for value in values
                        if not isinstance(value, bool) and str(value).isascii()
                        and str(value).isdecimal() and int(value) > 0
                    )
            if not versions:
                raise protocol_error(
                    "core_versions", "未取得可用的 Chrome 内核版本，无法从最新三个版本随机选择；请检查 MoreLogin 本机接口，或手动填写内核版本",
                )
            self._versions_cache = tuple(sorted(versions, reverse=True))
            self._versions_expiry = time.monotonic() + 300
            return self._versions_cache

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        os_id = MORELOGIN_OS_IDS.get(setting.fingerprint.os)
        if not os_id:
            raise ValueError(
                f"MoreLogin 不支持 {setting.fingerprint.os} 指纹"
            )
        options = dict(setting.fingerprint.options)
        browser_core = options.pop("browser_core", None)
        advanced = options.pop("advanced", {})
        if not isinstance(advanced, Mapping):
            raise ValueError("MoreLogin advanced 必须是对象")
        resolution = advanced.get("resolution")
        if (isinstance(resolution, Mapping) and str(resolution.get("switcher")) == "2"
                and not str(resolution.get("id") or "").strip()):
            raise ValueError("MoreLogin 自定义分辨率缺少分辨率 ID；请填写 resolution.id，或使用 resolution.switcher=1 的默认分辨率。尚未发送创建请求")
        if options:
            raise ValueError(
                "MoreLogin 存在未支持的指纹参数: "
                + ", ".join(sorted(options))
            )

        body: dict[str, Any] = {
            "envName": setting.name,
            "browserTypeId": 1,
            "operatorSystemId": os_id,
        }
        if browser_core not in (None, "", 0, "0"):
            body["browserCore"] = int(browser_core)
        group_id = setting.metadata.get("group_id")
        if group_id in (None, "") and setting.group:
            group_id = self._group_id(setting.group, create=True)
        if group_id not in (None, ""):
            body["groupId"] = str(int(group_id))
        if remark := setting.metadata.get("remark"):
            body["envRemark"] = str(remark)
        if setting.proxy:
            proxy = setting.proxy
            body["proxy"] = {
                # Local API 使用字符串协议编号。proxyType 仅供 7/8 服务商选择子协议。
                "proxyProvider": {"http": "0", "https": "1", "socks5": "2", "ssh": "3"}[proxy.protocol],
                "proxyIp": proxy.host,
                "proxyPort": proxy.port,
                "username": proxy.username,
                "password": proxy.password,
            }
        data = self.api.request(
            "POST",
            MORELOGIN_CREATE_PATH,
            body,
            operation="create",
        )
        # 2026-09 Local API 的 data 直接返回环境 ID；兼容旧版 {envId: ...}。
        created_id = data.get("envId") if isinstance(data, Mapping) else data
        browser_id = str(created_id).strip() if isinstance(created_id, (str, int)) and not isinstance(created_id, bool) else ""
        if not browser_id or not browser_id.isascii() or not browser_id.isdecimal() or int(browser_id) <= 0:
            raise protocol_error("create", "成功响应缺少有效环境 ID（data 字符串或 data.envId）")
        if advanced:
            # 当前 Local API 不会给局部 advancedSetting 补齐所有指纹信息，会返回 19002。
            # 先让厂商生成相互匹配的 UA/屏幕/GPU，再保留这些字段并合并用户指定的设置。
            try:
                details = self.api.request(
                    "POST", MORELOGIN_DETAIL_PATH, {"envId": browser_id}, operation="get_created",
                )
                defaults = details.get("advancedSetting") if isinstance(details, Mapping) else None
                if not isinstance(defaults, Mapping) or not defaults:
                    raise protocol_error("get_created", "新建环境详情缺少完整 advancedSetting")
                merged = self._merge_advanced(defaults, advanced)
                self.api.request(
                    "POST", MORELOGIN_UPDATE_PATH,
                    {"envId": browser_id, "advancedSetting": merged}, operation="configure_created",
                )
            except Exception as exc:
                try:
                    self.delete_browser(browser_id, permanent=False)
                    cleanup = "本次新建环境已移入回收站"
                except Exception as cleanup_exc:
                    cleanup = f"本次新建环境回收失败：{type(cleanup_exc).__name__}: {cleanup_exc}"
                raise BrowserError(
                    provider=self.key, operation="create", code="created_profile_configuration_failed",
                    message=f"MoreLogin 环境 {browser_id} 已创建，但指纹配置未完成：{type(exc).__name__}: {exc}；{cleanup}",
                    retryable=False,
                    details={"environmentId": browser_id, "configurationError": str(exc), "cleanup": cleanup},
                ) from exc
        return BrowserInfo(
            self.key,
            browser_id,
            setting.name,
            group=setting.group,
            raw=safe_data(data if isinstance(data, Mapping) else {"envId": browser_id}),
        )

    @staticmethod
    def _merge_advanced(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
        merged = deepcopy(dict(defaults))
        for key, value in overrides.items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
                merged[key] = MoreLoginBrowser._merge_advanced(merged[key], value)
            else:
                merged[key] = deepcopy(value)
        return merged

    def start_browser(
        self,
        setting: StartBrowserSetting,
    ) -> StartedBrowserInfo:
        if setting.flags:
            raise ValueError(
                "MoreLogin 启动接口不支持临时 flags，"
                "请先用 update_browser 修改 startupParams"
            )
        data = self.api.request(
            "POST",
            MORELOGIN_START_PATH,
            {
                "envId": self._browser_id(setting.browser_id),
                "isHeadless": setting.headless,
            },
            operation="start",
            timeout=max(
                self.setting.start_timeout,
                setting.timeout_seconds,
            ),
        )
        if not isinstance(data, Mapping):
            raise protocol_error("start", "成功响应的 data 不是对象")
        try:
            debug_port = int(data.get("debugPort"))
        except (TypeError, ValueError) as exc:
            raise protocol_error(
                "start",
                "成功响应缺少有效 debugPort",
            ) from exc
        return StartedBrowserInfo(
            self.key,
            setting.browser_id,
            debug_port,
            f"http://127.0.0.1:{debug_port}",
            self.process_name,
            safe_data(data),
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        del force
        self.api.request(
            "POST",
            MORELOGIN_STOP_PATH,
            {"envId": self._browser_id(browser_id)},
            operation="stop",
        )

    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        self.api.request(
            "POST",
            MORELOGIN_DELETE_PATH,
            {
                "envIds": [self._browser_id(browser_id)],
                "removeEnvData": permanent,
            },
            operation="delete",
        )

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, Any],
    ) -> BrowserInfo:
        value = self._browser_id(browser_id)
        body = dict(values)
        if not body:
            raise ValueError("修改浏览器时 values 不能为空")
        if "name" in body and "envName" not in body:
            body["envName"] = body.pop("name")
        if "group" in body and "groupId" not in body:
            group_name = str(body.pop("group") or "").strip()
            if group_name:
                body["groupId"] = int(self._group_id(group_name, create=True))
        supplied_id = str(body.pop("envId", value))
        if supplied_id != value:
            raise ValueError("values.envId 与 browser_id 不一致")
        body["envId"] = int(value)
        self.api.request(
            "POST",
            MORELOGIN_UPDATE_PATH,
            body,
            operation="update",
        )
        return BrowserInfo(
            provider=self.key,
            browser_id=value,
            name=str(body.get("envName") or value),
            group=str(body.get("groupId") or ""),
            raw=safe_data(body),
        )

    def get_browser(self, browser_id: str) -> BrowserInfo:
        value = self._browser_id(browser_id)
        data = self.api.request(
            "POST",
            MORELOGIN_DETAIL_PATH,
            {"envId": int(value)},
            operation="get",
        )
        if not isinstance(data, Mapping):
            raise protocol_error("get", "成功响应的 data 不是对象")
        return self._browser_info(data, fallback_id=value)

    def list_browsers(
        self,
        *,
        group: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[BrowserInfo, ...]:
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if page_size < 1:
            raise ValueError("page_size 必须大于等于 1")
        body: dict[str, Any] = {
            "pageNo": page,
            "pageSize": page_size,
        }
        if group:
            group_id = self._group_id(group, create=False)
            if not group_id:
                return ()
            body["groupId"] = int(group_id)
        data = self.api.request(
            "POST",
            MORELOGIN_LIST_PATH,
            body,
            operation="list",
        )
        if not isinstance(data, Mapping):
            raise protocol_error("list", "成功响应的 data 不是对象")
        items = data.get("dataList")
        if not isinstance(items, list):
            raise protocol_error("list", "成功响应缺少 data.dataList")
        return tuple(
            self._browser_info(item)
            for item in items
            if isinstance(item, Mapping) and (item.get("envId") or item.get("id"))
        )

    def list_groups(self) -> tuple[BrowserGroup, ...]:
        data = self.api.request(
            "POST",
            MORELOGIN_GROUP_LIST_PATH,
            {"pageNo": 1, "pageSize": 2000, "groupName": ""},
            operation="list_groups",
        )
        if not isinstance(data, Mapping):
            raise protocol_error(
                "list_groups",
                "成功响应的 data 不是对象",
            )
        items = data.get("dataList")
        if not isinstance(items, list):
            raise protocol_error(
                "list_groups",
                "成功响应缺少 data.dataList",
            )
        groups = tuple(
            BrowserGroup(
                group_id=str(item.get("id") or ""),
                name=str(item.get("groupName") or ""),
                raw=safe_data(item),
            )
            for item in items
            if isinstance(item, Mapping)
            and item.get("id") not in (None, "")
            and item.get("groupName")
        )
        with self._group_lock:
            self._groups.update({
                item.name: item.group_id
                for item in groups
            })
        return groups

    def check_health(self) -> BrowserHealth:
        try:
            self.api.request(
                "GET",
                MORELOGIN_HEALTH_PATH,
                None,
                operation="health",
                timeout=min(self.setting.request_timeout, 3),
            )
            return BrowserHealth(
                self.key,
                HealthState.ONLINE,
                "MoreLogin 本机接口 在线",
                self.setting.local_api_url,
            )
        except BrowserError as exc:
            return BrowserHealth(
                self.key,
                HealthState.OFFLINE,
                exc.diagnostic_message(),
                self.setting.local_api_url,
            )

    def get_limits(self) -> BrowserLimits:
        return BrowserLimits.unavailable(
            "MoreLogin 官方 本机接口 未提供账号的创建、打开和请求上限"
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "MoreLogin 官方 本机接口 未提供账号当前用量"
        )

    def _group_id(self, name: str, *, create: bool) -> str:
        with self._group_lock:
            cached = self._groups.get(name)
        if cached:
            return cached
        groups = self.list_groups()
        found = next(
            (
                item.group_id
                for item in groups
                if item.name == name or item.group_id == name
            ),
            "",
        )
        if found or not create:
            return found
        data = self.api.request(
            "POST",
            MORELOGIN_GROUP_CREATE_PATH,
            {"groupName": name},
            operation="create_group",
        )
        if isinstance(data, Mapping):
            group_id = str(
                data.get("id")
                or data.get("groupId")
                or data.get("envGroupId")
                or ""
            )
        else:
            group_id = str(data or "")
        if not group_id or group_id == "0":
            raise protocol_error(
                "create_group",
                "成功响应缺少有效 data 分组 ID",
            )
        with self._group_lock:
            self._groups[name] = group_id
        return group_id

    @staticmethod
    def _browser_info(
        data: Mapping[str, Any],
        *,
        fallback_id: str = "",
    ) -> BrowserInfo:
        browser_id = str(
            data.get("envId")
            or data.get("id")
            or fallback_id
        ).strip()
        if not browser_id:
            raise protocol_error("read", "浏览器数据缺少 envId/id")
        return BrowserInfo(
            provider=MORELOGIN_BROWSER_KEY,
            browser_id=browser_id,
            name=str(data.get("envName") or browser_id),
            group=str(
                data.get("groupName")
                or data.get("groupId")
                or ""
            ),
            status=str(
                data.get("status")
                or data.get("localStatus")
                or ""
            ),
            raw=safe_data(data),
        )

    @staticmethod
    def _browser_id(browser_id: str) -> str:
        value = browser_id.strip()
        if not value:
            raise ValueError("browser_id 不能为空")
        try:
            int(value)
        except ValueError as exc:
            raise ValueError("MoreLogin browser_id 必须是数字") from exc
        return value


__all__ = ["MoreLoginBrowser"]
