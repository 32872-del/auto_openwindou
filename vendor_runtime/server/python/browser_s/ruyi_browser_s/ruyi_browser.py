"""ruyi（自定义 Firefox 指纹内核）的统一浏览器实现。"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from server.python.utils_s.path_utils import get_server_paths

from ..browser_task_setting import CreateBrowserSetting, StartBrowserSetting
from ..browser_module_s.browser_base import BrowserBase
from ..browser_module_s.browser_cache import (
    browser_profile_directory,
    prepare_local_browser_proxy,
)
from ..browser_module_s.browser_error import BrowserError
from server.python.browser_s.browser_info import (
    BrowserCapabilities,
    BrowserHealth,
    BrowserInfo,
    HealthState,
    StartedBrowserInfo,
)
from ..browser_module_s.browser_limit import BrowserLimits, BrowserUsage
from .ruyi_browser_default import (
    RUYI_BROWSER_KEY,
    RUYI_BROWSER_NAME,
    RUYI_BROWSER_PROCESS,
)
from .ruyi_browser_fingerprint import (
    assign_ruyi_fingerprint,
    list_ruyi_fingerprint_templates,
    prepare_ruyi_fingerprint,
)
from .ruyi_browser_probe import install_ruyi_core, probe_ruyi
from .ruyi_browser_runtime_setting import RuyiBrowserRuntimeSetting


class RuyiBrowser(BrowserBase):
    """管理 ruyi 数据目录，并把 FirefoxPage 交给 RuyiDriver。"""

    key = RUYI_BROWSER_KEY
    display_name = RUYI_BROWSER_NAME
    process_name = RUYI_BROWSER_PROCESS
    capabilities = BrowserCapabilities(groups=False)

    def __init__(self, setting: RuyiBrowserRuntimeSetting) -> None:
        self.setting = setting
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._pages: dict[str, object] = {}
        self._desktop_processes: dict[str, object] = {}

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        browser_id = f"ruyi-{uuid4().hex}"
        profile_dir = browser_profile_directory(
            self._profile_root(),
            browser_name=setting.name,
            browser_id=browser_id,
        )
        profile_dir.mkdir(parents=True, exist_ok=True)
        fingerprint_mode = str(
            setting.fingerprint.options.get("ruyi_fingerprint_mode") or "random"
        ).strip().lower()
        fingerprint_template_name = (
            str(
                setting.fingerprint.options.get("ruyi_fingerprint_template")
                or ""
            ).strip()
            if fingerprint_mode == "fixed"
            else ""
        )
        fingerprint_template = assign_ruyi_fingerprint(
            profile_dir,
            library_directory=self._fingerprint_root(),
            template_name=fingerprint_template_name,
        )
        with self._lock:
            self._records[browser_id] = {
                "name": setting.name,
                "group": setting.group,
                "profile_dir": profile_dir,
                "proxy": setting.proxy,
                "metadata": {
                    **dict(setting.metadata),
                    "fingerprint_template": fingerprint_template,
                },
            }
        return BrowserInfo(
            provider=self.key,
            browser_id=browser_id,
            name=setting.name,
            group=setting.group,
            status="created",
        )

    def start_browser(self, setting: StartBrowserSetting) -> StartedBrowserInfo:
        record = self._record(setting.browser_id)
        browser_path = self._ensure_browser_path()
        try:
            from ruyipage import FirefoxOptions, FirefoxPage
        except ImportError as exc:
            raise BrowserError(
                provider=self.key,
                operation="start",
                code="python_package_missing",
                message="服务端没有安装 ruyi 组件，请先安装 ruyiPage",
                retryable=False,
            ) from exc

        profile_dir = Path(record["profile_dir"])
        profile_dir.mkdir(parents=True, exist_ok=True)
        proxy = prepare_local_browser_proxy(
            record.get("proxy"),
            metadata=record.get("metadata"),
            provider=self.key,
            display_name=self.display_name,
        )
        owned_process = None
        try:
            options = FirefoxOptions().set_auto_port()
            options.set_browser_path(str(browser_path))
            prepare_ruyi_fingerprint(
                options,
                profile_dir=profile_dir,
                proxy=proxy,
                geo_timeout=self.setting.geo_timeout,
                geo_retries=self.setting.geo_retries,
                library_directory=self._fingerprint_root(),
            )
            options.set_profile(str(profile_dir))
            from ..desktop_launch_bridge import desktop_launch_coordinator
            from server.python.driver_s.win32_dp_module_s.win32_native import current_desktop_name
            desktop_name = current_desktop_name()
            if desktop_name.startswith("AutoDrission_") and desktop_launch_coordinator()._background_protocol_active():
                from .desktop_process import launch_on_desktop
                owned_process = launch_on_desktop(
                    options, desktop_name, setting.timeout_seconds,
                )
            page = FirefoxPage(options)
            port = self._page_port(page)
        except Exception as exc:
            if owned_process is not None:
                owned_process.close(timeout=0, terminate=True)
            if isinstance(exc, BrowserError):
                raise
            raise BrowserError(
                provider=self.key,
                operation="start",
                code="browser_start_failed",
                message=(
                    "ruyi 启动失败，请检查浏览器内核、数据目录和代理"
                ),
                retryable=True,
            ) from exc

        with self._lock:
            self._pages[setting.browser_id] = page
            if owned_process is not None:
                self._desktop_processes[setting.browser_id] = owned_process
        return StartedBrowserInfo(
            provider=self.key,
            browser_id=setting.browser_id,
            debug_port=port,
            cdp_url="",
            process_name=self.process_name,
            raw={"driver_source": page},
            driver_type="ruyi",
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        with self._lock:
            page = self._pages.get(browser_id)
            owned_process = self._desktop_processes.get(browser_id)
        close_error = None
        if page is not None:
            try:
                try:
                    page.quit(timeout=5, force=force)
                except TypeError:
                    # 兼容旧版 ruyipage 的无参数 quit()。
                    page.quit()
            except Exception as exc:
                close_error = exc
        if owned_process is not None:
            try:
                # SDK 断线不阻止回收自有进程；force 只作用于创建时保留的句柄。
                owned_process.close(timeout=5, terminate=force)
            except Exception as exc:
                close_error = exc
            else:
                close_error = None  # 进程退出已得到 Windows 确认。
        if close_error is not None:
            raise BrowserError(
                provider=self.key, operation="stop", code="browser_close_failed",
                message=f"ruyi 浏览器 {browser_id} 关闭失败，已保留清理记录：{close_error}",
                retryable=True,
            ) from close_error
        with self._lock:
            if self._pages.get(browser_id) is page:
                self._pages.pop(browser_id, None)
            if self._desktop_processes.get(browser_id) is owned_process:
                self._desktop_processes.pop(browser_id, None)

    def delete_browser(
        self,
        browser_id: str,
        *,
        permanent: bool = True,
    ) -> None:
        del permanent
        self.close_browser(browser_id)
        with self._lock:
            record = self._records.get(browser_id)
        if not record:
            return
        profile_dir = Path(record["profile_dir"]).resolve()
        root = self._profile_root().resolve()
        if profile_dir == root or not profile_dir.is_relative_to(root):
            raise BrowserError(
                provider=self.key, operation="delete", code="invalid_profile_path",
                message=f"环境 {browser_id} 的目录不在允许删除的范围内：{profile_dir}",
            )
        last_error: OSError | None = None
        for delay in (0.0, 0.2, 0.5, 1.0, 2.0):
            if delay:
                time.sleep(delay)
            try:
                shutil.rmtree(profile_dir)
                break
            except FileNotFoundError as exc:
                if not profile_dir.exists():
                    break
                last_error = exc
            except OSError as exc:
                last_error = exc
        else:
            raise BrowserError(
                provider=self.key, operation="delete", code="profile_in_use",
                message=f"ruyi 环境 {browser_id} 的目录删除失败，已保留以便重试：{profile_dir}",
                retryable=True,
            ) from last_error
        with self._lock:
            if self._records.get(browser_id) is record:
                self._records.pop(browser_id, None)

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, object],
    ) -> BrowserInfo:
        """修改执行端当前管理的 Ruyi 环境名称或分组。"""

        updates = dict(values)
        unknown = sorted(set(updates) - {"name", "group"})
        if unknown:
            raise ValueError("Ruyi 不支持修改这些字段：" + "、".join(unknown))
        if not updates:
            raise ValueError("修改浏览器时 values 不能为空")
        record = self._record(browser_id)
        with self._lock:
            if "name" in updates:
                name = str(updates["name"] or "").strip()
                if not name:
                    raise ValueError("浏览器名称不能为空")
                record["name"] = name
            if "group" in updates:
                record["group"] = str(updates["group"] or "").strip()
        return self.get_browser(browser_id)

    def get_browser(self, browser_id: str) -> BrowserInfo:
        record = self._record(browser_id)
        with self._lock:
            running = browser_id in self._pages
        return BrowserInfo(
            provider=self.key,
            browser_id=browser_id,
            name=str(record["name"]),
            group=str(record["group"]),
            status="running" if running else "created",
        )

    def list_browsers(
        self,
        *,
        group: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[BrowserInfo, ...]:
        if page < 1 or page_size < 1:
            raise ValueError("页码和每页数量必须大于 0")
        with self._lock:
            ids = tuple(self._records)
        items = tuple(self.get_browser(browser_id) for browser_id in ids)
        if group:
            items = tuple(item for item in items if item.group == group)
        start = (page - 1) * page_size
        return items[start:start + page_size]

    def check_health(self) -> BrowserHealth:
        probe = probe_ruyi(self.setting)
        if not probe.ready:
            return BrowserHealth(
                provider=self.key,
                state=HealthState.MISCONFIGURED,
                message=probe.message,
                version=probe.version,
            )
        try:
            fingerprint_count = len(
                list_ruyi_fingerprint_templates(self._fingerprint_root())
            )
        except BrowserError as exc:
            return BrowserHealth(
                provider=self.key,
                state=HealthState.MISCONFIGURED,
                message=exc.diagnostic_message(),
                version=probe.version,
            )
        return BrowserHealth(
            provider=self.key,
            state=HealthState.ONLINE,
            message=(
                f"ruyi 组件和浏览器内核都可以使用（{probe.source}），"
                f"已读取 {fingerprint_count} 套指纹参数"
            ),
            version=probe.version,
        )

    def get_limits(self) -> BrowserLimits:
        return BrowserLimits.unavailable(
            "ruyi 是本地浏览器，没有账号使用上限"
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "ruyi 是本地浏览器，没有账号用量"
        )

    def _record(self, browser_id: str) -> dict:
        value = browser_id.strip()
        with self._lock:
            record = self._records.get(value)
        if record is None:
            raise BrowserError(
                provider=self.key,
                operation="get",
                code="browser_not_found",
                message="没有找到这个 ruyi 浏览器",
                retryable=False,
            )
        return record

    def _browser_path(self) -> Path:
        return probe_ruyi(self.setting).browser_path or Path()

    def _profile_root(self) -> Path:
        return get_server_paths().resolve_resource(self.setting.profile_root)

    def _fingerprint_root(self) -> Path:
        return get_server_paths().resolve_resource(
            self.setting.fingerprint_directory
        )

    def _ensure_browser_path(self) -> Path:
        probe = probe_ruyi(self.setting)
        if probe.ready and probe.browser_path is not None:
            return probe.browser_path
        if not probe.package_ready:
            raise BrowserError(
                provider=self.key,
                operation="start",
                code="python_package_missing",
                message=probe.message,
                retryable=False,
            )
        if not self.setting.auto_download_core:
            raise self._missing_core_error()
        try:
            return install_ruyi_core(self.setting)
        except Exception as exc:
            raise BrowserError(
                provider=self.key,
                operation="install_core",
                code="browser_core_install_failed",
                message="ruyi 内核自动安装失败，请检查执行端网络后重试",
                retryable=True,
            ) from exc

    def _missing_core_error(self) -> BrowserError:
        return BrowserError(
            provider=self.key,
            operation="start",
            code="browser_core_missing",
            message="执行端没有找到 ruyi 内核，请先在执行端下载或选择内核",
            retryable=False,
        )

    @staticmethod
    def _page_port(page: object) -> int | None:
        browser = getattr(page, "browser", None)
        address = str(getattr(browser, "address", "") or "")
        if not address:
            return None
        try:
            return int(address.rsplit(":", 1)[-1])
        except ValueError:
            return None


__all__ = ["RuyiBrowser"]
