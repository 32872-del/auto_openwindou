"""受控安装原生 CreateProcess Hook，并读取其 Desktop 路由状态。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import sys
import threading
import time
import shutil
import uuid

from .model import DesktopBridgeError, DesktopBridgePolicy, DesktopBridgeStatus


_BRIDGE_MAGIC = 0x31424C44
_BRIDGE_VERSION = 1
_MAX_EXECUTABLES = 16
_MAX_MARKERS = 16
_EXECUTABLE_CHARS = 64
_MARKER_CHARS = 128
_DESKTOP_CHARS = 128
_IMAGE_CHARS = 260
_MAPPING_PREFIX = "Local\\AutoDrissionDesktopBridge_"

_PAGE_READWRITE = 0x04
_FILE_MAP_ALL_ACCESS = 0x000F001F
_PROCESS_CREATE_THREAD = 0x0002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_OPERATION = 0x0008
_PROCESS_VM_READ = 0x0010
_PROCESS_VM_WRITE = 0x0020
_MEM_COMMIT = 0x1000
_MEM_RESERVE = 0x2000
_MEM_RELEASE = 0x8000
_PAGE_READWRITE_MEMORY = 0x04
_WAIT_OBJECT_0 = 0x00000000
_IMAGE_FILE_MACHINE_UNKNOWN = 0x0000
_IMAGE_FILE_MACHINE_AMD64 = 0x8664


class _NativeBridgeConfig(ctypes.Structure):
    _fields_ = [
        ("magic", wintypes.DWORD),
        ("version", wintypes.DWORD),
        ("target_pid", wintypes.DWORD),
        ("enabled", wintypes.LONG),
        ("fail_closed", wintypes.LONG),
        ("desktop_name", ctypes.c_wchar * _DESKTOP_CHARS),
        ("executable_count", wintypes.DWORD),
        ("marker_count", wintypes.DWORD),
        (
            "executable_names",
            (ctypes.c_wchar * _EXECUTABLE_CHARS) * _MAX_EXECUTABLES,
        ),
        ("command_markers", (ctypes.c_wchar * _MARKER_CHARS) * _MAX_MARKERS),
        ("installed", wintypes.LONG),
        ("modules_patched", wintypes.LONG),
        ("hooks_patched", wintypes.LONG),
        ("matched_processes", wintypes.LONG),
        ("successful_processes", wintypes.LONG),
        ("failed_processes", wintypes.LONG),
        ("last_child_pid", wintypes.DWORD),
        ("last_error", wintypes.DWORD),
        ("last_executable", ctypes.c_wchar * _IMAGE_CHARS),
    ]


class DesktopLaunchBridge:
    """只向已确认的 64 位厂商管理器安装固定原生 Hook。"""

    def __init__(
        self,
        *,
        manager_pid: int,
        manager_path: Path,
        manager_sha256: str,
        policy: DesktopBridgePolicy,
        native_library: Path | None = None,
        install_timeout_seconds: float = 8.0,
    ) -> None:
        if os.name != "nt":
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_WINDOWS_REQUIRED",
                "Desktop 启动桥只支持 Windows",
            )
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_X64_REQUIRED",
                "当前仅支持 64 位执行端与 64 位浏览器管理器",
            )
        self.manager_pid = int(manager_pid)
        if self.manager_pid <= 0:
            raise ValueError("manager_pid 必须大于 0")
        self.manager_path = Path(manager_path).expanduser().resolve(strict=True)
        self.manager_sha256 = str(manager_sha256 or "").strip().lower()
        if len(self.manager_sha256) != 64:
            raise ValueError("manager_sha256 必须是完整 SHA-256")
        self.policy = policy
        self.native_library = _runtime_native_library(_resolve_native_library(native_library))
        self.install_timeout_seconds = max(1.0, float(install_timeout_seconds))
        self._mapping_handle: int | None = None
        self._mapping_view: int | None = None
        self._config_pointer: ctypes.POINTER[_NativeBridgeConfig] | None = None
        self._process_handle: int | None = None
        self._lock = threading.RLock()

    def arm(self) -> DesktopBridgeStatus:
        """校验目标、写入共享策略并受控加载原生 Hook。"""

        with self._lock:
            if self._config_pointer is not None:
                self._write_policy(self.policy, enabled=True)
                return self.status()
            self._validate_manager_identity()
            process_handle = _open_manager_process(self.manager_pid)
            self._process_handle = process_handle
            try:
                _require_native_x64(process_handle)
                mapping_created = self._create_mapping()
                if not mapping_created:
                    existing = self.status()
                    if existing.installed and existing.hooks_patched > 0:
                        self._write_policy(self.policy, enabled=True)
                        return self.status()
                self._inject_library(process_handle)
                deadline = time.monotonic() + self.install_timeout_seconds
                while time.monotonic() < deadline:
                    status = self.status()
                    if status.installed and status.hooks_patched > 0:
                        return status
                    time.sleep(0.05)
                raise DesktopBridgeError(
                    "DESKTOP_BRIDGE_HOOK_TIMEOUT",
                    "原生 Hook 已加载，但没有在限定时间内完成安装",
                )
            except Exception:
                self.close()
                raise

    def update_policy(self, policy: DesktopBridgePolicy) -> DesktopBridgeStatus:
        """原子切换下一次 Chromium 根进程使用的 Desktop 路由。"""

        if policy.provider != self.policy.provider:
            raise ValueError("同一个管理器 Hook 不能切换浏览器厂商")
        with self._lock:
            self._validate_manager_identity()
            if self._config_pointer is None:
                self.policy = policy
                return self.arm()
            self.policy = policy
            self._write_policy(policy, enabled=True)
            return self.status()

    def disarm(self) -> None:
        """保留已安装 Hook，仅停止把新进程路由到隔离 Desktop。"""

        with self._lock:
            if self._config_pointer is not None:
                self._config_pointer.contents.enabled = 0

    def status(self) -> DesktopBridgeStatus:
        pointer = self._config_pointer
        if pointer is None:
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_NOT_ARMED",
                "Desktop 启动桥尚未安装",
            )
        config = pointer.contents
        return DesktopBridgeStatus(
            manager_pid=int(config.target_pid),
            desktop_name=str(config.desktop_name),
            enabled=bool(config.enabled),
            installed=bool(config.installed),
            modules_patched=max(0, int(config.modules_patched)),
            hooks_patched=max(0, int(config.hooks_patched)),
            matched_processes=max(0, int(config.matched_processes)),
            successful_processes=max(0, int(config.successful_processes)),
            failed_processes=max(0, int(config.failed_processes)),
            last_child_pid=max(0, int(config.last_child_pid)),
            last_error=max(0, int(config.last_error)),
            last_executable=str(config.last_executable),
        )

    def close(self) -> None:
        """禁用路由并关闭本端句柄；DLL 随管理器进程退出而卸载。"""

        with self._lock:
            if self._config_pointer is not None:
                self._config_pointer.contents.enabled = 0
            kernel32 = _kernel32()
            if self._mapping_view:
                kernel32.UnmapViewOfFile(ctypes.c_void_p(self._mapping_view))
            if self._mapping_handle:
                kernel32.CloseHandle(wintypes.HANDLE(self._mapping_handle))
            if self._process_handle:
                kernel32.CloseHandle(wintypes.HANDLE(self._process_handle))
            self._mapping_view = None
            self._mapping_handle = None
            self._process_handle = None
            self._config_pointer = None

    def __enter__(self) -> "DesktopLaunchBridge":
        self.arm()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _validate_manager_identity(self) -> None:
        actual_path = _query_process_path(self.manager_pid)
        if actual_path.casefold() != str(self.manager_path).casefold():
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_MANAGER_PATH_MISMATCH",
                "目标进程与已确认的浏览器管理器路径不一致",
            )
        actual_hash = _sha256(self.manager_path)
        if actual_hash != self.manager_sha256:
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_MANAGER_HASH_MISMATCH",
                "浏览器管理器文件已经变化，请重新探测并确认",
            )

    def _create_mapping(self) -> bool:
        kernel32 = _kernel32()
        mapping_name = f"{_MAPPING_PREFIX}{self.manager_pid}"
        handle = kernel32.CreateFileMappingW(
            wintypes.HANDLE(-1),
            None,
            _PAGE_READWRITE,
            0,
            ctypes.sizeof(_NativeBridgeConfig),
            mapping_name,
        )
        if not handle:
            _raise_last_error("DESKTOP_BRIDGE_MAPPING_CREATE_FAILED", "无法创建 Hook 配置")
        already_exists = ctypes.get_last_error() == 183
        view = kernel32.MapViewOfFile(
            handle,
            _FILE_MAP_ALL_ACCESS,
            0,
            0,
            ctypes.sizeof(_NativeBridgeConfig),
        )
        if not view:
            kernel32.CloseHandle(handle)
            _raise_last_error("DESKTOP_BRIDGE_MAPPING_VIEW_FAILED", "无法写入 Hook 配置")
        self._mapping_handle = int(handle)
        self._mapping_view = int(view)
        pointer = ctypes.cast(view, ctypes.POINTER(_NativeBridgeConfig))
        self._config_pointer = pointer
        config = pointer.contents
        if already_exists:
            if (
                int(config.magic) != _BRIDGE_MAGIC
                or int(config.version) != _BRIDGE_VERSION
                or int(config.target_pid) != self.manager_pid
            ):
                raise DesktopBridgeError(
                    "DESKTOP_BRIDGE_MAPPING_MISMATCH",
                    "管理器内已有不兼容的 Desktop Hook 配置",
                )
            return False
        ctypes.memset(view, 0, ctypes.sizeof(_NativeBridgeConfig))
        config.magic = _BRIDGE_MAGIC
        config.version = _BRIDGE_VERSION
        config.target_pid = self.manager_pid
        config.enabled = 1
        config.fail_closed = int(self.policy.fail_closed)
        config.desktop_name = self.policy.desktop_name
        config.executable_count = len(self.policy.executable_names)
        config.marker_count = len(self.policy.command_markers)
        for index, value in enumerate(self.policy.executable_names):
            config.executable_names[index].value = value[: _EXECUTABLE_CHARS - 1]
        for index, value in enumerate(self.policy.command_markers):
            config.command_markers[index].value = value[: _MARKER_CHARS - 1]
        return True

    def _write_policy(
        self,
        policy: DesktopBridgePolicy,
        *,
        enabled: bool,
    ) -> None:
        pointer = self._config_pointer
        if pointer is None:
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_NOT_ARMED",
                "Desktop 启动桥尚未安装",
            )
        config = pointer.contents
        # 先关闭路由，再整体更新规则，避免厂商启动线程读到半写入策略。
        config.enabled = 0
        config.fail_closed = int(policy.fail_closed)
        config.desktop_name = policy.desktop_name
        config.executable_count = len(policy.executable_names)
        config.marker_count = len(policy.command_markers)
        for index in range(_MAX_EXECUTABLES):
            config.executable_names[index].value = ""
        for index in range(_MAX_MARKERS):
            config.command_markers[index].value = ""
        for index, value in enumerate(policy.executable_names):
            config.executable_names[index].value = value[: _EXECUTABLE_CHARS - 1]
        for index, value in enumerate(policy.command_markers):
            config.command_markers[index].value = value[: _MARKER_CHARS - 1]
        _kernel32().FlushProcessWriteBuffers()
        config.enabled = int(enabled)

    def _inject_library(self, process_handle: int) -> None:
        kernel32 = _kernel32()
        dll_text = str(self.native_library)
        payload = dll_text.encode("utf-16-le") + b"\x00\x00"
        payload_buffer = ctypes.create_string_buffer(payload)
        remote_memory = kernel32.VirtualAllocEx(
            process_handle,
            None,
            len(payload),
            _MEM_COMMIT | _MEM_RESERVE,
            _PAGE_READWRITE_MEMORY,
        )
        if not remote_memory:
            _raise_last_error("DESKTOP_BRIDGE_REMOTE_ALLOC_FAILED", "无法准备原生 Hook")
        remote_thread = None
        try:
            written = ctypes.c_size_t()
            if not kernel32.WriteProcessMemory(
                process_handle,
                remote_memory,
                payload_buffer,
                len(payload),
                ctypes.byref(written),
            ) or int(written.value) != len(payload):
                _raise_last_error("DESKTOP_BRIDGE_REMOTE_WRITE_FAILED", "无法写入原生 Hook")
            kernel_module = kernel32.GetModuleHandleW("kernel32.dll")
            load_library = kernel32.GetProcAddress(kernel_module, b"LoadLibraryW")
            if not load_library:
                _raise_last_error("DESKTOP_BRIDGE_LOADLIBRARY_MISSING", "系统缺少 LoadLibraryW")
            thread_id = wintypes.DWORD()
            remote_thread = kernel32.CreateRemoteThread(
                process_handle,
                None,
                0,
                load_library,
                remote_memory,
                0,
                ctypes.byref(thread_id),
            )
            if not remote_thread:
                _raise_last_error("DESKTOP_BRIDGE_INJECTION_FAILED", "浏览器管理器拒绝加载原生 Hook")
            wait_result = kernel32.WaitForSingleObject(remote_thread, 10_000)
            if wait_result != _WAIT_OBJECT_0:
                raise DesktopBridgeError(
                    "DESKTOP_BRIDGE_INJECTION_TIMEOUT",
                    "浏览器管理器加载原生 Hook 超时",
                )
            module_result = wintypes.DWORD()
            if not kernel32.GetExitCodeThread(remote_thread, ctypes.byref(module_result)):
                _raise_last_error("DESKTOP_BRIDGE_INJECTION_STATUS_FAILED", "无法确认 Hook 状态")
            if not module_result.value:
                native_error = 0
                if self._config_pointer is not None:
                    native_error = int(self._config_pointer.contents.last_error)
                raise DesktopBridgeError(
                    "DESKTOP_BRIDGE_LIBRARY_REJECTED",
                    f"浏览器管理器没有接受原生 Hook（初始化码 {native_error}）",
                )
        finally:
            if remote_thread:
                kernel32.CloseHandle(remote_thread)
            kernel32.VirtualFreeEx(process_handle, remote_memory, 0, _MEM_RELEASE)


@lru_cache(maxsize=1)
def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CreateFileMappingW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateFileMappingW.restype = wintypes.HANDLE
    kernel32.MapViewOfFile.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p
    kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    kernel32.UnmapViewOfFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    kernel32.VirtualFreeEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        wintypes.DWORD,
    ]
    kernel32.VirtualFreeEx.restype = wintypes.BOOL
    kernel32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.WriteProcessMemory.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]
    kernel32.GetProcAddress.restype = ctypes.c_void_p
    kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeThread.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeThread.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.FlushProcessWriteBuffers.argtypes = []
    kernel32.FlushProcessWriteBuffers.restype = None
    if hasattr(kernel32, "IsWow64Process"):
        kernel32.IsWow64Process.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel32.IsWow64Process.restype = wintypes.BOOL
    if hasattr(kernel32, "IsWow64Process2"):
        kernel32.IsWow64Process2.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.USHORT),
            ctypes.POINTER(wintypes.USHORT),
        ]
        kernel32.IsWow64Process2.restype = wintypes.BOOL
    return kernel32


def _open_manager_process(pid: int) -> int:
    rights = (
        _PROCESS_CREATE_THREAD
        | _PROCESS_QUERY_LIMITED_INFORMATION
        | _PROCESS_VM_OPERATION
        | _PROCESS_VM_READ
        | _PROCESS_VM_WRITE
    )
    handle = _kernel32().OpenProcess(rights, False, int(pid))
    if not handle:
        _raise_last_error(
            "DESKTOP_BRIDGE_MANAGER_OPEN_FAILED",
            "无法打开已确认的浏览器管理器进程",
        )
    return int(handle)


def _query_process_path(pid: int) -> str:
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        _raise_last_error(
            "DESKTOP_BRIDGE_MANAGER_QUERY_FAILED",
            "无法读取浏览器管理器进程信息",
        )
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            _raise_last_error(
                "DESKTOP_BRIDGE_MANAGER_QUERY_FAILED",
                "无法读取浏览器管理器进程信息",
            )
        return str(Path(buffer.value).resolve())
    finally:
        kernel32.CloseHandle(handle)


def _require_native_x64(process_handle: int) -> None:
    kernel32 = _kernel32()
    is_wow64_process2 = getattr(kernel32, "IsWow64Process2", None)
    if is_wow64_process2 is None:
        target_wow64 = wintypes.BOOL()
        current_wow64 = wintypes.BOOL()
        if not kernel32.IsWow64Process(process_handle, ctypes.byref(target_wow64)):
            _raise_last_error("DESKTOP_BRIDGE_BITNESS_QUERY_FAILED", "无法确认管理器位数")
        if not kernel32.IsWow64Process(
            kernel32.GetCurrentProcess(),
            ctypes.byref(current_wow64),
        ):
            _raise_last_error("DESKTOP_BRIDGE_BITNESS_QUERY_FAILED", "无法确认执行端位数")
        if bool(target_wow64.value) != bool(current_wow64.value):
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_BITNESS_MISMATCH",
                "执行端与浏览器管理器位数不一致",
            )
        return
    process_machine = wintypes.USHORT()
    native_machine = wintypes.USHORT()
    if not is_wow64_process2(
        process_handle,
        ctypes.byref(process_machine),
        ctypes.byref(native_machine),
    ):
        _raise_last_error("DESKTOP_BRIDGE_BITNESS_QUERY_FAILED", "无法确认管理器位数")
    if (
        process_machine.value != _IMAGE_FILE_MACHINE_UNKNOWN
        or native_machine.value != _IMAGE_FILE_MACHINE_AMD64
    ):
        raise DesktopBridgeError(
            "DESKTOP_BRIDGE_X64_TARGET_REQUIRED",
            "当前原生 Hook 只支持 64 位浏览器管理器",
        )


_NATIVE_LIBRARY_NAME = "desktop_launch_hook.dll"


def _native_library_candidates() -> tuple[Path, ...]:
    """按优先级列出原生 Hook 可能在的位置。

    原来这里用 `getattr(sys, "frozen", False)` 二选一，而 **Nuitka 不设置
    `sys.frozen`**（`utils_s/path_utils.py` 里早就写着这句注释，这里漏了）。
    于是成品跑的是源码分支，去找一个成品里根本没有的 `native/bin/x64/`，
    `resolve(strict=True)` 当场抛 FileNotFoundError——每个浏览器都起不来，
    而源码模式一切正常，所以只有部署之后才炸。

    改成逐个候选去找，不再赌某一个布尔量：
      - 成品：`sys.executable` 是 `依赖/执行端-core.exe`，DLL 就在它旁边；
      - 根启动器启动时 `sys.executable` 在成品根，往里看一层 `依赖/`；
      - 源码：模块目录下的 `native/bin/x64/`。
    源码模式下前两条指向 Python 安装目录，天然不存在，会正确落到第三条。
    """

    candidates: list[Path] = []
    try:
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "native" / _NATIVE_LIBRARY_NAME)
        candidates.append(exe_dir / "依赖" / "native" / _NATIVE_LIBRARY_NAME)
    except Exception:
        pass
    candidates.append(
        Path(__file__).resolve().parent
        / "native"
        / "bin"
        / "x64"
        / _NATIVE_LIBRARY_NAME
    )
    return tuple(candidates)


def _resolve_native_library(path: Path | None) -> Path:
    from server.python.runtime_s.native_library import published_library, valid_library_name
    if path is not None:
        candidates = (Path(path),)
    else:
        candidates = _native_library_candidates()

    found: Path | None = None
    for candidate in candidates:
        try:
            if path is None:
                candidate = published_library(candidate)
            if candidate.expanduser().is_file():
                found = candidate.expanduser().resolve(strict=True)
                break
        except OSError:
            continue
    if found is None:
        looked = "、".join(str(item) for item in candidates)
        raise DesktopBridgeError(
            "DESKTOP_BRIDGE_LIBRARY_MISSING",
            f"找不到隔离桌面需要的 {_NATIVE_LIBRARY_NAME}。"
            f"找过这些位置：{looked}。"
            "成品里它应该在「依赖/native/」下；缺了就重新安装执行端。",
        )
    if not valid_library_name(found.name, _NATIVE_LIBRARY_NAME):
        raise DesktopBridgeError(
            "DESKTOP_BRIDGE_LIBRARY_NAME_INVALID",
            "原生 Hook 文件名不符合固定构建契约",
        )
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_native_library(source: Path, *, cache_root: Path | None = None) -> Path:
    """管理器长期持有运行副本，不锁住更新目录里的发布 DLL。"""
    from server.python.runtime_s.native_library import runtime_library
    return runtime_library(source, cache_root=cache_root)


def _raise_last_error(code: str, message: str) -> None:
    error = ctypes.get_last_error()
    raise DesktopBridgeError(code, f"{message}（Windows 错误 {error}）")


__all__ = ["DesktopLaunchBridge"]
