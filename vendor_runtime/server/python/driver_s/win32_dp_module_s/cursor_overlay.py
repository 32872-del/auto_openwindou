"""在隔离 Desktop 显示后台鼠标的实际输入位置；移动、点击和滚轮仍由 BgMouse 统一发送。"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
import os
import threading
import time

from . import win32_native as W


@dataclass(frozen=True, slots=True)
class PointerFrame:
    # 相对浏览器顶层客户区的位置；原生下拉可以位于浏览器矩形之外。
    x: int
    y: int
    buttons: int = 0
    click_until: float = 0.0
    wheel_until: float = 0.0
    wheel: int = 0


_DOWN = {0x0201, 0x0203, 0x0204, 0x0206, 0x0207, 0x020B, 0x020D}
_UP = {0x0202, 0x0205, 0x0208, 0x020C}
_BUTTONS = 0x0001 | 0x0002 | 0x0010 | 0x0020 | 0x0040
_SIZE = 72
_HOTSPOT = 20
_TRANSPARENT = 0x00FF00FF
_EX_STYLE = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000


def advance_frame(previous: PointerFrame | None, x: int, y: int,
                  message: int, wparam: int, now: float) -> PointerFrame:
    """观察发送结果；滚轮与点击提示不因下一条移动消息而提前消失。"""
    frame = replace(previous, x=x, y=y) if previous else PointerFrame(x, y)
    if message in _DOWN or message in _UP:
        return replace(frame, buttons=wparam & _BUTTONS, click_until=now + 0.35)
    if message == 0x020A:
        delta = (wparam >> 16) & 0xFFFF
        if delta & 0x8000:
            delta -= 0x10000
        return replace(frame, wheel=delta, wheel_until=now + 0.45)
    return replace(frame, buttons=wparam & _BUTTONS)


def _warn(error: Exception) -> None:
    from server.python.log_s import log
    log.warning(f"[虚拟鼠标] 指针显示失败：{type(error).__name__}: {error}")


class CursorObserver:
    """每个 BgMouse 的轻量观察器；GUI 创建、重绘均在另外的线程完成。"""

    def __init__(self) -> None:
        self._target = None
        self._overlay: _PointerWindow | None = None
        self._failed = False

    def notify(self, target, x, y, message: int, wparam: int) -> None:
        if self._failed or os.name != "nt" or x is None or y is None:
            return
        try:
            key = (int(target.hwnd), int(getattr(target, "cursor_owner_hwnd", 0) or 0))
            ended = self._overlay is not None and self._overlay.stopped.is_set() and self._overlay.error is None
            if key != self._target or ended:
                self._overlay = _acquire(*key)
                self._target = key
            overlay = self._overlay
            if overlay is None or overlay.stopped.is_set():
                return
            sx, sy = W.client_to_screen(key[0], int(x), int(y))
            ox, oy = W.client_to_screen(overlay.owner, 0, 0)
            overlay.update(sx - ox, sy - oy, message, wparam)
        except Exception as error:
            self._failed = True
            _warn(error)

    def close(self) -> None:
        overlay, self._overlay = self._overlay, None
        self._target = None
        if overlay is not None:
            overlay.stop()


_windows: dict[int, "_PointerWindow"] = {}
_windows_lock = threading.Lock()


def _acquire(target: int, owner_hint: int = 0) -> "_PointerWindow | None":
    owner = int(W.root_of(owner_hint or target) or owner_hint or target)
    name = W.window_desktop_name(owner)
    # 仅在本项目的隔离 Desktop 绘制；用户进入该 Desktop 查看时仍继续显示。
    if not name or not name.casefold().startswith("autodrission"):
        return None
    if W.window_desktop_name(target) != name:
        return None
    with _windows_lock:
        window = _windows.get(owner)
        if window is None or window.stopped.is_set():
            window = _PointerWindow(owner, name)
            _windows[owner] = window
            window.thread.start()
        return window


def close_all() -> None:
    with _windows_lock:
        windows = list(_windows.values())
    for window in windows:
        window.stop()


atexit.register(close_all)


class _PointerWindow:
    def __init__(self, owner: int, desktop: str) -> None:
        self.owner, self.desktop = owner, desktop
        self.owner_thread = W.thread_of(owner)
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self._lock = threading.Lock()
        self._frame: PointerFrame | None = None
        self._paint_frame: PointerFrame | None = None
        self.hwnd = 0
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"virtual-pointer-{owner}")

    def update(self, x: int, y: int, message: int, wparam: int) -> None:
        with self._lock:
            self._frame = advance_frame(self._frame, x, y, message, wparam, time.monotonic())

    def stop(self) -> None:
        self.stopped.set()
        if self.thread is not threading.current_thread() and self.thread.is_alive():
            self.thread.join(timeout=0.5)

    def _run(self) -> None:
        try:
            self._run_native()
        except Exception as error:
            self.error = error
            _warn(error)
        finally:
            self.stopped.set()
            self.ready.set()
            with _windows_lock:
                if _windows.get(self.owner) is self:
                    _windows.pop(self.owner, None)

    def _run_native(self) -> None:
        import win32api
        import win32gui

        self._gui = win32gui
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.GetThreadDesktop.argtypes = [wintypes.DWORD]
        api.GetThreadDesktop.restype = wintypes.HANDLE
        api.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenDesktopW.restype = wintypes.HANDLE
        api.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        api.SetThreadDesktop.restype = wintypes.BOOL
        api.CloseDesktop.argtypes = [wintypes.HANDLE]
        api.CloseDesktop.restype = wintypes.BOOL
        previous = api.GetThreadDesktop(threading.get_native_id())
        desktop = 0
        atom = 0
        instance = win32api.GetModuleHandle(None)
        name = f"AutoDrissionVirtualPointer_{os.getpid()}_{self.owner}_{id(self)}"
        try:
            # 不需要 SwitchDesktop 权限，也不切换用户当前桌面。
            desktop = api.OpenDesktopW(self.desktop, 0, False, 0x0001 | 0x0002 | 0x0080)
            if not desktop or not api.SetThreadDesktop(desktop):
                raise ctypes.WinError(ctypes.get_last_error())
            wc = win32gui.WNDCLASS()
            wc.hInstance, wc.lpszClassName, wc.lpfnWndProc = instance, name, self._wndproc
            atom = win32gui.RegisterClass(wc)
            self.hwnd = win32gui.CreateWindowEx(
                _EX_STYLE, name, "自动化鼠标", 0x80000000,
                0, 0, _SIZE, _SIZE, 0, 0, instance, None,
            )
            # Layered + TRANSPARENT 使实际鼠标穿过整个窗口（包括指针图形）。
            win32gui.SetLayeredWindowAttributes(self.hwnd, _TRANSPARENT, 255, 1)
            self.ready.set()
            rendered = None
            while not self.stopped.wait(1 / 60):
                win32gui.PumpWaitingMessages()
                if not win32gui.IsWindow(self.owner) or W.thread_of(self.owner) != self.owner_thread:
                    break
                with self._lock:
                    frame = self._frame
                if frame is None or win32gui.IsIconic(self.owner) or not win32gui.IsWindowVisible(self.owner):
                    win32gui.ShowWindow(self.hwnd, 0)
                    rendered = None
                    continue
                now = time.monotonic()
                ox, oy = win32gui.ClientToScreen(self.owner, (0, 0))
                current = (ox + frame.x, oy + frame.y, frame.buttons,
                           frame.click_until > now, frame.wheel if frame.wheel_until > now else 0)
                if current != rendered:
                    self._paint_frame = frame
                    win32gui.SetWindowPos(self.hwnd, -1, current[0] - _HOTSPOT,
                                         current[1] - _HOTSPOT, _SIZE, _SIZE,
                                         0x0010 | 0x0040)  # NOACTIVATE | SHOWWINDOW
                    win32gui.InvalidateRect(self.hwnd, None, False)
                    win32gui.UpdateWindow(self.hwnd)
                    rendered = current
        finally:
            self.stopped.set()
            self.ready.set()
            if self.hwnd and win32gui.IsWindow(self.hwnd):
                win32gui.DestroyWindow(self.hwnd)
            self.hwnd = 0
            if atom:
                win32gui.UnregisterClass(name, instance)
            if desktop:
                if previous:
                    api.SetThreadDesktop(previous)
                api.CloseDesktop(desktop)

    def _wndproc(self, hwnd, message, wparam, lparam):
        gui = self._gui
        if message == 0x0084:  # WM_NCHITTEST
            return -1  # HTTRANSPARENT
        if message == 0x0021:  # WM_MOUSEACTIVATE
            return 3  # MA_NOACTIVATE
        if message == 0x0014:
            return 1
        if message == 0x000F:
            dc, paint = gui.BeginPaint(hwnd)
            try:
                self._draw(dc)
            finally:
                gui.EndPaint(hwnd, paint)
            return 0
        if message == 0x0318:  # WM_PRINTCLIENT：使用同一绘制代码导出诊断画面。
            self._draw(wparam)
            return 0
        if message == 0x0010:
            self.stopped.set()
            return 0
        return gui.DefWindowProc(hwnd, message, wparam, lparam)

    def _draw(self, dc) -> None:
        gui = self._gui
        background = gui.CreateSolidBrush(_TRANSPARENT)
        fill = gui.CreateSolidBrush(0xFFFFFF)
        outline = gui.CreatePen(0, 2, 0x202020)
        accent = gui.CreatePen(0, 3, 0x00B7FF)
        old_pen = old_brush = None
        try:
            gui.FillRect(dc, (0, 0, _SIZE, _SIZE), background)
            old_pen = gui.SelectObject(dc, outline)
            old_brush = gui.SelectObject(dc, fill)
            gui.Polygon(dc, [(20, 20), (20, 45), (27, 39), (33, 51),
                             (39, 48), (33, 36), (45, 36)])
            frame = self._paint_frame
            now = time.monotonic()
            if frame and (frame.buttons or frame.click_until > now):
                gui.SelectObject(dc, accent)
                gui.SelectObject(dc, gui.GetStockObject(5))  # NULL_BRUSH
                gui.Ellipse(dc, 5, 5, 35, 35)
            if frame and frame.wheel_until > now:
                gui.SelectObject(dc, accent)
                y, end = (12, 34) if frame.wheel > 0 else (34, 12)
                gui.Polyline(dc, [(53, end), (53, y), (47, y + (6 if y == 12 else -6))])
                gui.Polyline(dc, [(53, y), (59, y + (6 if y == 12 else -6))])
        finally:
            if old_pen is not None:
                gui.SelectObject(dc, old_pen)
            if old_brush is not None:
                gui.SelectObject(dc, old_brush)
            for item in (background, fill, outline, accent):
                gui.DeleteObject(item)


__all__ = ["CursorObserver", "close_all"]
