"""为严格后台 Win32 输入提供目标窗口级 ``TrackMouseEvent`` 短租约。

只有校准确认 Chromium 会在每个后台 ``WM_MOUSEMOVE`` 后注册 ``TME_LEAVE``
时才安装。DLL 内部仍按 PID、UI 线程、HWND 和租约到期时间逐项校验；任何不匹配
调用都会进入系统原实现。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sys
import threading
import time


_HOOK_MAGIC = 0x31484341
_HOOK_VERSION = 1
_MAPPING_PREFIX = "Local\\AutoDrissionCursorHook_"

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
_WAIT_OBJECT_0 = 0x00000000
_ERROR_ALREADY_EXISTS = 183
_IMAGE_FILE_MACHINE_UNKNOWN = 0x0000
_IMAGE_FILE_MACHINE_AMD64 = 0x8664


class CursorStateHookError(RuntimeError):
    """目标浏览器不满足受控 Hook 契约。"""


class _NativeCursorConfig(ctypes.Structure):
    _fields_ = [
        ("magic", wintypes.DWORD),
        ("version", wintypes.DWORD),
        ("target_pid", wintypes.DWORD),
        ("target_tid", wintypes.DWORD),
        ("target_hwnd", ctypes.c_uint64),
        ("enabled", wintypes.LONG),
        ("fail_closed", wintypes.LONG),
        ("installed", wintypes.LONG),
        ("hooks_patched", wintypes.LONG),
        ("screen_x", wintypes.LONG),
        ("screen_y", wintypes.LONG),
        ("lease_until_ms", ctypes.c_uint64),
        ("suppressed_tracks", wintypes.LONG),
        ("rejected_calls", wintypes.LONG),
        ("last_error", wintypes.LONG),
    ]


@dataclass(frozen=True, slots=True)
class CursorStateHookStatus:
    installed: bool
    enabled: bool
    hooks_patched: int
    suppressed_tracks: int
    rejected_calls: int
    last_error: int


class CursorStateHook:
    """只向已由会话校验的 64 位浏览器 UI 进程安装固定 Hook。"""

    def __init__(
        self,
        *,
        target_pid: int,
        target_tid: int,
        target_hwnd: int,
        native_library: Path | None = None,
        install_timeout_seconds: float = 8.0,
    ) -> None:
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise CursorStateHookError("光标状态 Hook 只支持 Windows x64")
        self.target_pid = int(target_pid)
        self.target_tid = int(target_tid)
        self.target_hwnd = int(target_hwnd)
        if min(self.target_pid, self.target_tid, self.target_hwnd) <= 0:
            raise ValueError("目标 PID、线程和 HWND 必须全部有效")
        from server.python.runtime_s.native_library import runtime_library
        self.native_library = runtime_library(_resolve_native_library(native_library))
        self.install_timeout_seconds = max(1.0, float(install_timeout_seconds))
        self._mapping_handle: int | None = None
        self._mapping_view: int | None = None
        self._config_pointer: ctypes.POINTER[_NativeCursorConfig] | None = None
        self._process_handle: int | None = None
        self._process_path: Path | None = None
        self._process_sha256 = ""
        self._image_handle: int | None = None
        self._image_identity: tuple[int, ...] | None = None
        self._lock = threading.RLock()

    def install(self) -> CursorStateHookStatus:
        with self._lock:
            if self._config_pointer is not None:
                return self.status()
            self._validate_window_identity()
            process_handle = _open_process(self.target_pid)
            self._process_handle = process_handle
            try:
                _require_native_x64(process_handle)
                process_path = _query_process_path(process_handle)
                self._process_path = process_path
                # 只共享读取：句柄存活期间 Windows 拒绝改写/删除/替换映像。
                # 内容只在安装时读一次；每个 WM_MOUSEMOVE 仍检查进程和文件身份。
                self._image_handle = _open_image_guard(process_path)
                self._image_identity = _file_identity(process_path)
                self._process_sha256 = _hash_file(process_path)
                mapping_created = self._create_mapping()
                if not mapping_created:
                    existing = self.status()
                    if existing.installed and existing.hooks_patched > 0:
                        return existing
                self._inject_library(process_handle)
                deadline = time.monotonic() + self.install_timeout_seconds
                while time.monotonic() < deadline:
                    status = self.status()
                    if status.installed and status.hooks_patched >= 1:
                        self._validate_process_identity()
                        return status
                    if self._config_pointer.contents.installed < 0:
                        raise CursorStateHookError("目标浏览器拒绝安装 TrackMouseEvent Hook")
                    time.sleep(0.05)
                raise CursorStateHookError("TrackMouseEvent Hook 安装超时")
            except Exception:
                self.close()
                raise

    def arm(self, screen_x: int, screen_y: int, *, lease_ms: int = 240) -> None:
        """为下一批目标 HWND 鼠标移动开放极短 ``TME_LEAVE`` 抑制租约。"""

        with self._lock:
            self._validate_window_identity()
            self._validate_process_identity()
            pointer = self._require_pointer()
            config = pointer.contents
            config.enabled = 0
            config.screen_x = int(screen_x)
            config.screen_y = int(screen_y)
            duration = min(1000, max(40, int(lease_ms)))
            config.lease_until_ms = int(_kernel32().GetTickCount64()) + duration
            _kernel32().FlushProcessWriteBuffers()
            config.enabled = 1

    def disarm(self) -> None:
        with self._lock:
            if self._config_pointer is None:
                return
            self._config_pointer.contents.enabled = 0
            self._config_pointer.contents.lease_until_ms = 0
            _kernel32().FlushProcessWriteBuffers()

    def status(self) -> CursorStateHookStatus:
        config = self._require_pointer().contents
        return CursorStateHookStatus(
            installed=int(config.installed) > 0,
            enabled=bool(config.enabled),
            hooks_patched=max(0, int(config.hooks_patched)),
            suppressed_tracks=max(0, int(config.suppressed_tracks)),
            rejected_calls=max(0, int(config.rejected_calls)),
            last_error=max(0, int(config.last_error)),
        )

    def close(self) -> None:
        with self._lock:
            self.disarm()
            kernel32 = _kernel32()
            if self._mapping_view:
                kernel32.UnmapViewOfFile(ctypes.c_void_p(self._mapping_view))
            if self._mapping_handle:
                kernel32.CloseHandle(wintypes.HANDLE(self._mapping_handle))
            if self._process_handle:
                kernel32.CloseHandle(wintypes.HANDLE(self._process_handle))
            if self._image_handle:
                kernel32.CloseHandle(wintypes.HANDLE(self._image_handle))
            self._mapping_view = None
            self._mapping_handle = None
            self._process_handle = None
            self._config_pointer = None
            self._image_handle = None
            self._image_identity = None
            self._process_path = None
            self._process_sha256 = ""

    def _validate_window_identity(self) -> None:
        from . import win32_native

        if not win32_native.is_alive(self.target_hwnd):
            raise CursorStateHookError("目标浏览器窗口已经失效")
        if win32_native.process_of(self.target_hwnd) != self.target_pid:
            raise CursorStateHookError("目标浏览器 HWND 与 PID 不一致")
        if win32_native.thread_of(self.target_hwnd) != self.target_tid:
            raise CursorStateHookError("目标浏览器 HWND 与 UI 线程不一致")

    def _validate_process_identity(self) -> None:
        handle = self._process_handle
        path = self._process_path
        if not handle or path is None:
            raise CursorStateHookError("目标浏览器进程尚未建立受控句柄")
        current = _query_process_path(handle)
        if (current != path or not self._image_handle or self._image_identity is None
                or _file_identity(current) != self._image_identity):
            raise CursorStateHookError("目标浏览器进程映像在 Hook 期间发生变化")

    def _create_mapping(self) -> bool:
        kernel32 = _kernel32()
        name = f"{_MAPPING_PREFIX}{self.target_pid}"
        handle = kernel32.CreateFileMappingW(
            wintypes.HANDLE(-1),
            None,
            _PAGE_READWRITE,
            0,
            ctypes.sizeof(_NativeCursorConfig),
            name,
        )
        if not handle:
            raise CursorStateHookError("无法创建光标 Hook 共享状态")
        created = ctypes.get_last_error() != _ERROR_ALREADY_EXISTS
        view = kernel32.MapViewOfFile(
            handle,
            _FILE_MAP_ALL_ACCESS,
            0,
            0,
            ctypes.sizeof(_NativeCursorConfig),
        )
        if not view:
            kernel32.CloseHandle(handle)
            raise CursorStateHookError("无法映射光标 Hook 共享状态")
        self._mapping_handle = int(handle)
        self._mapping_view = int(view)
        pointer = ctypes.cast(view, ctypes.POINTER(_NativeCursorConfig))
        self._config_pointer = pointer
        config = pointer.contents
        if created:
            ctypes.memset(view, 0, ctypes.sizeof(_NativeCursorConfig))
            config.magic = _HOOK_MAGIC
            config.version = _HOOK_VERSION
            config.target_pid = self.target_pid
            config.fail_closed = 1
        elif (
            config.magic != _HOOK_MAGIC
            or config.version != _HOOK_VERSION
            or config.target_pid != self.target_pid
        ):
            raise CursorStateHookError("已有光标 Hook 共享状态不属于当前目标进程")
        config.enabled = 0
        config.target_tid = self.target_tid
        config.target_hwnd = self.target_hwnd
        config.lease_until_ms = 0
        _kernel32().FlushProcessWriteBuffers()
        return created

    def _inject_library(self, process_handle: int) -> None:
        kernel32 = _kernel32()
        payload = (str(self.native_library) + "\0").encode("utf-16-le")
        remote = kernel32.VirtualAllocEx(
            process_handle,
            None,
            len(payload),
            _MEM_COMMIT | _MEM_RESERVE,
            _PAGE_READWRITE,
        )
        if not remote:
            raise CursorStateHookError("无法准备光标 Hook 远程内存")
        remote_thread = None
        try:
            buffer = ctypes.create_string_buffer(payload)
            written = ctypes.c_size_t()
            if not kernel32.WriteProcessMemory(
                process_handle,
                remote,
                buffer,
                len(payload),
                ctypes.byref(written),
            ) or int(written.value) != len(payload):
                raise CursorStateHookError("无法写入光标 Hook DLL 路径")
            module = kernel32.GetModuleHandleW("kernel32.dll")
            loader = kernel32.GetProcAddress(module, b"LoadLibraryW")
            if not loader:
                raise CursorStateHookError("系统缺少 LoadLibraryW")
            remote_thread = kernel32.CreateRemoteThread(
                process_handle,
                None,
                0,
                loader,
                remote,
                0,
                None,
            )
            if not remote_thread:
                raise CursorStateHookError("目标浏览器拒绝创建受控 Hook 线程")
            if kernel32.WaitForSingleObject(remote_thread, 10_000) != _WAIT_OBJECT_0:
                raise CursorStateHookError("目标浏览器加载光标 Hook 超时")
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeThread(remote_thread, ctypes.byref(exit_code)):
                raise CursorStateHookError("无法确认光标 Hook 加载结果")
            if int(exit_code.value) == 0:
                raise CursorStateHookError("目标浏览器拒绝加载光标 Hook DLL")
        finally:
            if remote_thread:
                kernel32.CloseHandle(remote_thread)
            kernel32.VirtualFreeEx(process_handle, remote, 0, _MEM_RELEASE)

    def _require_pointer(self) -> ctypes.POINTER[_NativeCursorConfig]:
        if self._config_pointer is None:
            raise CursorStateHookError("光标状态 Hook 尚未安装")
        return self._config_pointer


def _open_image_guard(path: Path) -> int:
    """锁定已验证映像的内容；不允许用元数据缓存替代可修改文件的哈希。"""
    handle = _kernel32().CreateFileW(str(path), 0x80000000, 0x00000001, None, 3, 0x80, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise CursorStateHookError(
            f"无法锁定目标浏览器程序以校验后台输入身份：{ctypes.WinError(ctypes.get_last_error())}"
        )
    return int(handle)


def _file_identity(path: Path) -> tuple[int, ...]:
    try:
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError as exc:
        raise CursorStateHookError("无法核对目标浏览器程序文件身份") from exc


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_process(pid: int) -> int:
    access = (
        _PROCESS_CREATE_THREAD
        | _PROCESS_QUERY_LIMITED_INFORMATION
        | _PROCESS_VM_OPERATION
        | _PROCESS_VM_READ
        | _PROCESS_VM_WRITE
    )
    handle = _kernel32().OpenProcess(access, False, int(pid))
    if not handle:
        raise CursorStateHookError("无法打开目标浏览器进程")
    return int(handle)


def _query_process_path(process_handle: int) -> Path:
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not _kernel32().QueryFullProcessImageNameW(
        process_handle,
        0,
        buffer,
        ctypes.byref(size),
    ):
        raise CursorStateHookError("无法读取目标浏览器程序路径")
    return Path(buffer.value).resolve(strict=True)


def _require_native_x64(process_handle: int) -> None:
    process_machine = wintypes.USHORT()
    native_machine = wintypes.USHORT()
    if not _kernel32().IsWow64Process2(
        process_handle,
        ctypes.byref(process_machine),
        ctypes.byref(native_machine),
    ):
        raise CursorStateHookError("无法确认目标浏览器位数")
    machine = int(process_machine.value or native_machine.value)
    if machine not in {_IMAGE_FILE_MACHINE_UNKNOWN, _IMAGE_FILE_MACHINE_AMD64}:
        raise CursorStateHookError("光标状态 Hook 只支持 64 位目标浏览器")


_NATIVE_LIBRARY_NAME = "cursor_state_hook.dll"


def _native_library_candidates() -> tuple[Path, ...]:
    """按优先级列出光标状态 Hook 可能在的位置。

    原来用 `getattr(sys, "frozen", False)` 二选一，而 **Nuitka 不设置
    `sys.frozen`**（`utils_s/path_utils.py` 里早就写着这句注释）。成品于是
    走了源码分支，去找成品里没有的 `native/bin/x64/`，当场抛
    FileNotFoundError——Win32 输入会话建不起来，而严格模式**不退回 DP/CDP
    键鼠**，于是每个输入动作都失败。隔离桌面那个 DLL 犯过一模一样的错。

    改成逐个候选去找，不再赌某一个布尔量。
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
        raise CursorStateHookError(
            f"找不到后台输入需要的 {_NATIVE_LIBRARY_NAME}。"
            f"找过这些位置：{looked}。"
            "成品里它应该在「依赖/native/」下；缺了就重新安装执行端。"
        )
    if not valid_library_name(found.name, _NATIVE_LIBRARY_NAME):
        raise CursorStateHookError("光标状态 Hook 文件名不符合固定构建契约")
    return found


_KERNEL32 = None


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
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
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.IsWow64Process2.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT),
        ctypes.POINTER(wintypes.USHORT),
    ]
    kernel32.IsWow64Process2.restype = wintypes.BOOL
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
        ctypes.c_void_p,
    ]
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeThread.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeThread.restype = wintypes.BOOL
    kernel32.FlushProcessWriteBuffers.argtypes = []
    kernel32.FlushProcessWriteBuffers.restype = None
    kernel32.GetTickCount64.argtypes = []
    kernel32.GetTickCount64.restype = ctypes.c_uint64
    _KERNEL32 = kernel32
    return kernel32


__all__ = [
    "CursorStateHook",
    "CursorStateHookError",
    "CursorStateHookStatus",
]
