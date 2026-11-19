"""与 UI 无关的 Windows 窗口查找和截图能力。"""

import ctypes
import ctypes.wintypes
import os
from dataclasses import dataclass
from io import BytesIO
import re
import random
import threading
from typing import Optional


_PW_RENDERFULLCONTENT = 0x00000002
_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_DIB_RGB_COLORS = 0
_BI_RGB = 0
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_NOOWNERZORDER = 0x0200
_SW_MAXIMIZE = 3
_SW_RESTORE = 9
_MIN_EXPOSED_EDGE = 10


# 这里只登记当前执行端进程亲自定位并摆放的自动化浏览器窗口。多窗口任务若仍
# 保留浏览器原始大尺寸并仅错开几十像素，Chromium 会把被覆盖页面判定为
# hidden，后台 Win32 随即无法取得页面指针事件进行校准。登记表按任务槽位复用，
# 浏览器重试时新 HWND 会替换同槽旧 HWND；PID 复核防止 HWND 被其它程序复用。
_AUTOMATION_WINDOW_LAYOUT_LOCK = threading.RLock()
_AUTOMATION_WINDOW_SLOTS: dict[int, tuple[int, int]] = {}


@dataclass(frozen=True, slots=True)
class MonitorBounds:
    """Windows 显示器在虚拟桌面中的绝对矩形。"""

    device: str
    left: int
    top: int
    right: int
    bottom: int


class _MonitorInfoExW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szDevice", ctypes.wintypes.WCHAR * 32),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    """Win32 BITMAPINFOHEADER；这里只使用 32 位无压缩像素。"""

    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BitmapInfoHeader),
        ("bmiColors", ctypes.wintypes.DWORD * 3),
    ]


def _window_candidates(
    process_ids: set[int],
    *,
    desktop_handle: int | None = None,
) -> list[tuple[int, int, int]]:
    """返回指定进程族的可见顶层窗口：(浏览器类优先级, 面积, 句柄)。"""

    if not process_ids:
        return []
    user32 = ctypes.windll.user32
    windows: list[tuple[int, int, int]] = []

    def collect_window(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid_out = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
            if pid_out.value in process_ids:
                rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                if width > 100 and height > 100:
                    class_name = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, class_name, len(class_name))
                    browser_class = int(
                        class_name.value.startswith("Chrome_WidgetWin_")
                        or class_name.value == "MozillaWindowClass"
                    )
                    windows.append((browser_class, width * height, int(hwnd)))
        return True

    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )
    callback = callback_type(collect_window)
    if desktop_handle:
        user32.EnumDesktopWindows(
            ctypes.wintypes.HANDLE(int(desktop_handle)),
            callback,
            0,
        )
    else:
        user32.EnumWindows(callback, 0)
    windows.sort(reverse=True)
    return windows


def browser_process_ids_by_port(debug_port: int, *, include_parents: bool = True) -> set[int]:
    """返回调试端口所属进程及子进程；需要时再包含有限父进程链。"""

    if not debug_port:
        return set()

    import psutil

    target_pid = None
    try:
        for connection in psutil.net_connections(kind="inet"):
            if (
                connection.status == psutil.CONN_LISTEN
                and connection.laddr
                and connection.laddr.port == debug_port
                and connection.pid
            ):
                target_pid = connection.pid
                break
    except (psutil.AccessDenied, OSError):
        for process in psutil.process_iter(["pid"]):
            try:
                for connection in process.connections():
                    if (
                        connection.status == "LISTEN"
                        and connection.laddr.port == debug_port
                    ):
                        target_pid = process.pid
                        break
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
            if target_pid:
                break

    if not target_pid:
        return set()

    process_ids = {target_pid}
    try:
        process = psutil.Process(target_pid)
        process_ids.update(child.pid for child in process.children(recursive=True))
        if include_parents:
            # 有些内核由子进程监听调试端口，而顶层窗口属于父进程。
            for _ in range(4):
                process = process.parent()
                if process is None:
                    break
                process_ids.add(process.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    return process_ids


def find_chrome_window_by_port(
    debug_port: int,
    *,
    desktop_handle: int | None = None,
) -> Optional[int]:
    """通过调试端口找浏览器主窗口，可限定到一个隔离 Desktop。"""

    windows = _window_candidates(
        browser_process_ids_by_port(debug_port),
        desktop_handle=desktop_handle,
    )
    return windows[0][2] if windows else None


def find_browser_windows_by_process_name(process_name: str) -> list[int]:
    """按进程名列出可见浏览器窗口，供无调试端口的内核定位。"""

    import psutil

    expected = str(process_name or "").replace("/", "\\").rsplit("\\", 1)[-1]
    expected = expected.casefold()
    if not expected:
        return []
    expected_stem = expected[:-4] if expected.endswith(".exe") else expected
    process_ids: set[int] = set()
    for process in psutil.process_iter(["pid", "name"]):
        try:
            actual = str(process.info.get("name") or "").casefold()
            actual_stem = actual[:-4] if actual.endswith(".exe") else actual
            if actual == expected or actual_stem == expected_stem:
                process_ids.add(int(process.info["pid"]))
        except (psutil.AccessDenied, psutil.NoSuchProcess, TypeError, ValueError):
            continue
    return [item[2] for item in _window_candidates(process_ids)]


def list_monitor_bounds() -> list[MonitorBounds]:
    """按 Windows 显示器编号列出所有屏幕，最后一项即最后一块屏幕。"""

    user32 = ctypes.windll.user32
    monitors: list[MonitorBounds] = []
    monitor_callback = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HMONITOR,
        ctypes.wintypes.HDC,
        ctypes.POINTER(ctypes.wintypes.RECT),
        ctypes.wintypes.LPARAM,
    )

    def collect_monitor(monitor, _dc, _rect, _data):
        info = _MonitorInfoExW()
        info.cbSize = ctypes.sizeof(_MonitorInfoExW)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            monitors.append(MonitorBounds(
                device=str(info.szDevice),
                left=int(info.rcMonitor.left),
                top=int(info.rcMonitor.top),
                right=int(info.rcMonitor.right),
                bottom=int(info.rcMonitor.bottom),
            ))
        return True

    user32.EnumDisplayMonitors(0, None, monitor_callback(collect_monitor), 0)

    def sort_key(item: MonitorBounds) -> tuple[int, str, int, int]:
        match = re.search(r"(\d+)$", item.device)
        return (
            int(match.group(1)) if match else -1,
            item.device.casefold(),
            item.left,
            item.top,
        )

    monitors.sort(key=sort_key)
    return monitors


def move_browser_window_to_last_monitor(hwnd: int, slot_index: int = 0) -> tuple[int, int]:
    """把自动化窗口放到最后一块屏幕，并让并发窗口保持真实可见。

    单个窗口继续保留浏览器启动尺寸。登记到两个及以上活动槽位后，只重排本
    进程登记的自动化窗口，按可见网格缩放；不激活窗口，也不触碰用户窗口。
    """

    monitors = list_monitor_bounds()
    if not monitors:
        raise RuntimeError("Windows 没有返回可用显示器")
    target = monitors[-1]
    safe_hwnd = int(hwnd or 0)
    if safe_hwnd <= 0:
        raise ValueError("浏览器窗口句柄无效")
    process_id = get_window_process_id(safe_hwnd)
    if process_id is None:
        raise LookupError("浏览器窗口已失效")

    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.wintypes.UINT,
    ]
    user32.SetWindowPos.restype = ctypes.wintypes.BOOL

    with _AUTOMATION_WINDOW_LAYOUT_LOCK:
        safe_slot = max(0, int(slot_index))
        _AUTOMATION_WINDOW_SLOTS[safe_slot] = (safe_hwnd, process_id)
        for registered_slot, (registered_hwnd, registered_pid) in tuple(
            _AUTOMATION_WINDOW_SLOTS.items()
        ):
            if get_window_process_id(registered_hwnd) != registered_pid:
                _AUTOMATION_WINDOW_SLOTS.pop(registered_slot, None)

        windows = sorted(_AUTOMATION_WINDOW_SLOTS.items())
        if not windows:
            raise LookupError("没有可摆放的自动化浏览器窗口")

        common_flags = _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER
        if len(windows) == 1:
            x, y = target.left, target.top
            show_window = getattr(user32, "ShowWindow", None)
            if callable(show_window):
                show_window(ctypes.wintypes.HWND(safe_hwnd), _SW_RESTORE)
            moved = user32.SetWindowPos(
                ctypes.wintypes.HWND(safe_hwnd),
                None,
                x,
                y,
                0,
                0,
                _SWP_NOSIZE | common_flags,
            )
            if not moved:
                raise OSError("Win32 无法移动浏览器窗口")
            if callable(show_window):
                show_window(ctypes.wintypes.HWND(safe_hwnd), _SW_MAXIMIZE)
            return x, y

        monitor_width = max(1, target.right - target.left)
        monitor_height = max(1, target.bottom - target.top)
        gap_count = len(windows) - 1
        # 网格把 1920×1080 的五个窗口压成 640×540，会触发站点响应式布局并让
        # 原本正确的桌面选择器消失。这里改为大视口阶梯排列：每个旧窗口都保留
        # 一条真实网页区域暴露在外，Chromium 不会判定为完全遮挡，同时主视口
        # 仍接近正常桌面尺寸。窗口越多间距自动缩小，全部保持在目标屏幕内。
        preferred_offset = min(
            (monitor_width - 1200) // gap_count,
            (monitor_height - 680) // gap_count,
        )
        fit_offset = min(
            (monitor_width - 320) // gap_count,
            (monitor_height - 240) // gap_count,
        )
        if fit_offset < _MIN_EXPOSED_EDGE:
            raise RuntimeError("当前屏幕无法为全部浏览器保留至少 10 像素可见边缘")
        offset = max(
            _MIN_EXPOSED_EDGE,
            min(120, max(40, preferred_offset), fit_offset),
        )
        window_width = max(320, monitor_width - offset * gap_count)
        window_height = max(240, monitor_height - offset * gap_count)
        current_position: tuple[int, int] | None = None
        current_moved = False
        for rank, (_registered_slot, (registered_hwnd, _pid)) in enumerate(windows):
            show_window = getattr(user32, "ShowWindow", None)
            if callable(show_window):
                show_window(ctypes.wintypes.HWND(registered_hwnd), _SW_RESTORE)
            left = target.left + rank * offset
            top = target.top + rank * offset
            moved = bool(user32.SetWindowPos(
                ctypes.wintypes.HWND(registered_hwnd),
                None,
                left,
                top,
                window_width,
                window_height,
                common_flags,
            ))
            if registered_hwnd == safe_hwnd:
                current_position = (left, top)
                current_moved = moved

        if not current_moved or current_position is None:
            raise OSError("Win32 无法排列浏览器窗口")
        return current_position


def move_browser_window_random_offset(
    hwnd: int,
    *,
    min_x: int = 0,
    max_x: int = 100,
    min_y: int = 0,
    max_y: int = 100,
    rng=None,
) -> tuple[int, int]:
    """在隔离 Desktop 内把已验证窗口随机错开，不激活也不改变原尺寸。

    该函数不登记普通桌面的多窗口布局槽位，只供管理器型指纹浏览器的隔离
    Desktop 分支调用。最大化窗口先恢复为普通窗口，否则 Windows 会忽略坐标。
    """

    safe_hwnd = int(hwnd or 0)
    if safe_hwnd <= 0:
        raise ValueError("浏览器窗口句柄无效")
    if get_window_process_id(safe_hwnd) is None:
        raise LookupError("浏览器窗口已失效")
    lower_x, upper_x = sorted((int(min_x), int(max_x)))
    lower_y, upper_y = sorted((int(min_y), int(max_y)))
    chooser = rng or random
    x = int(chooser.randint(lower_x, upper_x))
    y = int(chooser.randint(lower_y, upper_y))

    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.wintypes.UINT,
    ]
    user32.SetWindowPos.restype = ctypes.wintypes.BOOL
    native_hwnd = ctypes.wintypes.HWND(safe_hwnd)
    show_window = getattr(user32, "ShowWindow", None)
    is_zoomed = getattr(user32, "IsZoomed", None)
    if callable(show_window) and callable(is_zoomed) and bool(is_zoomed(native_hwnd)):
        show_window(native_hwnd, _SW_RESTORE)
    # 隔离 Desktop 只显示主屏那一块，不是多屏虚拟区。窗口尺寸是按普通桌面
    # （本机虚拟区 6720 宽）定的，直接搬过来右边和下边会被切在屏幕外——人工
    # 切过去只能看到半个浏览器。这里按主屏真实大小把窗口收进来。
    width, height = _window_size(safe_hwnd)
    screen_w = int(user32.GetSystemMetrics(_SM_CXSCREEN))
    screen_h = int(user32.GetSystemMetrics(_SM_CYSCREEN))
    flags = _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER
    if width > 0 and height > 0 and screen_w > 0 and screen_h > 0:
        x = max(0, min(x, max(0, screen_w - _MIN_VISIBLE_EDGE)))
        y = max(0, min(y, max(0, screen_h - _MIN_VISIBLE_EDGE)))
        width = min(width, max(_MIN_VISIBLE_EDGE, screen_w - x))
        height = min(height, max(_MIN_VISIBLE_EDGE, screen_h - y))
    else:
        width = height = 0
        flags |= _SWP_NOSIZE
    moved = user32.SetWindowPos(
        native_hwnd,
        None,
        x,
        y,
        width,
        height,
        flags,
    )
    if not moved:
        raise OSError("Win32 无法在隔离 Desktop 内移动浏览器窗口")
    return x, y


def desktop_has_window(desktop_name: str, hwnd: int) -> bool:
    """这个隔离 Desktop 上现在还有没有这个窗口。

    用来挡住「切过去一片黑」：浏览器关掉之后 Desktop 还留着（本机一度攒到 10
    个，其中好几个是空的）。槽位记的 desktop_name 可能指向一个已经空掉的桌面，
    真切过去就是全黑，人还得摸黑切回来。进去之前先确认它真在那儿。
    """

    if os.name != "nt":
        return False
    name = str(desktop_name or "").strip()
    target = int(hwnd or 0)
    if not name or target <= 0:
        return False
    user32 = ctypes.windll.user32
    user32.OpenDesktopW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    user32.OpenDesktopW.restype = ctypes.wintypes.HANDLE
    handle = user32.OpenDesktopW(name, 0, False, _DESKTOP_ENUMERATE_ACCESS)
    if not handle:
        return False
    found = False

    @_ENUM_DESKTOP_PROC
    def _each(candidate, _param):
        nonlocal found
        if int(candidate) == target:
            found = True
            return False
        return True

    try:
        user32.EnumDesktopWindows.argtypes = [
            ctypes.wintypes.HANDLE,
            _ENUM_DESKTOP_PROC,
            ctypes.wintypes.LPARAM,
        ]
        user32.EnumDesktopWindows.restype = ctypes.wintypes.BOOL
        user32.EnumDesktopWindows(handle, _each, 0)
    finally:
        user32.CloseDesktop(ctypes.wintypes.HANDLE(handle))
    return found


def _window_size(hwnd: int) -> tuple[int, int]:
    """当前窗口的外框宽高；读不到返回 (0, 0)。"""

    user32 = ctypes.windll.user32
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(ctypes.wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
        return 0, 0
    return int(rect.right - rect.left), int(rect.bottom - rect.top)


def move_browser_window_by_port(
    debug_port: int,
    slot_index: int = 0,
) -> int | None:
    """按调试端口定位并移动窗口，返回实际窗口句柄。"""

    hwnd = find_chrome_window_by_port(debug_port)
    if not hwnd:
        return None
    move_browser_window_to_last_monitor(hwnd, slot_index)
    return hwnd


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """返回 Win32 顶层窗口的绝对矩形。"""

    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.RECT),
    ]
    user32.GetWindowRect.restype = ctypes.wintypes.BOOL
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(ctypes.wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
        raise OSError("无法读取浏览器窗口尺寸")
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def get_window_process_id(hwnd: int) -> int | None:
    """读取当前拥有窗口句柄的进程号；窗口失效时返回 ``None``。"""

    safe_hwnd = int(hwnd or 0)
    if safe_hwnd <= 0:
        return None
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindow.restype = ctypes.wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
    native_hwnd = ctypes.wintypes.HWND(safe_hwnd)
    if not user32.IsWindow(native_hwnd):
        return None
    process_id = ctypes.wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(native_hwnd, ctypes.byref(process_id)):
        return None
    return int(process_id.value) or None


# 截图时右边和下边各多留的像素
_CAPTURE_EDGE_PAD = 10   # 画面在窗口四周各留的边
_BLACKNESS = 0x00000042

# 主屏尺寸（隔离 Desktop 显示的就是这一块，不是多屏虚拟区）
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1
# 收进屏幕时至少保留的可见边长，避免算成 0 尺寸
_MIN_VISIBLE_EDGE = 200

# 只枚举窗口，够用就行
_DESKTOP_ENUMERATE_ACCESS = 0x0001 | 0x0100
_ENUM_DESKTOP_PROC = ctypes.WINFUNCTYPE(
    ctypes.c_bool,
    ctypes.wintypes.HWND,
    ctypes.wintypes.LPARAM,
)


def _require_window_process(hwnd: int, expected_process_id: int | None) -> None:
    """在捕获边界拒绝已失效或已被其它进程复用的窗口句柄。"""

    if expected_process_id is None:
        return
    current_process_id = get_window_process_id(hwnd)
    if current_process_id != int(expected_process_id):
        raise LookupError("浏览器窗口已失效或已被其它进程复用")


def _capture_window_with_win32(
    hwnd: int,
    *,
    visible_surface: bool = False,
    expected_process_id: int | None = None,
):
    """使用 PrintWindow/GDI 读取窗口，不占用任何浏览器自动化连接。"""

    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    # ctypes 未声明返回类型时会按 32 位整数处理，在 64 位 Windows 上可能截断
    # HDC/HBITMAP。这里把本函数用到的 Win32 签名集中声明清楚。
    user32.GetWindowRect.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.RECT),
    ]
    user32.GetWindowRect.restype = ctypes.wintypes.BOOL
    user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
    user32.IsIconic.restype = ctypes.wintypes.BOOL
    user32.GetWindowDC.argtypes = [ctypes.wintypes.HWND]
    user32.GetWindowDC.restype = ctypes.wintypes.HDC
    user32.GetDC.argtypes = [ctypes.wintypes.HWND]
    user32.GetDC.restype = ctypes.wintypes.HDC
    user32.PrintWindow.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.wintypes.HDC,
        ctypes.wintypes.UINT,
    ]
    user32.PrintWindow.restype = ctypes.wintypes.BOOL
    user32.ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [ctypes.wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = ctypes.wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [
        ctypes.wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
    ]
    gdi32.CreateCompatibleBitmap.restype = ctypes.wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [ctypes.wintypes.HDC, ctypes.wintypes.HANDLE]
    gdi32.SelectObject.restype = ctypes.wintypes.HANDLE
    gdi32.BitBlt.argtypes = [
        ctypes.wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = ctypes.wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        ctypes.wintypes.HDC,
        ctypes.wintypes.HBITMAP,
        ctypes.wintypes.UINT,
        ctypes.wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BitmapInfo),
        ctypes.wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.PatBlt.argtypes = [
        ctypes.wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.wintypes.DWORD,
    ]
    gdi32.PatBlt.restype = ctypes.wintypes.BOOL
    gdi32.SetViewportOrgEx.argtypes = [
        ctypes.wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    gdi32.SetViewportOrgEx.restype = ctypes.wintypes.BOOL
    gdi32.DeleteObject.argtypes = [ctypes.wintypes.HANDLE]
    gdi32.DeleteObject.restype = ctypes.wintypes.BOOL
    gdi32.DeleteDC.argtypes = [ctypes.wintypes.HDC]
    gdi32.DeleteDC.restype = ctypes.wintypes.BOOL
    _require_window_process(hwnd, expected_process_id)
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("无法读取浏览器窗口尺寸")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise RuntimeError("浏览器窗口尺寸无效")
    # 上下左右各向外扩 10 像素。两个原因：非整数缩放（本机 125%）下
    # GetWindowRect 取整会削掉最后一行/列，浏览器窗口的下边框在画面里就没了；
    # 而且贴边显示时人分不清「窗口边缘」和「画面被裁掉了」。留一圈窄边，
    # 整个软件窗口连边框一起看得见。
    width += _CAPTURE_EDGE_PAD * 2
    height += _CAPTURE_EDGE_PAD * 2
    if user32.IsIconic(hwnd):
        raise RuntimeError("浏览器窗口已最小化，恢复窗口后才能读取画面")

    # Chromium/WebView2 使用 DirectComposition 时，PrintWindow 可能返回成功却
    # 只画出空白客户区。真实 UI 验收窗口在独立桌面无遮挡显示，此时允许从
    # 该桌面的屏幕 DC 读取可见合成结果；普通浏览器远程画面仍默认 PrintWindow。
    source_handle = 0 if visible_surface else hwnd
    window_dc = (
        user32.GetDC(ctypes.wintypes.HWND(0))
        if visible_surface
        else user32.GetWindowDC(hwnd)
    )
    if not window_dc:
        raise OSError("无法取得浏览器窗口画布")
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    previous = None
    try:
        if not memory_dc or not bitmap:
            raise OSError("无法创建浏览器截图画布")
        previous = gdi32.SelectObject(memory_dc, bitmap)
        # 新位图内容未定义，先涂黑；外扩出来的那一圈如果抓不到真实像素，
        # 至少是一条干净的黑边，不是随机噪点。
        gdi32.PatBlt(memory_dc, 0, 0, width, height, _BLACKNESS)

        if visible_surface:
            _require_window_process(hwnd, expected_process_id)
            captured = bool(gdi32.BitBlt(
                memory_dc,
                0,
                0,
                width,
                height,
                window_dc,
                int(rect.left) - _CAPTURE_EDGE_PAD,
                int(rect.top) - _CAPTURE_EDGE_PAD,
                _SRCCOPY | _CAPTUREBLT,
            ))
        else:
            # PrintWindow 可以抓取被其他窗口遮挡或移动到屏幕外的窗口。某些旧内核
            # 不支持 PW_RENDERFULLCONTENT，失败时再退回窗口 DC 的 GDI BitBlt。
            _require_window_process(hwnd, expected_process_id)
            # PrintWindow 固定画在 DC 原点，没有偏移参数；改用视口原点把它整体
            # 往里挪，四周才留得出那一圈。GetDIBits 读整张位图，不受视口影响。
            gdi32.SetViewportOrgEx(
                memory_dc, _CAPTURE_EDGE_PAD, _CAPTURE_EDGE_PAD, None)
            try:
                captured = bool(
                    user32.PrintWindow(hwnd, memory_dc, _PW_RENDERFULLCONTENT))
            finally:
                gdi32.SetViewportOrgEx(memory_dc, 0, 0, None)
            if not captured:
                _require_window_process(hwnd, expected_process_id)
                captured = bool(gdi32.BitBlt(
                    memory_dc,
                    _CAPTURE_EDGE_PAD,
                    _CAPTURE_EDGE_PAD,
                    width - _CAPTURE_EDGE_PAD * 2,
                    height - _CAPTURE_EDGE_PAD * 2,
                    window_dc,
                    0,
                    0,
                    _SRCCOPY | _CAPTUREBLT,
                ))
        if not captured:
            raise OSError("Win32 无法读取浏览器窗口画面")

        bitmap_info = _BitmapInfo()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        bitmap_info.bmiHeader.biWidth = width
        # 负高度让 GetDIBits 直接返回从上到下排列的像素。
        bitmap_info.bmiHeader.biHeight = -height
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = _BI_RGB
        pixel_buffer = ctypes.create_string_buffer(width * height * 4)
        copied_lines = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            pixel_buffer,
            ctypes.byref(bitmap_info),
            _DIB_RGB_COLORS,
        )
        if copied_lines != height:
            raise OSError("Win32 浏览器截图像素读取不完整")
        _require_window_process(hwnd, expected_process_id)
        return Image.frombuffer(
            "RGB",
            (width, height),
            pixel_buffer,
            "raw",
            "BGRX",
            0,
            1,
        ).copy()
    finally:
        if previous and memory_dc:
            gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(source_handle, window_dc)


def capture_browser_window_by_handle(
    hwnd: int,
    *,
    image_format: str = "PNG",
    region: tuple[int, int, int, int] | None = None,
    max_width: int | None = None,
    quality: int = 65,
    visible_surface: bool = False,
    desktop_region: bool = False,
    expected_process_id: int | None = None,
) -> bytes:
    """只通过 Win32 窗口句柄截图，并返回 PNG 或 JPEG 字节。

    ``region`` 使用窗口左上角为原点，传入 ``(left, top, right, bottom)``。
    该函数是产品内唯一截图实现；驱动、动作保存和远程画面都必须调用它。
    """

    from PIL import Image

    normalized_format = str(image_format or "PNG").strip().upper()
    if normalized_format not in {"PNG", "JPEG"}:
        raise ValueError("Win32 截图只支持 PNG 或 JPEG")
    if desktop_region:
        from .browser_region_capture import capture_desktop_region

        image = capture_desktop_region(
            int(hwnd), expected_process_id=expected_process_id,
            capture_window=_capture_window_with_win32, edge_pad=_CAPTURE_EDGE_PAD,
        )
    else:
        image = _capture_window_with_win32(
            int(hwnd),
            visible_surface=bool(visible_surface),
            expected_process_id=expected_process_id,
        )
    try:
        if region is not None:
            if len(region) != 4:
                raise ValueError("截图区域必须包含 left、top、right、bottom")
            left, top, right, bottom = (int(value) for value in region)
            if desktop_region:
                left, top, right, bottom = (
                    value + _CAPTURE_EDGE_PAD for value in (left, top, right, bottom)
                )
            left = max(0, min(image.width, left))
            top = max(0, min(image.height, top))
            right = max(left + 1, min(image.width, right))
            bottom = max(top + 1, min(image.height, bottom))
            cropped = image.crop((left, top, right, bottom))
            image.close()
            image = cropped
        if max_width is not None:
            width_limit = max(320, min(1920, int(max_width)))
            if image.width > width_limit:
                target_height = max(
                    1,
                    round(image.height * width_limit / image.width),
                )
                resized = image.resize(
                    (width_limit, target_height),
                    Image.Resampling.LANCZOS,
                )
                image.close()
                image = resized
        if image.mode != "RGB":
            converted = image.convert("RGB")
            image.close()
            image = converted
        output = BytesIO()
        save_options = {}
        if normalized_format == "JPEG":
            save_options = {
                "quality": max(20, min(90, int(quality))),
                "optimize": True,
            }
        image.save(output, format=normalized_format, **save_options)
        return output.getvalue()
    finally:
        image.close()


def capture_chrome_window_by_port(
    debug_port: int,
    *,
    max_width: int = 1280,
    quality: int = 65,
) -> bytes:
    """按调试端口用 Win32 抓取浏览器窗口，并返回有限尺寸的 JPEG。

    截图只使用窗口句柄和 Win32 GDI，不调用 DrissionPage、Selenium、CDP 或
    指纹浏览器截图接口，因此不会抢占正在执行动作的浏览器连接。
    """

    hwnd = find_chrome_window_by_port(debug_port)
    if not hwnd:
        raise LookupError(f"没有找到连接端口 {debug_port} 对应的浏览器窗口")
    process_id = get_window_process_id(hwnd)
    if process_id is None:
        raise LookupError(f"连接端口 {debug_port} 对应的浏览器窗口已经失效")

    return capture_browser_window_by_handle(
        hwnd,
        image_format="JPEG",
        max_width=max_width,
        quality=quality,
        expected_process_id=process_id,
    )


__all__ = [
    "MonitorBounds",
    "capture_browser_window_by_handle",
    "capture_chrome_window_by_port",
    "browser_process_ids_by_port",
    "find_browser_windows_by_process_name",
    "find_chrome_window_by_port",
    "get_window_process_id",
    "get_window_rect",
    "list_monitor_bounds",
    "move_browser_window_by_port",
    "move_browser_window_to_last_monitor",
]
