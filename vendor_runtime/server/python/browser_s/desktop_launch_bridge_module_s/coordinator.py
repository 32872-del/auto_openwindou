"""把可信管理器、单次环境启动和隔离 Desktop 组成一条生产链。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Callable, Iterator
from uuid import uuid4

from server.python.log_s import log

from .model import DesktopBridgeError
from .native_bridge import DesktopLaunchBridge
from .providers import (
    normalize_desktop_bridge_provider,
    provider_desktop_policy,
    provider_launcher_relative_paths,
    supports_desktop_launch_bridge,
)


_DESKTOP_ALL_ACCESS = 0x000F01FF


@dataclass(frozen=True, slots=True)
class DesktopBridgeProgram:
    provider: str
    manager_path: Path
    manager_sha256: str


@dataclass(slots=True)
class DesktopBridgeLease:
    provider: str
    browser_id: str
    desktop_name: str
    desktop_handle: int

    def close(self) -> bool:
        """只在 Windows 确认关闭后丢弃句柄；占用失败由协调器保留重试。"""
        handle = int(self.desktop_handle or 0)
        if handle and os.name == "nt":
            if not _user32().CloseDesktop(wintypes.HANDLE(handle)):
                log.warning(
                    f"隔离 Desktop 句柄暂未关闭，保留待回收；Windows 错误 {ctypes.get_last_error()}",
                    module="隔离桌面", operation="桌面回收",
                )
                return False
        self.desktop_handle = 0
        return True


class DesktopLaunchCoordinator:
    """管理器进程只注入一次；每个浏览器环境使用独立 Desktop。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._programs: dict[str, DesktopBridgeProgram] = {}
        self._program_errors: dict[str, str] = {}
        self._provider_locks: dict[str, threading.RLock] = {}
        self._bridges: dict[int, DesktopLaunchBridge] = {}
        self._leases: dict[tuple[str, str], DesktopBridgeLease] = {}
        self._retired_leases: list[DesktopBridgeLease] = []
        # 哪个线程当前绑在哪个 Desktop 上。退休的租约只有在没有任何活着的
        # 线程还站在那个 Desktop 上时才能关——见 _reap_unbound。
        self._thread_desktops: dict[int, str] = {}

    def configure(
        self,
        provider: str,
        *,
        manager_path: str | Path,
        manager_sha256: str,
    ) -> None:
        key = normalize_desktop_bridge_provider(provider)
        if not supports_desktop_launch_bridge(key):
            return
        path = Path(manager_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("浏览器管理器路径不是文件")
        digest = str(manager_sha256 or "").strip().lower()
        if len(digest) != 64:
            raise ValueError("浏览器管理器必须提供完整 SHA-256")
        program = DesktopBridgeProgram(key, path, digest)
        with self._lock:
            self._programs[key] = program
            self._program_errors.pop(key, None)
            self._provider_locks.setdefault(key, threading.RLock())

    def clear_provider(self, provider: str, *, reason: str = "") -> None:
        key = normalize_desktop_bridge_provider(provider)
        with self._lock:
            self._programs.pop(key, None)
            self._program_errors[key] = reason
        # 重新探测只影响后续启动，已有任务的 Desktop 仍由原任务释放。

    def enabled_for_current_task(self, provider: str) -> bool:
        key = normalize_desktop_bridge_provider(provider)
        if not self._background_protocol_active():
            return False
        ready, _ = self.background_isolation_status(key)
        return ready

    @staticmethod
    def _background_protocol_active() -> bool:
        try:
            from server.python.driver_s.drission_s import stealth

            protocol = stealth.active_task_input_protocol()
        except Exception:
            return False
        return bool(
            protocol
            and protocol[0] == "win32_dp_background"
            and protocol[1] == "background"
        )

    def background_isolation_status(self, provider: str) -> tuple[bool, str]:
        """返回后台多窗口协议能否为该厂商严格创建隔离 Desktop。"""

        key = normalize_desktop_bridge_provider(provider)
        if os.name != "nt":
            return False, "多窗口协议只支持 Windows 交互用户会话"
        if key not in {"chrome", "ruyi"} and not supports_desktop_launch_bridge(key):
            return False, "当前浏览器尚未接入隔离 Desktop 启动桥"
        if key in {"chrome", "ruyi"}:
            return True, "本地浏览器将显式创建于专属任务线程的隔离 Desktop"
        with self._lock:
            if key not in self._programs:
                if self._program_errors.get(key):
                    return False, f"{key}：{self._program_errors[key]}"
                return False, (
                    f"浏览器 {key} 的程序尚未确认，或更新后旧确认已失效。"
                    "请在执行端的浏览器设置中重新探测并保存确认"
                )
        return True, "隔离 Desktop 启动桥已就绪"

    def _require_background_isolation(self, provider: str) -> None:
        ready, reason = self.background_isolation_status(provider)
        if ready:
            return
        raise DesktopBridgeError(
            "DESKTOP_BRIDGE_REQUIRED",
            f"多窗口协议要求每个浏览器进入独立 Desktop：{reason}",
        )

    @contextmanager
    def start_scope(
        self,
        provider: str,
        browser_id: str,
    ) -> Iterator[DesktopBridgeLease | None]:
        key = normalize_desktop_bridge_provider(provider)
        if not self._background_protocol_active():
            yield None
            return
        self._require_background_isolation(key)

        if key in {"chrome", "ruyi"}:
            lease = self._new_lease(key, browser_id)
            completed = False
            try:
                self._bind_current_thread(lease)
                yield lease
                with self._lock:
                    old = self._leases.pop((key, browser_id), None)
                    self._leases[(key, browser_id)] = lease
                if old is not None and old is not lease:
                    self._retire_or_close(old)
                completed = True
                log.info(
                    f"[DesktopLaunchBridge] browser={browser_id[:16]} "
                    f"desktop={lease.desktop_name} outcome=direct-inherit",
                    operation="启动浏览器",
                )
            finally:
                if not completed:
                    self._retire_or_close(lease)
            return

        with self._lock:
            program = self._programs[key]
            provider_lock = self._provider_locks[key]
        with provider_lock:
            lease = self._new_lease(key, browser_id)
            bridges: list[DesktopLaunchBridge] = []
            completed = False
            try:
                self._bind_current_thread(lease)
                bridges = self._arm_running_managers(program, lease.desktop_name)
                before = {
                    bridge.manager_pid: bridge.status().successful_processes
                    for bridge in bridges
                }
                yield lease
                self._wait_for_routed_child(bridges, before)
                with self._lock:
                    old = self._leases.pop((key, browser_id), None)
                    self._leases[(key, browser_id)] = lease
                if old is not None and old is not lease:
                    self._retire_or_close(old)
                completed = True
                log.info(
                    f"[DesktopLaunchBridge] browser={browser_id[:16]} "
                    f"desktop={lease.desktop_name} outcome=routed",
                    operation="启动浏览器",
                )
            finally:
                for bridge in bridges:
                    bridge.disarm()
                if not completed:
                    self._retire_or_close(lease)

    def find_window(
        self,
        provider: str,
        browser_id: str,
        debug_port: int,
        *,
        timeout_seconds: float = 30.0,
        stop_requested: Callable[[], bool] | None = None,
    ) -> int | None:
        key = normalize_desktop_bridge_provider(provider)
        with self._lock:
            lease = self._leases.get((key, browser_id))
        if lease is None:
            return None
        from server.python.browser_s.browser_module_s.browser_window import (
            find_chrome_window_by_port,
        )
        from server.python.driver_s.win32_dp_module_s.win32_native import (
            window_desktop_name,
        )

        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            if stop_requested is not None and stop_requested():
                return None
            hwnd = find_chrome_window_by_port(
                int(debug_port),
                desktop_handle=lease.desktop_handle,
            )
            if hwnd:
                actual = window_desktop_name(hwnd)
                if actual and actual.casefold() == lease.desktop_name.casefold():
                    return int(hwnd)
                raise DesktopBridgeError(
                    "DESKTOP_BRIDGE_WINDOW_MISMATCH",
                    "浏览器窗口没有进入本次分配的隔离 Desktop",
                )
            time.sleep(0.1)
        # 超时了。原来只说一句「没找到对应窗口」——那台机器上每次都这样，
        # 现场完全不知道是「进程都没找到」「窗口还没画出来」还是「窗口在别的
        # 桌面上」，三种要改的地方完全不同。把看到的说出来。
        raise DesktopBridgeError(
            "DESKTOP_BRIDGE_WINDOW_NOT_FOUND",
            "浏览器进程已启动，但隔离 Desktop 中没有找到对应窗口"
            f"（等了 {timeout_seconds:.0f} 秒；"
            f"{self._window_hint(int(debug_port), lease)}）",
        )

    @staticmethod
    def _window_hint(debug_port: int, lease) -> str:
        """超时那一刻实际看到了什么，用来区分三种完全不同的原因。"""

        try:
            from server.python.browser_s.browser_module_s.browser_window import (
                browser_process_ids_by_port,
            )
        except Exception:  # noqa: BLE001
            return "拿不到进程信息"
        try:
            pids = browser_process_ids_by_port(debug_port)
        except Exception as exc:  # noqa: BLE001
            return f"查进程失败：{type(exc).__name__}"
        if not pids:
            # 端口上没有进程：浏览器可能已经退了，或者端口被别人占了。
            return f"端口 {debug_port} 上没有找到浏览器进程"
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            seen = {"total": 0, "small": 0}

            def visit(hwnd, _):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value in pids and user32.IsWindowVisible(hwnd):
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    seen["total"] += 1
                    if (rect.right - rect.left) <= 100                             or (rect.bottom - rect.top) <= 100:
                        seen["small"] += 1
                return True

            callback = ctypes.WINFUNCTYPE(
                ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(visit)
            user32.EnumDesktopWindows(
                wintypes.HANDLE(int(lease.desktop_handle)), callback, 0)
        except Exception as exc:  # noqa: BLE001
            return f"进程 {sorted(pids)} 在，但枚举窗口失败：{type(exc).__name__}"
        if seen["total"] == 0:
            return (f"进程 {sorted(pids)} 在，但这个桌面上它一个可见窗口都没有"
                    "——多半是还没画出来，可以把等待时间调长")
        return (f"进程 {sorted(pids)} 在，桌面上有 {seen['total']} 个可见窗口，"
                f"其中 {seen['small']} 个小于 100×100 被尺寸过滤掉了")

    def desktop_name(self, provider: str, browser_id: str) -> str | None:
        key = normalize_desktop_bridge_provider(provider)
        with self._lock:
            lease = self._leases.get((key, str(browser_id)))
            return lease.desktop_name if lease is not None else None

    def release(self, provider: str, browser_id: str) -> None:
        key = normalize_desktop_bridge_provider(provider)
        with self._lock:
            lease = self._leases.pop((key, str(browser_id)), None)
        if lease is not None:
            self._retire_or_close(lease)

    def _remember_binding(self, lease: DesktopBridgeLease) -> None:
        with self._lock:
            self._thread_desktops[threading.get_ident()] = lease.desktop_name

    def bound_desktop_names(self) -> set[str]:
        """当前还有活线程绑着的 Desktop。

        闲置守护靠它判断"这个 Desktop 还有没有人在用"——有窗口不算，正在跑的
        任务线程一定绑在自己那个 Desktop 上，所以绑定关系才是真正的在用。
        """

        alive = {thread.ident for thread in threading.enumerate()}
        with self._lock:
            names = {
                name for ident, name in self._thread_desktops.items()
                if ident in alive
            }
            # 还没退休的租约同样算在用：线程可能刚建好还没绑上去。
            names.update(lease.desktop_name for lease in self._leases.values())
        return {name for name in names if name}

    def _reap_unbound(self) -> int:
        """关掉已经没有任何活线程站在上面的退休 Desktop。"""

        alive = {thread.ident for thread in threading.enumerate()}
        with self._lock:
            self._thread_desktops = {
                ident: name
                for ident, name in self._thread_desktops.items()
                if ident in alive
            }
            occupied = {
                name.casefold()
                for name in self._thread_desktops.values()
            }
            keep: list[DesktopBridgeLease] = []
            release: list[DesktopBridgeLease] = []
            for lease in self._retired_leases:
                if lease.desktop_name.casefold() in occupied:
                    keep.append(lease)
                else:
                    release.append(lease)
            self._retired_leases = keep
        return sum(self._close_or_retain(lease) for lease in release)

    def reap_retired(self) -> int:
        """回收无人占用的退休句柄；一个任务退出不代表全进程线程均已退出。"""
        return self._reap_unbound()

    def _close_or_retain(self, lease: DesktopBridgeLease) -> bool:
        if lease.close() is False:
            self._queue_retired(lease)
            return False
        return True

    def _queue_retired(self, lease: DesktopBridgeLease) -> None:
        with self._lock:
            if not any(item is lease for item in self._retired_leases):
                self._retired_leases.append(lease)

    def close(self) -> None:
        with self._lock:
            leases = list(self._leases.values())
            leases.extend(self._retired_leases)
            bridges = list(self._bridges.values())
            self._leases.clear()
            self._retired_leases.clear()
            self._bridges.clear()
            self._programs.clear()
        for lease in leases:
            self._retire_or_close(lease)
        for bridge in bridges:
            bridge.close()

    def _retire_or_close(self, lease: DesktopBridgeLease) -> None:
        """任何受管活线程仍属于该 Desktop 时都延迟回收。"""

        try:
            from server.python.driver_s.win32_dp_module_s.win32_native import (
                current_desktop_name,
            )

            current = current_desktop_name()
        except Exception:
            current = ""
        occupied = {name.casefold() for name in self.bound_desktop_names()}
        if current:
            occupied.add(current.casefold())
        if lease.desktop_name.casefold() in occupied:
            self._queue_retired(lease)
            return
        self._close_or_retain(lease)

    def _bind_current_thread(self, lease: DesktopBridgeLease) -> None:
        """浏览器启动前把专属 worker 线程绑定到本槽位 Desktop。

        成功切换并记录新绑定后才回收旧桌面。切换前当前线程仍占用旧句柄，
        CloseDesktop 会失败；提前清零句柄会使它再也无法被正常回收。
        """

        if not _user32().SetThreadDesktop(wintypes.HANDLE(lease.desktop_handle)):
            error = ctypes.get_last_error()
            # 170 = ERROR_BUSY：线程名下还挂着窗口或钩子（典型是上传对话框那轮
            # COM/UIA 留下的隐藏消息窗口）。这些窗口都是本线程自己创建的，先把
            # 它们销毁再试一次；仍失败才真正报错。
            if error == 170 and _destroy_current_thread_windows():
                if _user32().SetThreadDesktop(wintypes.HANDLE(lease.desktop_handle)):
                    error = 0
                else:
                    error = ctypes.get_last_error()
            if error:
                raise DesktopBridgeError(
                    "DESKTOP_BRIDGE_THREAD_BIND_FAILED",
                    f"任务线程无法进入隔离 Desktop（Windows 错误 {error}）",
                )
        self._remember_binding(lease)
        from server.python.driver_s.win32_dp_module_s.win32_native import (
            current_desktop_name,
        )

        actual = current_desktop_name()
        if not actual or actual.casefold() != lease.desktop_name.casefold():
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_THREAD_MISMATCH",
                "任务线程没有进入本槽位分配的隔离 Desktop",
            )
        self._reap_unbound()

    def _new_lease(self, provider: str, browser_id: str) -> DesktopBridgeLease:
        identity = hashlib.sha256(
            f"{provider}\0{browser_id}\0{uuid4().hex}".encode("utf-8")
        ).hexdigest()[:20]
        from server.python.runtime_s.desktop_scope import desktop_prefix

        name = f"{desktop_prefix()}{provider}_{identity}"
        handle = _user32().CreateDesktopW(
            name,
            None,
            None,
            0,
            _DESKTOP_ALL_ACCESS,
            None,
        )
        if not handle:
            error = ctypes.get_last_error()
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_DESKTOP_CREATE_FAILED",
                f"无法创建隔离 Desktop（Windows 错误 {error}）",
            )
        return DesktopBridgeLease(provider, str(browser_id), name, int(handle))

    def _arm_running_managers(
        self,
        program: DesktopBridgeProgram,
        desktop_name: str,
    ) -> list[DesktopLaunchBridge]:
        launchers = _running_bridge_launchers(program)
        if not launchers:
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_MANAGER_NOT_RUNNING",
                "已确认的指纹浏览器管理器或厂商启动器没有运行",
            )
        policy = provider_desktop_policy(program.provider, desktop_name)
        armed: list[DesktopLaunchBridge] = []
        failures: list[str] = []
        for pid, launcher_path, launcher_sha256 in launchers:
            try:
                with self._lock:
                    bridge = self._bridges.get(pid)
                if bridge is None:
                    bridge = DesktopLaunchBridge(
                        manager_pid=pid,
                        manager_path=launcher_path,
                        manager_sha256=launcher_sha256,
                        policy=policy,
                    )
                    bridge.arm()
                    with self._lock:
                        self._bridges[pid] = bridge
                else:
                    bridge.update_policy(policy)
                armed.append(bridge)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or type(exc).__name__)
                failures.append(f"PID {pid}: {code}（{exc}）")
        if not armed:
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_NO_MANAGER_HOOKED",
                "浏览器管理器拒绝安装 Desktop Hook：" + "；".join(failures),
            )
        return armed

    @staticmethod
    def _wait_for_routed_child(
        bridges: list[DesktopLaunchBridge],
        before: dict[int, int],
        timeout_seconds: float = 5.0,
    ) -> None:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            for bridge in bridges:
                status = bridge.status()
                if status.successful_processes > before.get(bridge.manager_pid, 0):
                    return
                if status.failed_processes and status.last_error:
                    raise DesktopBridgeError(
                        "DESKTOP_BRIDGE_CHILD_ROUTE_FAILED",
                        f"浏览器子进程路由失败（Windows 错误 {status.last_error}）",
                    )
            time.sleep(0.05)
        diagnostics = []
        for bridge in bridges:
            status = bridge.status()
            diagnostics.append(
                f"PID {bridge.manager_pid}: hooks={status.hooks_patched}, "
                f"matched={status.matched_processes}, "
                f"success={status.successful_processes}, "
                f"last={status.last_executable or '-'}"
            )
        raise DesktopBridgeError(
            "DESKTOP_BRIDGE_CHILD_NOT_INTERCEPTED",
            "浏览器管理器返回成功，但没有拦截到对应 Chromium 根进程；"
            + "；".join(diagnostics),
        )


def _manager_process_ids(expected_path: Path) -> list[int]:
    import psutil

    expected = str(expected_path.resolve(strict=True)).casefold()
    found: list[int] = []
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            actual = str(process.info.get("exe") or process.exe()).casefold()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
        if actual == expected:
            found.append(int(process.pid))
    return sorted(set(found))


def _running_bridge_launchers(
    program: DesktopBridgeProgram,
) -> list[tuple[int, Path, str]]:
    """查找已确认管理器及厂商固定目录内的可信常驻启动器。"""

    candidates: list[tuple[Path, str]] = [
        (program.manager_path, program.manager_sha256),
    ]
    manager_root = program.manager_path.parent.resolve(strict=True)
    for relative_text in provider_launcher_relative_paths(program.provider):
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        candidate = (manager_root / relative).resolve(strict=False)
        try:
            candidate.relative_to(manager_root)
        except ValueError:
            continue
        if candidate.is_file():
            candidates.append((candidate, _sha256_file(candidate)))

    found: list[tuple[int, Path, str]] = []
    seen: set[int] = set()
    for candidate, digest in candidates:
        for pid in _manager_process_ids(candidate):
            if pid in seen:
                continue
            seen.add(pid)
            found.append((pid, candidate, digest))
    return sorted(found, key=lambda item: item[0])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_USER32 = None


def _destroy_current_thread_windows() -> bool:
    """销毁当前线程名下残留的窗口，返回是否确实清掉了东西。"""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumThreadWindows.argtypes = [wintypes.DWORD, enum_proc, wintypes.LPARAM]
    user32.EnumThreadWindows.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    handles: list[int] = []

    def collect(hwnd, _param):
        handles.append(int(hwnd))
        return True

    user32.EnumThreadWindows(kernel32.GetCurrentThreadId(), enum_proc(collect), 0)
    if not handles:
        return False
    destroyed: list[str] = []
    for hwnd in handles:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
        if user32.DestroyWindow(wintypes.HWND(hwnd)):
            destroyed.append(buffer.value or "?")
    log.warning(
        "任务线程进入隔离 Desktop 前清理了残留窗口："
        + "、".join(destroyed[:8])
        + (f" 等 {len(destroyed)} 个" if len(destroyed) > 8 else ""),
        module="隔离桌面",
        operation="线程绑定",
    )
    return bool(destroyed)


def _user32():
    global _USER32
    if os.name != "nt":
        raise DesktopBridgeError(
            "DESKTOP_BRIDGE_WINDOWS_REQUIRED",
            "隔离 Desktop 只支持 Windows",
        )
    if _USER32 is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateDesktopW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        user32.CreateDesktopW.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL
        user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        user32.SetThreadDesktop.restype = wintypes.BOOL
        _USER32 = user32
    return _USER32


_COORDINATOR = DesktopLaunchCoordinator()


def desktop_launch_coordinator() -> DesktopLaunchCoordinator:
    return _COORDINATOR


__all__ = [
    "DesktopBridgeLease",
    "DesktopBridgeProgram",
    "DesktopLaunchCoordinator",
    "desktop_launch_coordinator",
]
