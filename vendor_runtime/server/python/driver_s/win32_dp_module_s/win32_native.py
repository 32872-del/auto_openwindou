# -*- coding: utf-8 -*-
"""Win32 + DP 驱动唯一 Win32 平台底层。

依据：Chromium 151 源码取证 +  AdsPower/Chrome 149 实测，详见
《后台键鼠注入-实现规格.md》。

要点
----
* 鼠标用 PostMessage：坐标取自 lParam、按键状态取自 wParam，Chromium 全程不查
  GetCursorPos / GetKeyState（events_win_utils.cc:154-167, 316-363），因此
  screenX / buttons / pressure 全部与真人一致，且不需要窗口前台。
* 输入桌面的后台键盘使用 SendMessageTimeout 并显式控制 WM_CHAR；独立非输入
  Desktop 使用 PostMessage，让 Chromium 自己通过 TranslateMessage 生成普通字符，
  避免隐藏 Desktop 的窗口过程反向堵住工作进程。
* SetCapture 是 Windows 全局资源，只允许单窗口兼容模式短时使用。多窗口后台协议
  为每个 HWND 维护独立虚拟光标，不争抢全局捕获；检测站仍需持续观察不同 Chrome
  版本是否产生真实光标 pointerout 泄漏。
旧 Drission stealth 通过兼容模块引用这里；禁止在旧目录复制另一份实现。
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time as _time
from contextlib import contextmanager

IS_WINDOWS = True
try:
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
except Exception:  # pragma: no cover - 非 Windows
    IS_WINDOWS = False
    user32 = kernel32 = None


# --------------------------------------------------------------------------- #
# 消息常量
# --------------------------------------------------------------------------- #
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_ACTIVATE = 0x0006
WM_SETFOCUS = 0x0007
WM_MOUSEACTIVATE = 0x0021
WM_INPUTLANGCHANGEREQUEST = 0x0050
WA_ACTIVE = 1

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_SHIFT = 0x0004
MK_CONTROL = 0x0008
MK_MBUTTON = 0x0010
MK_XBUTTON1 = 0x0020
MK_XBUTTON2 = 0x0040

MAPVK_VK_TO_VSC = 0
KF_EXTENDED = 0x0100

VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_DELETE = 0x2E
VK_PACKET = 0xE7

# 扩展键（lParam 需要置 KF_EXTENDED）
EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                          0x2D, 0x2E, 0x0D, 0x6F, 0x90})

SMTO_ABORTIFHUNG = 0x0002
UOI_NAME = 2
DESKTOP_READOBJECTS = 0x0001

BROWSER_TOPLEVEL_PREFIX = 'Chrome_WidgetWin_'
RENDER_WIDGET_CLASS = 'Chrome_RenderWidgetHostHWND'

if IS_WINDOWS:
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
                                           wt.UINT, wt.UINT, ctypes.POINTER(wt.DWORD)]
    user32.SendMessageTimeoutW.restype = wt.LPARAM
    user32.GetThreadDesktop.argtypes = [wt.DWORD]
    user32.GetThreadDesktop.restype = wt.HANDLE
    user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    user32.OpenInputDesktop.restype = wt.HANDLE
    user32.CloseDesktop.argtypes = [wt.HANDLE]
    user32.CloseDesktop.restype = wt.BOOL
    user32.GetUserObjectInformationW.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
    ]
    user32.GetUserObjectInformationW.restype = wt.BOOL
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
    user32.MapVirtualKeyW.restype = wt.UINT
    user32.VkKeyScanW.argtypes = [wt.WCHAR]
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowExW.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.AttachThreadInput.restype = wt.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.GetCursorPos.restype = wt.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.WindowFromPoint.argtypes = [wt.POINT]
    user32.WindowFromPoint.restype = wt.HWND
    user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    user32.GetAncestor.restype = wt.HWND
    user32.SetKeyboardState.argtypes = [ctypes.POINTER(ctypes.c_ubyte * 256)]
    user32.GetKeyboardState.argtypes = [ctypes.POINTER(ctypes.c_ubyte * 256)]
    user32.GetKeyboardLayout.argtypes = [wt.DWORD]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    user32.GetKeyboardLayoutList.restype = ctypes.c_int
    user32.SetCapture.argtypes = [wt.HWND]
    user32.SetCapture.restype = wt.HWND
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
    user32.GetWindow.restype = wt.HWND
    user32.GetCapture.restype = wt.HWND
    user32.ReleaseCapture.restype = wt.BOOL
    user32.GetForegroundWindow.restype = wt.HWND
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ('cbSize', wt.DWORD),
            ('flags', wt.DWORD),
            ('hwndActive', wt.HWND),
            ('hwndFocus', wt.HWND),
            ('hwndCapture', wt.HWND),
            ('hwndMenuOwner', wt.HWND),
            ('hwndMoveSize', wt.HWND),
            ('hwndCaret', wt.HWND),
            ('rcCaret', wt.RECT),
        ]
    user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]
    user32.GetGUIThreadInfo.restype = wt.BOOL
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.WaitForSingleObject.restype = wt.DWORD
    kernel32.ReleaseMutex.argtypes = [wt.HANDLE]
    kernel32.ReleaseMutex.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.GetCurrentThreadId.restype = wt.DWORD
    _EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


# --------------------------------------------------------------------------- #
# DPI
# --------------------------------------------------------------------------- #
_dpi_mode = None


def make_dpi_aware():
    """必须在任何窗口/坐标 API 之前调用，否则系统会做 DPI 虚拟化，坐标全错。"""
    global _dpi_mode
    if _dpi_mode is not None or not IS_WINDOWS:
        return _dpi_mode
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _dpi_mode = 'per-monitor-v2'
            return _dpi_mode
    except Exception:
        pass
    try:
        ctypes.WinDLL('shcore').SetProcessDpiAwareness(2)
        _dpi_mode = 'per-monitor'
        return _dpi_mode
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        _dpi_mode = 'system'
    except Exception:
        _dpi_mode = 'none'
    return _dpi_mode


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def lparam_xy(x, y):
    """坐标打包成 lParam（两个 16 位有符号数）。"""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def foreground_window():
    """当前真实 Windows 前台窗口；页面 ``document.hasFocus`` 不能替代它。"""
    if not IS_WINDOWS:
        return 0
    return int(user32.GetForegroundWindow() or 0)


GUI_INMOVESIZE = 0x0002


def gui_thread_in_move_size(hwnd):
    """目标顶层窗口是否正处在用户拖动/缩放的 Windows 模态循环。"""

    if not IS_WINDOWS or not hwnd:
        return False
    root = int(root_of(hwnd) or hwnd)
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    try:
        if not user32.GetGUIThreadInfo(thread_of(root), ctypes.byref(info)):
            return False
        return bool(info.flags & GUI_INMOVESIZE) or int(info.hwndMoveSize or 0) == root
    except Exception:
        return False


def wait_for_window_geometry_stable(hwnd, timeout=8.0, stable_seconds=0.25):
    """等待目标窗口停止拖动/缩放并保持矩形稳定，返回最终矩形。"""

    root = int(root_of(hwnd) or hwnd)
    last_rect = window_rect(root)
    stable_since = None
    end = _time.monotonic() + max(0.25, float(timeout))
    while _time.monotonic() < end:
        current_rect = window_rect(root)
        state = physical_mouse_state()
        left_down = bool(state and state[2] and state[2][0])
        if gui_thread_in_move_size(root) or left_down or current_rect != last_rect:
            last_rect = current_rect
            stable_since = None
        elif stable_since is None:
            stable_since = _time.monotonic()
        elif _time.monotonic() - stable_since >= max(0.05, float(stable_seconds)):
            return current_rect
        _time.sleep(0.03)
    return window_rect(root)


def wait_for_user_window_move(previous_hwnd, target_hwnd, baseline_rect=None,
                              timeout=8.0, stable_seconds=0.25):
    """识别并等待用户拖动目标 Chrome，避免把主动拖窗误判为自动化抢焦点。

    只有目标浏览器已经成为前台，并且 GUI 线程处于移窗/缩放循环或窗口矩形相对
    本次操作起点发生变化时才成立。普通 PostMessage 意外激活但矩形未变化仍交给
    :func:`restore_foreground_if_target_stole` 处理，不能借此放松焦点保护。
    """

    if not IS_WINDOWS or not previous_hwnd or not target_hwnd:
        return False
    try:
        current = foreground_window()
        target_root = int(root_of(target_hwnd) or target_hwnd)
        if int(root_of(current) or current) != target_root:
            return False
        if int(root_of(previous_hwnd) or previous_hwnd) == target_root:
            return False
        current_rect = window_rect(target_root)
        moved = baseline_rect is not None and tuple(current_rect) != tuple(baseline_rect)
        if not gui_thread_in_move_size(target_root) and not moved:
            return False
    except Exception:
        return False

    end = _time.monotonic() + max(0.25, float(timeout))
    stable_since = None
    last_rect = current_rect
    while _time.monotonic() < end:
        try:
            current_rect = window_rect(target_root)
            state = physical_mouse_state()
            left_down = bool(state and state[2] and state[2][0])
            moving = gui_thread_in_move_size(target_root)
        except Exception:
            break
        if moving or left_down or current_rect != last_rect:
            stable_since = None
            last_rect = current_rect
        elif stable_since is None:
            stable_since = _time.monotonic()
        elif _time.monotonic() - stable_since >= max(0.08, float(stable_seconds)):
            return True
        _time.sleep(0.04)
    # 已确认为用户移窗，即使超过等待上限也不能反向抢走用户当前前台。
    return True


def restore_foreground_if_target_stole(previous_hwnd, target_hwnd):
    """后台注入后若目标浏览器夺走系统前台，立即恢复用户原窗口。

    返回 ``(发生抢焦点, 是否恢复成功)``。只有当前前台确实属于目标浏览器时
    才恢复，用户在动作期间主动切到第三个窗口不会被本函数反向抢回。
    """

    if not IS_WINDOWS or not previous_hwnd or not target_hwnd:
        return False, True
    current = foreground_window()
    if not current or current == int(previous_hwnd):
        return False, True
    try:
        target_root = int(root_of(target_hwnd) or target_hwnd)
        current_root = int(root_of(current) or current)
    except Exception:
        return False, True
    if current_root != target_root:
        return False, True
    if not is_alive(previous_hwnd):
        return True, False

    # SetForegroundWindow 的前台锁以“当前前台线程”为边界。只附加原窗口线程并
    # 不足以取得切换资格；守卫线程必须同时加入当前目标与原窗口两个输入队列。
    current_tid = thread_of(current)
    previous_tid = thread_of(previous_hwnd)
    attached_current = attach_input(current_tid)
    attached_previous = (
        attach_input(previous_tid)
        if previous_tid != current_tid
        else False
    )
    try:
        restored = bool(user32.SetForegroundWindow(previous_hwnd))
    except Exception:
        restored = False
    finally:
        if attached_previous:
            detach_input(previous_tid)
        if attached_current:
            detach_input(current_tid)
    if restored:
        restored = foreground_window() == int(previous_hwnd)
    return True, bool(restored)


def key_lparam(vk, is_up=False, repeat=1):
    """
    键盘 lParam。scan code 必须正确 —— Chromium 用 GetScanCodeFromLParam 推导
    KeyboardEvent.code（events_win_utils.cc:369-372）。
    """
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) if vk else 0
    lp = (repeat & 0xFFFF) | ((scan & 0xFF) << 16)
    if vk in EXTENDED_VKS:
        lp |= KF_EXTENDED << 16
    if is_up:
        lp |= (1 << 30) | (1 << 31)
    return lp


def post(hwnd, msg, wparam, lparam):
    return bool(user32.PostMessageW(hwnd, msg, wt.WPARAM(wparam), wt.LPARAM(lparam)))


def _desktop_name(handle):
    if not IS_WINDOWS or not handle:
        return None
    needed = wt.DWORD(0)
    user32.GetUserObjectInformationW(handle, UOI_NAME, None, 0, ctypes.byref(needed))
    if needed.value <= 0:
        return None
    char_count = max(2, (int(needed.value) + ctypes.sizeof(wt.WCHAR) - 1)
                     // ctypes.sizeof(wt.WCHAR))
    buffer = ctypes.create_unicode_buffer(char_count)
    if not user32.GetUserObjectInformationW(
        handle,
        UOI_NAME,
        buffer,
        ctypes.sizeof(buffer),
        ctypes.byref(needed),
    ):
        return None
    return buffer.value or None


def desktop_name_for_thread(thread_id):
    """返回指定 GUI 线程所属 Desktop 名称；无法确认时返回 ``None``。"""
    if not IS_WINDOWS or not thread_id:
        return None
    return _desktop_name(user32.GetThreadDesktop(int(thread_id)))


def current_desktop_name():
    """返回调用线程所属 Desktop 名称，供隔离进程与浏览器 HWND 做归属核验。"""
    if not IS_WINDOWS:
        return None
    return desktop_name_for_thread(kernel32.GetCurrentThreadId())


def window_desktop_name(hwnd):
    """返回窗口线程所属 Desktop 名称；不会切换或激活该 Desktop。"""
    if not IS_WINDOWS or not hwnd:
        return None
    return desktop_name_for_thread(thread_of(hwnd))


def is_input_desktop():
    """当前线程是否属于用户此刻可见并接收真实输入的 Desktop。"""
    if not IS_WINDOWS:
        return True
    current = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    input_desktop = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not current or not input_desktop:
        return True
    try:
        current_name = _desktop_name(current)
        input_name = _desktop_name(input_desktop)
        if current_name is None or input_name is None:
            return True
        return current_name.casefold() == input_name.casefold()
    finally:
        user32.CloseDesktop(input_desktop)


def send(hwnd, msg, wparam, lparam, timeout_ms=500):
    """
    SendMessageTimeout。后台多窗不能让单条消息卡住 2 秒并按字符累计；500ms 内
    目标窗口仍无响应就失败关闭，由上层做整次输入校验/重试。
    """
    res = wt.DWORD()
    return bool(user32.SendMessageTimeoutW(
        hwnd, msg, wt.WPARAM(wparam), wt.LPARAM(lparam),
        SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(res)))


def prepare_background_keyboard(hwnd):
    """刷新 Chromium 当前 HWND 的进程内键盘焦点，不抢 Windows 前台。

    后台鼠标点击通常会由 Chromium 的 ``SetKeyboardFocus`` 把焦点交给页面，
    但多进程并发或用户刚移动另一个 Chrome 时，DOM ``activeElement`` 可能正确，
    顶层 Widget 却仍丢弃随后整段键盘消息。这里只向目标 HWND 同步发送窗口消息；
    禁止调用 ``SetForegroundWindow``、``SetFocus`` 或修改真实系统键态。
    """

    if not IS_WINDOWS or not hwnd:
        return False
    root = int(root_of(hwnd) or hwnd)
    if not send(hwnd, WM_MOUSEACTIVATE, root, 0):
        return False
    if not send(root, WM_ACTIVATE, WA_ACTIVE, hwnd):
        return False
    if not send(hwnd, WM_SETFOCUS, 0, 0):
        return False
    return True


def vk_scan(ch):
    """返回 (vk, need_shift)；本键盘布局打不出的字符（中文等）返回 (None, False)。"""
    v = user32.VkKeyScanW(ch)
    if v == -1:
        return None, False
    return v & 0xFF, bool((v >> 8) & 1)


def text_vk_scan(ch, caps_lock=False):
    """仅文字输入补偿 Caps Lock；数字、符号和显式组合键保持原映射。"""
    vk, shift = vk_scan(ch)
    if vk is not None and ('a' <= ch <= 'z' or 'A' <= ch <= 'Z'):
        shift = bool(shift) != bool(caps_lock)
    return vk, shift


def target_caps_lock(tid):
    """读取目标输入队列的锁定状态，不发送 Caps Lock，也不读取另一桌面。"""
    if not attach_input(tid):
        raise RuntimeError('无法读取目标窗口的大小写锁定状态')
    try:
        state = (ctypes.c_ubyte * 256)()
        if not user32.GetKeyboardState(ctypes.byref(state)):
            raise RuntimeError('读取目标窗口键盘状态失败')
        return bool(state[0x14] & 1)
    finally:
        detach_input(tid)


_layout_locks_guard = threading.RLock()
_layout_locks = {}


def _keyboard_layout_value(layout):
    """把 ctypes HKL/整数统一成可比较、可投递的整数。"""

    if isinstance(layout, int):
        return int(layout)
    return int(getattr(layout, "value", 0) or 0)


def keyboard_layout(thread_id):
    """读取指定窗口线程当前的键盘布局；失败返回 0。"""

    if not IS_WINDOWS or not thread_id:
        return 0
    try:
        return _keyboard_layout_value(user32.GetKeyboardLayout(int(thread_id)))
    except Exception:
        return 0


def find_keyboard_layout(language_id=0x0409):
    """
    从系统已安装布局中查找指定语言，不调用 LoadKeyboardLayoutW。

    Windows 8 以后 LoadKeyboardLayoutW 可能改变系统级活动布局；后台多窗口不能为
    一个浏览器输入动作触碰该全局状态。这里只复用已经安装的 en-US 布局，再把切换
    请求只投递给目标 HWND 所在线程。
    """

    if not IS_WINDOWS:
        return 0
    try:
        count = int(user32.GetKeyboardLayoutList(0, None) or 0)
        if count <= 0:
            return 0
        items = (ctypes.c_void_p * count)()
        count = int(user32.GetKeyboardLayoutList(count, items) or 0)
        for item in items[:count]:
            value = _keyboard_layout_value(item)
            if value and (value & 0xFFFF) == int(language_id):
                return value
    except Exception:
        return 0
    return 0


def _layout_lock(thread_id):
    """键盘布局属于线程；只串行同一目标线程，不阻塞其它浏览器窗口。"""

    with _layout_locks_guard:
        lock = _layout_locks.get(int(thread_id))
        if lock is None:
            lock = _layout_locks[int(thread_id)] = threading.RLock()
        return lock


def _wait_keyboard_layout(thread_id, expected, timeout=0.25):
    end = _time.monotonic() + max(0.01, float(timeout))
    while _time.monotonic() < end:
        if keyboard_layout(thread_id) == int(expected):
            return True
        _time.sleep(0.01)
    return keyboard_layout(thread_id) == int(expected)


@contextmanager
def temporary_keyboard_layout(hwnd, language_id=0x0409):
    """
    仅在本次输入期间切换目标 HWND 线程的键盘布局，结束后恢复。

    返回值表示是否已经处于/成功切到目标布局。切换使用官方
    WM_INPUTLANGCHANGEREQUEST，由目标窗口的 DefWindowProc 在目标线程内应用；不会
    激活浏览器、不会修改用户当前 Hi 窗口线程，也不使用系统级 LoadKeyboardLayout。
    """

    thread_id = int(thread_of(hwnd) or 0)
    if not IS_WINDOWS or not hwnd or not thread_id:
        yield False
        return
    with _layout_lock(thread_id):
        old = keyboard_layout(thread_id)
        wanted = find_keyboard_layout(language_id)
        changed = False
        ready = bool(old and wanted and old == wanted)
        try:
            if old and wanted and old != wanted:
                changed = post(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, wanted)
                ready = bool(changed and _wait_keyboard_layout(thread_id, wanted))
            yield ready
        finally:
            if changed and old:
                try:
                    post(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, old)
                    _wait_keyboard_layout(thread_id, old)
                except Exception:
                    pass


def get_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def client_size(hwnd):
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def window_rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def client_to_screen(hwnd, x, y):
    p = wt.POINT(int(x), int(y))
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y


def is_alive(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


GA_ROOT = 2


def window_from_screen_point(sx, sy):
    return user32.WindowFromPoint(wt.POINT(int(sx), int(sy)))


def root_of(hwnd):
    return user32.GetAncestor(hwnd, GA_ROOT)


def point_is_on_window(hwnd, sx, sy):
    """
    屏幕点 (sx,sy) 处最顶层的窗口是否就是 hwnd（或它的子窗口）。

    滚轮必须满足这个条件才有效：ui::RerouteMouseWheel
    (ui/base/win/mouse_wheel_util.cc:72-122) 会用 WindowFromPoint(lParam) 找出该点
    上的窗口，只有它等于消息目标窗口时才返回 false 让 Chrome 正常处理；
    落在别的进程的窗口上就直接把消息丢掉。
    """
    w = window_from_screen_point(sx, sy)
    if not w:
        return False
    return w == hwnd or root_of(w) == root_of(hwnd)


def thread_of(hwnd):
    return user32.GetWindowThreadProcessId(hwnd, None)


def process_of(hwnd):
    """返回 HWND 当前所属 PID；句柄失效时返回 0。"""

    if not IS_WINDOWS or not hwnd:
        return 0
    process_id = wt.DWORD()
    try:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    except Exception:
        return 0
    return int(process_id.value or 0)


# --------------------------------------------------------------------------- #
# 窗口查找
# --------------------------------------------------------------------------- #
def toplevels_of_pid(pid):
    return toplevels_of_pids({pid})


def toplevels_of_pids(process_ids):
    """枚举进程集合拥有的全部顶层窗口。"""

    wanted = {int(pid) for pid in process_ids if pid}
    out = []

    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value in wanted:
            out.append(hwnd)
        return True

    if not wanted:
        return out
    user32.EnumWindows(_EnumProc(cb), 0)
    return out


def process_family_ids(pid, parent_depth=4):
    """返回调试端口进程、全部子进程及有限层级父进程。

    指纹浏览器常由子进程监听调试端口，而真正的顶层窗口属于父进程；只按
    DrissionPage 暴露的单个 PID 枚举会漏掉真实窗口。父进程只向上有限追溯，
    最终仍由页面坐标校准确认 HWND，不能仅凭进程关系误绑定。
    """

    try:
        root_pid = int(pid)
    except (TypeError, ValueError):
        return set()
    if root_pid <= 0:
        return set()
    result = {root_pid}
    try:
        import psutil

        process = psutil.Process(root_pid)
        result.update(
            int(child.pid)
            for child in process.children(recursive=True)
            if getattr(child, 'pid', None)
        )
        for _ in range(max(0, int(parent_depth))):
            process = process.parent()
            if process is None:
                break
            result.add(int(process.pid))
    except Exception:
        # 进程可能恰好退出或当前账户无权读取；至少保留 CDP 返回的原始 PID。
        pass
    return result


def find_content_window(pid, want_client=None):
    """
    找网页内容窗口。

    返回 (hwnd, kind)：
      kind == 'legacy'   -> Chrome_RenderWidgetHostHWND，客户区就是视口
      kind == 'toplevel' -> Chrome_WidgetWin_N，客户区含浏览器 UI，需要偏移

    实测 AdsPower/SunBrowser 不创建 legacy 窗口（它只在需要屏幕阅读器时才建，见
    ui/base/win/internal_constants.h:17-20），所以 toplevel 是常态路径。
    偏移量不要硬算，交给 _session 的注入回读自校准。
    """
    cands = []
    for top in toplevels_of_pid(pid):
        if not user32.IsWindowVisible(top):
            continue
        if not get_class(top).startswith(BROWSER_TOPLEVEL_PREFIX):
            continue
        w, h = client_size(top)
        if w <= 1 or h <= 1:
            continue
        child = user32.FindWindowExW(top, None, RENDER_WIDGET_CLASS, None)
        if child and user32.IsWindowVisible(child):
            cands.append((child, 'legacy', top, client_size(child)))
        else:
            cands.append((top, 'toplevel', top, (w, h)))

    if not cands:
        return None, None

    if want_client:
        ww, wh = want_client
        # 优先选客户区宽度与视口宽度吻合的窗口（多窗口时用来区分）
        cands.sort(key=lambda c: abs(c[3][0] - ww) + max(0, c[3][1] - wh))
    return cands[0][0], cands[0][1]


def describe_windows(pid, max_depth=2):
    """诊断用：dump 进程的窗口树。"""
    rows = []

    def walk(h, d):
        rows.append({'hwnd': h, 'depth': d, 'cls': get_class(h), 'title': get_title(h),
                     'visible': bool(user32.IsWindowVisible(h)), 'client': client_size(h)})
        if d >= max_depth:
            return
        kids = []

        def cb(c, _):
            kids.append(c)
            return True

        user32.EnumChildWindows(h, _EnumProc(cb), 0)
        for k in kids:
            walk(k, d + 1)

    for t in toplevels_of_pid(pid):
        walk(t, 0)
    return rows


def descendant_windows(hwnd):
    """返回 ``hwnd`` 下的全部后代窗口，保持 Win32 枚举顺序并去重。"""
    result = []
    seen = set()

    def cb(child, _):
        value = int(child)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
        return True

    user32.EnumChildWindows(hwnd, _EnumProc(cb), 0)
    return result


# --------------------------------------------------------------------------- #
# 共享键态（用于 Shift 组合键）
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 输入队列附加 / 鼠标捕获
# --------------------------------------------------------------------------- #
_attach_lock = threading.RLock()
_attach_refs = {}          # (本线程 tid, 目标 tid) -> 计数


def attach_input(tid):
    """
    带引用计数的 AttachThreadInput。

    Win32 的 AttachThreadInput 本身不是引用计数的 —— 任何一次 FALSE 都会立刻断开，
    于是「键盘的 SharedKeyState」和「鼠标的 arm_capture」会互相拆台。这里统一收口。
    """
    if not IS_WINDOWS or not tid:
        return False
    me = kernel32.GetCurrentThreadId()
    if tid == me:
        return False
    key = (me, tid)
    with _attach_lock:
        n = _attach_refs.get(key, 0)
        if n > 0:
            _attach_refs[key] = n + 1
            return True
        if not user32.AttachThreadInput(me, tid, True):
            return False
        _attach_refs[key] = 1
        return True


def detach_input(tid):
    if not IS_WINDOWS or not tid:
        return
    me = kernel32.GetCurrentThreadId()
    key = (me, tid)
    with _attach_lock:
        n = _attach_refs.get(key, 0)
        if n <= 0:
            return
        if n > 1:
            _attach_refs[key] = n - 1
            return
        _attach_refs.pop(key, None)
        user32.AttachThreadInput(me, tid, False)


class CaptureKeeper(object):
    """
    专职线程，长期把鼠标捕获挂在目标窗口上。

    为什么必须长期持有、且必须由固定线程持有
    ----------------------------------------
    捕获归"调用 SetCapture 的那个线程"所有。注入线程若用完就 AttachThreadInput(FALSE)，
    等于放手 —— 而持有捕获期间 Windows 只是把 WM_MOUSELEAVE **推迟**、并没有取消，
    一放手就立刻补发一条。实测表现为"每个操作恰好漏 1 个"，连一次移动都没有的
    滚轮操作也照漏。所以捕获必须由一个始终保持附加的线程持有。

    Chromium 处理 mouseup 时会自己 ReleaseCapture（hwnd_message_handler 的按键路径），
    所以还要能在抬起后把它夺回来 —— after_release() 做这件事，并且是等到
    "确实观察到 Chromium 释放了"之后才夺，避免抢在它前面白做一次。
    """

    POLL_IDLE = 0.05        # 平时自检间隔
    POLL_BUSY = 0.003       # 抬起按钮后的高频自检间隔

    #: 连续这么多次"捕获又掉了、得重抢"就判定抢不住，主动停掉自己。
    #: Win32 只保证前台窗口能可靠持有捕获；后台窗口会被系统反复收回，
    #: 我们再去抢就会产生 WM_CAPTURECHANGED 抖动，把注入的鼠标消息搅掉。
    #: 宁可退化成"有泄漏但注入正常"，也不能让注入失效。
    THRASH_LIMIT = 6

    def __init__(self, hwnd, tid=None):
        self.hwnd = hwnd
        self.tid = tid if tid is not None else thread_of(hwnd)
        self.held = threading.Event()
        self.ok = False
        self.degraded = False       # 抢不住，已主动放弃
        self.regrabs = 0            # 累计重抢次数（诊断用）
        self._thrash = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._want_restore = threading.Event()
        self._restored = threading.Event()
        self._thread = None

    # -------------------------------------------------------------- #
    def start(self):
        if not IS_WINDOWS or not self.hwnd or not self.tid or self._thread:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='stealth-capture-%X' % self.hwnd)
        self._thread.start()
        self.held.wait(0.5)          # 等它把捕获挂上去再返回
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(1.0)

    def after_release(self, timeout=0.25):
        """
        抬起按钮之后调用：等 Chromium 真的 ReleaseCapture 了，再把捕获夺回来。
        期间不要投递任何移动 —— 那正是会漏的窗口。
        """
        if not self.ok or self.degraded:
            return False
        self._restored.clear()
        self._want_restore.set()
        self._wake.set()
        return self._restored.wait(timeout)

    # -------------------------------------------------------------- #
    def _grab(self, count_thrash=True):
        if self.degraded:
            return False
        try:
            cur = user32.GetCapture()
            if cur == self.hwnd:
                self._thrash = 0          # 稳住了，清零
                self.held.set()
                return True
            if cur:                       # 别人持有（原生菜单等），不抢
                self.held.clear()
                return False
            user32.SetCapture(self.hwnd)
            good = user32.GetCapture() == self.hwnd
            self.regrabs += 1
            if good:
                self.held.set()
            else:
                self.held.clear()
            if count_thrash:
                # 平时自检时还需要重抢，说明系统一直在收回 —— 抢不住就别抢了
                self._thrash += 1
                if self._thrash >= self.THRASH_LIMIT:
                    self._give_up()
            return good
        except Exception:
            self.held.clear()
            return False

    def _give_up(self):
        """抢不住就主动放手，让注入回到"有泄漏但完全可用"的状态。"""
        self.degraded = True
        self.held.clear()
        try:
            if user32.GetCapture() == self.hwnd:
                user32.ReleaseCapture()
        except Exception:
            pass

    def _run(self):
        if not attach_input(self.tid):
            return
        self.ok = True
        try:
            self._grab(count_thrash=False)      # 首次夺取不算抖动
            while not self._stop.is_set() and not self.degraded:
                if self._want_restore.is_set():
                    self._want_restore.clear()
                    # 先等 Chromium 那边真的放手，再夺回。
                    # 这是我们自己点击造成的释放，属于预期内，不计入抖动。
                    end = _time.time() + 0.2
                    while _time.time() < end and user32.GetCapture() == self.hwnd:
                        _time.sleep(self.POLL_BUSY)
                    self._grab(count_thrash=False)
                    self._restored.set()
                    busy_until = _time.time() + 0.15
                else:
                    busy_until = 0
                while _time.time() < busy_until and not self._stop.is_set():
                    self._grab(count_thrash=False)
                    _time.sleep(self.POLL_BUSY)
                self._grab()                   # 平时自检，要计抖动
                self._wake.wait(self.POLL_IDLE)
                self._wake.clear()
            if self.degraded:                  # 放弃后线程留着但不再抢
                while not self._stop.wait(0.25):
                    pass
        finally:
            try:
                if user32.GetCapture() == self.hwnd:
                    user32.ReleaseCapture()
            except Exception:
                pass
            self.held.clear()
            self.ok = False
            detach_input(self.tid)


def arm_capture(hwnd, tid=None):
    """
    把鼠标捕获挂到目标窗口上，堵住「pointerout 泄漏真实光标位置」。

    为什么需要
    ----------
    Chromium 只在 !HasCapture() 时才给窗口注册 TME_LEAVE：
        hwnd_message_handler.cc:3511-3518   if (kMouseMoved && !HasCapture() && track_mouse)
        hwnd_message_handler.cc:1007-1009   HasCapture() { return ::GetCapture() == hwnd(); }
    一旦注册，而真实光标又不在该窗口上，Windows 立刻回一条 WM_MOUSELEAVE；
    Chromium 处理它时坐标不是取自消息，而是现场调 ::GetCursorPos()：
        events_win_utils.cc:314-333
    于是页面每收到一个我们注入的 pointermove，就跟着收到一个
    clientX/clientY == 真实光标位置 的 pointerout —— 直接把系统光标喂给网页。

    为什么是这个办法
    ----------------
    同一个 if 的三个条件里：
      * event.type()  —— 给 wParam 加按键位可变成 kMouseDragged 从而跳过注册，
                         但 Chrome 149 会把 MK_XBUTTON1/2 映射进 DOM 的 buttons
                         （实测 buttons=8 / 16），等于换了个泄漏，不可用。
      * track_mouse   —— 投给 legacy 子窗口才是 false，而 AdsPower 固定带
                         --disable-legacy-window，该窗口永远不会被创建。
      * HasCapture()  —— 可行，就是这里。
    跨进程 TrackMouseEvent(TME_CANCEL) 实测返回 TRUE 但对目标窗口无效，已排除。

    实现要点
    --------
    * capture 在 AttachThreadInput(FALSE) 之后仍留在目标输入队列上，所以这里
      不保留任何长期附加状态，只做 attach -> SetCapture -> detach 三个调用。
    * Chromium 处理 mouseup 时会 ReleaseCapture，所以每次鼠标操作前都要重新
      arm 一次。幂等、开销可忽略。
    * 别人（如原生弹出菜单）正持有 capture 时不抢，直接放弃本次。

    实测（AdsPower / Chrome 149.0.7827.54，真实光标在浏览器窗口之外）
        不 arm ：每个注入 move 跟一个越界 pointerout（49 / 50 个）
        arm 后 ：只剩 8 个真实元素边界事件，越界 0 个，连续 5 轮合计泄漏 0
    """
    if not IS_WINDOWS or not hwnd:
        return False
    if tid is None:
        tid = thread_of(hwnd)
    if not attach_input(tid):
        return False
    try:
        cur = user32.GetCapture()
        if cur and cur != hwnd:
            return False               # 别人正持有，不抢
        if cur != hwnd:
            user32.SetCapture(hwnd)
        return user32.GetCapture() == hwnd
    except Exception:
        return False
    finally:
        detach_input(tid)


def rearm_capture_after_release(hwnd, tid=None, timeout=0.25):
    """等待 Chromium 处理 mouseup 并释放捕获，再为同一次复合动作重新捕获。"""
    if not IS_WINDOWS or not hwnd:
        return False
    if tid is None:
        tid = thread_of(hwnd)
    if not attach_input(tid):
        return False
    try:
        end = _time.monotonic() + max(0.01, float(timeout))
        while user32.GetCapture() == hwnd and _time.monotonic() < end:
            _time.sleep(0.005)
        current = user32.GetCapture()
        if current and current != hwnd:
            return False
        if current != hwnd:
            user32.SetCapture(hwnd)
        return user32.GetCapture() == hwnd
    except Exception:
        return False
    finally:
        detach_input(tid)


def release_capture(hwnd, tid=None):
    """还原：把 arm_capture 挂上去的捕获放掉（revert / 会话结束时用）。"""
    if not IS_WINDOWS or not hwnd:
        return False
    if tid is None:
        tid = thread_of(hwnd)
    if not attach_input(tid):
        return False
    try:
        if user32.GetCapture() == hwnd:
            user32.ReleaseCapture()
            return True
        return False
    except Exception:
        return False
    finally:
        detach_input(tid)


def physical_mouse_state():
    """读取真实系统光标和物理按钮状态；PostMessage 注入不会改变这些值。"""
    if not IS_WINDOWS:
        return None
    point = wt.POINT()
    try:
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        buttons = tuple(
            bool(user32.GetAsyncKeyState(vk) & 0x8000)
            for vk in (0x01, 0x02, 0x04, 0x05, 0x06)
        )
        return int(point.x), int(point.y), buttons
    except Exception:
        return None


def physical_keyboard_active():
    """是否有真实键盘按键正被按住；SendMessage/PostMessage 注入不会改变该状态。"""
    if not IS_WINDOWS:
        return False
    try:
        # 排除鼠标键；覆盖控制键、字母数字、功能键、方向键和 OEM 标点键。
        return any(
            user32.GetAsyncKeyState(vk) & 0x8000
            for vk in range(0x08, 0xFF)
            if vk not in (0x0E, 0x0F)
        )
    except Exception:
        return False


SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
SWP_NOACTIVATE = 0x0010
_HWND_TOP, _HWND_TOPMOST, _HWND_NOTOPMOST = 0, -1, -2
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008

#: 提升 Z 序是全局副作用，多个会话同时抢会互相打架，串行化。
_zorder_lock = threading.RLock()
_ZORDER_MUTEX_NAME = 'Local\\AutoDrissionPageAI.Win32DP.WheelZOrder'


class _CrossProcessMutex(object):
    """只给 Windows 桌面级资源使用的有界跨进程互斥。"""

    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_ABANDONED = 0x00000080

    def __init__(self, name, timeout_ms=5000):
        self.name = name
        self.timeout_ms = max(1, int(timeout_ms))
        self.handle = None
        self.held = False

    def __enter__(self):
        if not IS_WINDOWS:
            self.held = True
            return self
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise RuntimeError('无法创建 Windows 跨进程输入互斥')
        wait = int(kernel32.WaitForSingleObject(self.handle, self.timeout_ms))
        self.held = wait in (self._WAIT_OBJECT_0, self._WAIT_ABANDONED)
        if not self.held:
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError('等待 Windows 跨进程输入互斥超时')
        return self

    def __exit__(self, *exc):
        try:
            if self.held and self.handle:
                kernel32.ReleaseMutex(self.handle)
        finally:
            if self.handle:
                kernel32.CloseHandle(self.handle)
            self.handle = None
            self.held = False
        return False


class raise_for_wheel(object):
    """
    临时把窗口提到 Z 序最上层（不激活、不移动、不改大小），退出时还原。

    为什么滚轮需要这个
    ------------------
    Chromium 的 ui::RerouteMouseWheel（mouse_wheel_util.cc:81-100）用
    WindowFromPoint(滚轮坐标) 决定目标窗口；若该点落在【别的进程的顶层窗口】上，
    就走这一步：
        if (!WindowSupportsRerouteMouseWheel(window_under_wheel)) return true;  // 丢弃
    而 WindowSupportsRerouteMouseWheel 的循环条件是 GWL_STYLE & WS_CHILD ——
    顶层窗口直接返回 false。多开时窗口互相叠着，除了最上面那个，其余的滚轮
    消息全部被丢弃（实测 4 开时 4 个浏览器无一能滚）。

    移动和点击不受影响（它们不走 RerouteMouseWheel），所以只有滚轮要这么做。
    只改 Z 序、不抢焦点，页面侧观测不到任何差异。
    """

    def __init__(self, hwnd, verify=None):
        """verify(hwnd) -> bool：判断"这个点现在归我了没"。提到普通顶层还不够时
        （屏幕上存在 WS_EX_TOPMOST 窗口），据此决定要不要升级成 TOPMOST。"""
        self.hwnd = hwnd
        self.verify = verify
        self.prev = None
        self.was_topmost = False
        self.made_topmost = False
        self.ok = False
        self._process_mutex = None

    def __enter__(self):
        if not IS_WINDOWS or not self.hwnd:
            return self
        _zorder_lock.acquire()
        try:
            # Z 序属于同一个 Windows 桌面，不属于某个 Python 进程。进程内 RLock
            # 只能挡住线程，多个执行 worker 仍会同时把不同 Chrome 提到最上层，
            # 造成滚轮路由和后续元素坐标错乱。这里只串行滚轮所需的短 Z 序租约；
            # 普通轨迹、点击、键盘仍由各 HWND 独立并发。
            self._process_mutex = _CrossProcessMutex(_ZORDER_MUTEX_NAME)
            self._process_mutex.__enter__()
            self.was_topmost = bool(
                user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
            # GW_HWNDPREV(3) 拿到 Z 序里紧邻其上的窗口，退出时用它还原
            self.prev = user32.GetWindow(self.hwnd, 3)
            self.ok = bool(user32.SetWindowPos(
                self.hwnd, wt.HWND(_HWND_TOP), 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))
            # 屏幕上若有 WS_EX_TOPMOST 的窗口（别的程序、或本库前台模式置顶过的
            # 浏览器），普通 HWND_TOP 压不过去，必须临时升级成 TOPMOST。
            if self.ok and not self.was_topmost and self.verify and not self.verify(self.hwnd):
                if user32.SetWindowPos(self.hwnd, wt.HWND(_HWND_TOPMOST), 0, 0, 0, 0,
                                       SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE):
                    self.made_topmost = True
        except Exception:
            self.ok = False
            if self._process_mutex is not None:
                self._process_mutex.__exit__(None, None, None)
                self._process_mutex = None
            _zorder_lock.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.made_topmost:
                user32.SetWindowPos(self.hwnd, wt.HWND(_HWND_NOTOPMOST), 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            if self.ok and self.prev and not self.was_topmost:
                user32.SetWindowPos(self.hwnd, wt.HWND(self.prev), 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass
        finally:
            try:
                if self._process_mutex is not None:
                    self._process_mutex.__exit__(*exc)
                    self._process_mutex = None
            except Exception:
                pass
            try:
                _zorder_lock.release()
            except Exception:
                pass
        return False


class SharedKeyState(object):
    """
    AttachThreadInput + SetKeyboardState。

    Chromium 键盘事件的修饰键全部取自 GetModifiersFromKeyState() ->
    ::GetKeyState()（events_win_utils.cc:127-150, system_event_state_lookup.cc:12-43），
    读的是目标线程输入队列的键态表；PostMessage/SendMessage 都不会更新它。
    附加输入队列后 SetKeyboardState 才能让目标线程读到我们设置的 Shift。

    实测：30/30 轮事件形态与真实键盘逐字段一致，且 NumLock 保持真实值。
    但存在偶发竞态（首字符可能丢、键态可能残留污染下一字符），所以
    (a) settle 延时不能省，(b) 上层必须回读校验并重试。
    """

    #: 修饰键 -> 需要一起按下的 VK（含左右键位，否则 GetKeyState(VK_SHIFT) 读不到）
    MODS = {'shift': (VK_SHIFT, VK_LSHIFT),
            'ctrl': (VK_CONTROL, 0xA2),
            'alt': (VK_MENU, 0xA4),
            'meta': (VK_LWIN,)}
    # generic 与左右键位全部清理；只清左键会留下 RCtrl/RAlt 等按下态。
    _MODIFIER_VKS = (VK_SHIFT, VK_CONTROL, VK_MENU,
                     0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, VK_LWIN, 0x5C)

    #: 全局串行。SetKeyboardState 改的是目标线程输入队列的键态表，而
    #: AttachThreadInput 会把多个线程的输入状态并到一起 —— 多开并发时几个会话
    #: 同时进出这段代码，Shift 键态会互相冲掉：实测 4 开并发时出现过
    #: keydown.key='4' 但插入的字符是 '$'（Shift 没来得及生效就被 Chromium 读了），
    #: 这种 key 与实际字符不一致是能被直接检测出来的。
    _GLOBAL = threading.RLock()
    _MUTEX_NAME = 'Local\\AutoDrissionPageAI.Win32DP.SharedKeyState'
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_ABANDONED = 0x00000080
    # 只锁“一个 Shift 字符”的键态准备。10 窗口同时输入符号时，2 秒会因
    # Windows 互斥调度不保证公平而误超时；30 秒仍是有界等待，进程异常退出时
    # named mutex 会以 WAIT_ABANDONED 交给下一槽，不会形成永久死锁。
    _WAIT_TIMEOUT_MS = 30_000

    def __init__(self, tid, mods=('shift',), settle=0.03, text_char=None):
        self.tid = tid
        self.mods = tuple(mods)
        self.text_char = text_char
        self.shift_needed = False
        self.settle = settle
        self._me = None
        self._saved = None
        self._held_global = False
        self._mutex = None
        self._held_mutex = False
        self.ok = False

    def __enter__(self):
        import time
        self._GLOBAL.acquire()
        self._held_global = True
        if IS_WINDOWS:
            self._mutex = kernel32.CreateMutexW(None, False, self._MUTEX_NAME)
            if not self._mutex:
                return self
            wait = int(kernel32.WaitForSingleObject(
                self._mutex,
                self._WAIT_TIMEOUT_MS,
            ))
            if wait not in (self._WAIT_OBJECT_0, self._WAIT_ABANDONED):
                kernel32.CloseHandle(self._mutex)
                self._mutex = None
                return self
            self._held_mutex = True
        self._me = kernel32.GetCurrentThreadId()
        # 走引用计数版本，避免与 arm_capture 互相拆掉附加状态
        if not attach_input(self.tid):
            self._me = None
            return self
        time.sleep(self.settle)
        saved = (ctypes.c_ubyte * 256)()
        if not user32.GetKeyboardState(ctypes.byref(saved)):
            return self
        if self.text_char is not None:
            _, self.shift_needed = text_vk_scan(self.text_char, bool(saved[0x14] & 1))
            self.mods = ('shift',) if self.shift_needed else ()
        st = (ctypes.c_ubyte * 256)()
        ctypes.memmove(st, saved, 256)
        # 请求 Shift 并不等于 Shift + 读到的残留 Ctrl。只在本次已附加队列的
        # 临时表中设置明确请求的修饰键，不注入系统按键；退出仍恢复完整 saved。
        # NumLock/CapsLock 等非修饰键状态不改动。
        for vk in self._MODIFIER_VKS:
            st[vk] = 0
        for m in self.mods:
            for vk in self.MODS.get(m, ()):
                st[vk] = 0x80
        if user32.SetKeyboardState(ctypes.byref(st)):
            self._saved = saved
            self.ok = True
            time.sleep(self.settle)
        return self

    def __exit__(self, *exc):
        import time
        try:
            if self._saved is not None:
                user32.SetKeyboardState(ctypes.byref(self._saved))
                time.sleep(self.settle)
        finally:
            if self._me is not None:
                detach_input(self.tid)
                time.sleep(self.settle)
            if self._held_mutex and self._mutex:
                self._held_mutex = False
                try:
                    kernel32.ReleaseMutex(self._mutex)
                except Exception:
                    pass
            if self._mutex:
                try:
                    kernel32.CloseHandle(self._mutex)
                except Exception:
                    pass
                self._mutex = None
            if self._held_global:
                self._held_global = False
                try:
                    self._GLOBAL.release()
                except Exception:
                    pass
        self.ok = False
        return False
