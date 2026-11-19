"""本地普通 Chrome 的统一浏览器实现。"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
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
from server.python.driver_s.drission_s.options import (
    build_chromium_options,
)
from server.python.driver_s.drission_s.page import MyChromiumPage
from .chrome_browser_default import (
    CHROME_BROWSER_KEY,
    CHROME_BROWSER_NAME,
    CHROME_BROWSER_PROCESS,
)
from .chrome_browser_runtime_setting import ChromeBrowserRuntimeSetting
from .chrome_browser_probe import ensure_chrome_ini, probe_chrome
from .chrome_browser_connection import wait_debug_port as _wait_debug_port


class ChromeBrowser(BrowserBase):
    """本机 Chrome 的创建、启动、关闭和删除入口。"""

    key = CHROME_BROWSER_KEY
    display_name = CHROME_BROWSER_NAME
    process_name = CHROME_BROWSER_PROCESS
    capabilities = BrowserCapabilities(
        android_fingerprint=False,
        groups=False,
    )

    def __init__(self, setting: ChromeBrowserRuntimeSetting) -> None:
        self.setting = setting
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._pages: dict[str, MyChromiumPage] = {}
        self._process_handles: dict[str, int] = {}

    def create_browser(self, setting: CreateBrowserSetting) -> BrowserInfo:
        browser_id = f"chrome-{uuid4().hex}"
        profile_dir = browser_profile_directory(
            self._profile_root(),
            browser_name=setting.name,
            browser_id=browser_id,
        )
        with self._lock:
            self._records[browser_id] = {
                "name": setting.name,
                "group": setting.group,
                "profile_dir": profile_dir,
                "proxy": setting.proxy,
                "metadata": dict(setting.metadata),
                "mobile": setting.fingerprint.os == "android",
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
        proxy = prepare_local_browser_proxy(
            record.get("proxy"),
            metadata=record.get("metadata"),
            provider=self.key,
            display_name=self.display_name,
        )

        browser_path = self._browser_path()
        if not browser_path.is_file():
            raise self._missing_browser_error()

        profile_dir = Path(record["profile_dir"])
        profile_dir.mkdir(parents=True, exist_ok=True)
        process_handle = 0
        page = None
        try:
            ini_path = ensure_chrome_ini(self.setting.ini_path)
            options = build_chromium_options(
                str(ini_path) if ini_path is not None else None
            )
            options.set_browser_path(str(browser_path))
            options.set_local_port(self._free_port())
            options.set_user_data_path(str(profile_dir))
            options.headless(setting.headless)
            # 本地 Chrome 不经过厂商启动接口，启动参数只能在这里下发。
            # 过检测层要求的 --disable-backgrounding-occluded-windows 少了这一步
            # 就只在指纹浏览器上生效，本地 Chrome 多开时输入注入会失效。
            for flag in setting.flags:
                options.set_argument(flag)
            if proxy:
                options.set_proxy(
                    f"{proxy.protocol}://{proxy.host}:{proxy.port}"
                )
            desktop_name = _background_desktop_name()
            if desktop_name:
                process_handle, process_id = launch_chromium_on_desktop(
                    options,
                    desktop_name,
                    timeout_seconds=setting.timeout_seconds,
                )
                record["process_id"] = process_id
            page = MyChromiumPage(
                addr_or_opts=options,
                mobile=bool(record.get("mobile")),
            )
            port = self._page_port(page)
        except BrowserError:
            if page is not None:
                try:
                    page.quit()
                except Exception:
                    pass
            if process_handle:
                _terminate_process_handle(process_handle)
            raise
        except Exception as exc:
            if page is not None:
                try:
                    page.quit()
                except Exception:
                    pass
            if process_handle:
                _terminate_process_handle(process_handle)
            raise BrowserError(
                provider=self.key,
                operation="start",
                code="browser_start_failed",
                message=(
                    "本地浏览器启动失败，请检查浏览器路径、端口和数据目录"
                ),
                retryable=True,
            ) from exc

        with self._lock:
            self._pages[setting.browser_id] = page
            if process_handle:
                self._process_handles[setting.browser_id] = process_handle
        return StartedBrowserInfo(
            provider=self.key,
            browser_id=setting.browser_id,
            debug_port=port,
            cdp_url=f"http://127.0.0.1:{port}",
            process_name=self.process_name,
            raw={"driver_source": page},
            driver_type="dp",
        )

    def close_browser(self, browser_id: str, *, force: bool = False) -> None:
        del force
        with self._lock:
            page = self._pages.get(browser_id)
            process_handle = self._process_handles.get(browser_id, 0)
        try:
            if page is not None:
                page.quit()
        except Exception as exc:
            if process_handle:
                _terminate_process_handle(process_handle)
                with self._lock:
                    if self._process_handles.get(browser_id) == process_handle:
                        self._process_handles.pop(browser_id, None)
            # quit 失败不能提前丢失页面引用，下次清理仍须确认关闭。
            raise BrowserError(
                provider=self.key, operation="stop", code="browser_close_failed",
                message=f"本地浏览器 {browser_id} 关闭失败，已保留清理记录",
                retryable=True,
            ) from exc
        with self._lock:
            if self._pages.get(browser_id) is page:
                self._pages.pop(browser_id, None)
            if self._process_handles.get(browser_id) == process_handle:
                self._process_handles.pop(browser_id, None)
        if process_handle:
            _close_process_handle(process_handle)

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
        try:
            shutil.rmtree(profile_dir)
        except FileNotFoundError:
            if profile_dir.exists():
                raise BrowserError(
                    provider=self.key, operation="delete", code="profile_in_use",
                    message=f"环境 {browser_id} 目录尚未删除：{profile_dir}", retryable=True,
                )
        except OSError as exc:
            raise BrowserError(
                provider=self.key, operation="delete", code="profile_in_use",
                message=f"环境 {browser_id} 的目录删除失败，已保留以便重试：{profile_dir}",
                retryable=True,
            ) from exc
        with self._lock:
            if self._records.get(browser_id) is record:
                self._records.pop(browser_id, None)

    def update_browser(
        self,
        browser_id: str,
        values: Mapping[str, object],
    ) -> BrowserInfo:
        """修改本次执行端管理的本地环境名称或分组。"""

        updates = dict(values)
        unknown = sorted(set(updates) - {"name", "group"})
        if unknown:
            raise ValueError("本地 Chrome 不支持修改这些字段：" + "、".join(unknown))
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
        probe = probe_chrome(
            browser_path=self.setting.browser_path,
            ini_path=self.setting.ini_path,
        )
        if not probe.ready:
            return BrowserHealth(
                provider=self.key,
                state=HealthState.MISCONFIGURED,
                message=probe.message,
            )
        return BrowserHealth(
            provider=self.key,
            state=HealthState.ONLINE,
            message=f"本地 Chrome 可以使用（{probe.source}）",
            version=probe.browser_path.name if probe.browser_path else "",
        )

    def get_limits(self) -> BrowserLimits:
        return BrowserLimits.unavailable(
            "本地普通浏览器没有账号使用上限"
        )

    def get_usage(self) -> BrowserUsage:
        return BrowserUsage.unavailable(
            "本地普通浏览器没有账号用量"
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
                message="没有找到这个本地浏览器",
                retryable=False,
            )
        return record

    def _browser_path(self) -> Path:
        probe = probe_chrome(
            browser_path=self.setting.browser_path,
            ini_path=self.setting.ini_path,
        )
        return probe.browser_path or Path()

    def _profile_root(self) -> Path:
        return get_server_paths().resolve_resource(self.setting.profile_root)

    def _missing_browser_error(self) -> BrowserError:
        return BrowserError(
            provider=self.key,
            operation="start",
            code="browser_not_installed",
            message="没有找到本地 Chrome，请先安装或重新选择 chrome.exe",
            retryable=False,
        )

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            return int(server.getsockname()[1])

    @staticmethod
    def _page_port(page: MyChromiumPage) -> int:
        address = str(page.browser.address)
        try:
            port = int(address.rsplit(":", 1)[-1])
        except (TypeError, ValueError) as exc:
            raise BrowserError(
                provider=CHROME_BROWSER_KEY,
                operation="start",
                code="missing_debug_port",
                message="本地浏览器已经打开，但没有返回可连接端口",
                retryable=True,
            ) from exc
        return port


def _background_desktop_name() -> str:
    """多窗口协议下必须返回当前专属任务线程所属 Desktop。"""

    try:
        from server.python.driver_s.drission_s import stealth
        from server.python.driver_s.win32_dp_module_s.win32_native import (
            current_desktop_name,
        )

        protocol = stealth.active_task_input_protocol()
        if not protocol or protocol[0] != "win32_dp_background":
            return ""
        desktop_name = str(current_desktop_name() or "").strip()
    except Exception as exc:
        raise RuntimeError("无法确认多窗口协议的任务 Desktop") from exc
    if not desktop_name or not desktop_name.startswith("AutoDrission_"):
        raise RuntimeError("多窗口协议任务线程没有进入独立 Desktop")
    return desktop_name


def launch_chromium_on_desktop(
    options, desktop_name: str, *, timeout_seconds: float = 20.0
) -> tuple[int, int]:
    """用 STARTUPINFO.lpDesktop 显式创建 Chrome 根进程，再交给 DP 连接。"""

    if not desktop_name:
        raise ValueError("desktop_name 不能为空")
    try:
        import win32api
        import win32con
        import win32process
    except ImportError as exc:
        raise RuntimeError("普通 Chrome 隔离启动需要 pywin32") from exc

    browser_path = Path(str(options.browser_path)).expanduser().resolve(strict=True)
    port = int(str(options.address).rsplit(":", 1)[-1])
    arguments = [
        str(browser_path),
        *[str(value) for value in options.arguments],
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "about:blank",
    ]
    startup = win32process.STARTUPINFO()
    startup.lpDesktop = f"winsta0\\{desktop_name}"
    process_handle = 0
    thread_handle = 0
    try:
        process_handle, thread_handle, process_id, _ = win32process.CreateProcess(
            str(browser_path),
            subprocess.list2cmdline(arguments),
            None,
            None,
            False,
            win32con.CREATE_NEW_PROCESS_GROUP | win32con.CREATE_UNICODE_ENVIRONMENT,
            None,
            str(browser_path.parent),
            startup,
        )
        # PyHANDLE 析构会自动关闭；仅转成 int 会留下悬空句柄，之后该数字
        # 可能被监听 socket 等资源复用。把所有权明确交给浏览器会话清理。
        process_handle = process_handle.Detach()
        win32api.CloseHandle(thread_handle)
        thread_handle = 0
        _wait_debug_port(
            port,
            timeout_seconds=timeout_seconds,
            process_handle=int(process_handle),
            process_id=int(process_id),
        )
        # 原生进程已经由本模块创建。连接期间若它退出，DP 只能报连接失败，
        # 禁止再按默认路径静默创建另一窗口（可能落回主桌面）。
        options.existing_only(True)
        return int(process_handle), int(process_id)
    except Exception:
        if thread_handle:
            try:
                win32api.CloseHandle(thread_handle)
            except Exception:
                pass
        if process_handle:
            _terminate_process_handle(int(process_handle))
        raise


def _close_process_handle(process_handle: int) -> None:
    try:
        import win32api

        win32api.CloseHandle(int(process_handle))
    except Exception:
        pass


def _terminate_process_handle(process_handle: int) -> None:
    try:
        import win32process

        win32process.TerminateProcess(int(process_handle), 1)
    except Exception:
        pass
    finally:
        _close_process_handle(process_handle)


__all__ = ["ChromeBrowser"]
