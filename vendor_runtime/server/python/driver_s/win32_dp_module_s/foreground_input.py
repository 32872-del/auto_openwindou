# -*- coding: utf-8 -*-
"""
前台键鼠注入（SendInput）。

与 _input.py 的后台模式接口完全一致，由 _session 按 cfg.input_mode 二选一，
上层补丁（patch_input / patch_ui / behavior）不需要任何改动。

两种模式的取舍
--------------
                    后台 (background)          前台 (foreground)
  注入方式          PostMessage/SendMessage    SendInput（真实输入流）
  真实光标          不动                       跟着一起走
  窗口              不抢系统前台焦点            必须前台且未最小化
  并发              不同窗口可并发              同一时刻只能一个
  你能否同时用电脑  能                         不能，鼠标键盘被占用
  WM_MOUSELEAVE     需要 CaptureKeeper 压制    天然没有（光标就在窗口上）

为什么前台模式天然没有那个泄漏
------------------------------
Chromium 处理 WM_MOUSELEAVE 时先做早退判断（hwnd_message_handler.cc:3493-3505）：
    if (hwnd() == ::WindowFromPoint(::GetCursorPos()) || IsChild(...)) return 0;
前台模式下真实光标就在窗口上，这条永远成立 —— 根本不会产生 kMouseExited。

关于 SendInput 的"注入"标记
---------------------------
SendInput 会给事件打 LLMHF_INJECTED，但那只有本机低级钩子（WH_MOUSE_LL）看得到，
网页侧拿不到任何相关字段。Chromium 唯一检查 GetMessageExtraInfo 的地方是
IsSynthesizedMouseMessage —— 只判 MOUSEEVENTF_FROMTOUCH，我们不带这个位。
"""
import random
import threading
import time
from contextlib import contextmanager

import ctypes
import ctypes.wintypes as wt

from . import cursor_trajectory as hz
from . import win32_native as W
from .foreground_recovery import foreground_lease, owns_foreground_lease
from .mobile_motion import travel_mobile

user32 = W.user32

# --------------------------------------------------------------------------- #
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

SW_RESTORE = 9
SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD),
                ('dwFlags', wt.DWORD), ('time', wt.DWORD),
                ('dwExtraInfo', ctypes.POINTER(wt.ULONG))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.POINTER(wt.ULONG))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wt.DWORD), ('wParamL', wt.WORD), ('wParamH', wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('mi', _MOUSEINPUT), ('ki', _KEYBDINPUT), ('hi', _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', wt.DWORD), ('u', _INPUTUNION)]


if W.IS_WINDOWS:
    user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wt.UINT
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetForegroundWindow.restype = wt.HWND
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wt.HWND]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]


def _send(*items):
    n = len(items)
    arr = (INPUT * n)(*items)
    return user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    i = INPUT()
    i.type = INPUT_MOUSE
    i.u.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, None)
    return i


def _key(vk, scan, flags):
    i = INPUT()
    i.type = INPUT_KEYBOARD
    i.u.ki = _KEYBDINPUT(vk, scan, flags, 0, None)
    return i


def _abs_xy(sx, sy):
    """屏幕坐标 -> SendInput 的 0..65535 归一化虚拟桌面坐标。"""
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or 1
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or 1
    # +0.5 是为了让归一化后再还原回像素时落在目标像素中心，否则会系统性偏 1px
    ax = int(round((sx - vx + 0.5) * 65535.0 / vw))
    ay = int(round((sy - vy + 0.5) * 65535.0 / vh))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008


def is_topmost(hwnd):
    try:
        return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    except Exception:
        return False


def set_topmost(hwnd, on=True):
    """
    把窗口钉在最上层。

    这是前台模式的关键前提 —— 只要有别的窗口盖住它，Chromium 的原生窗口遮挡
    跟踪就会把页面标成 hidden，此时它不会把输入交给渲染进程，注入直接失效
    （实测 visibilityState=hidden 时标定必然拿不到 pointermove）。
    置顶用 SWP_NOACTIVATE，只改 Z 序、不抢焦点。
    """
    if not W.IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(user32.SetWindowPos(
            hwnd, wt.HWND(HWND_TOPMOST if on else HWND_NOTOPMOST), 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | (SWP_SHOWWINDOW if on else 0)))
    except Exception:
        return False


def force_foreground(hwnd, timeout=1.0, topmost=True):
    """
    把窗口切到前台（可选同时置顶）。SetForegroundWindow 有前台锁限制，
    标准做法是先把当前前台窗口的输入队列附加过来。附加走 _win 的引用计数版本，
    避免和别处互相拆台。
    """
    if not W.IS_WINDOWS or not hwnd:
        return False
    hwnd = W.root_of(hwnd) or hwnd
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pass
    if topmost and not is_topmost(hwnd):
        set_topmost(hwnd, True)
    if user32.GetForegroundWindow() == hwnd:
        return True
    cur = user32.GetForegroundWindow()
    tid_cur = W.thread_of(cur) if cur else 0
    attached = bool(tid_cur) and W.attach_input(tid_cur)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            W.detach_input(tid_cur)
    end = time.time() + timeout
    while time.time() < end:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.02)
    return user32.GetForegroundWindow() == hwnd


def _reassert_focus_emulation(target):
    """前台租约归还后重新声明页面焦点状态；失败不掩盖输入结果。"""

    try:
        target.tab._run_cdp('Emulation.setFocusEmulationEnabled', enabled=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
class FgMouse(object):
    """
    前台鼠标。接口与 _input.BgMouse 完全一致（含 keeper 属性占位），
    内部坐标同样用「客户区物理像素」，轨迹同样走 humanize，人格一致。
    """

    _BTN = {'left': (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 1, 0),
            'right': (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 2, 0),
            'middle': (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 4, 0),
            'back': (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, 8, 1),
            'forward': (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, 16, 2)}

    def __init__(self, target, rng=None, profile=None, activate=True, **_):
        self.t = target
        self.rng = rng or random.Random()
        self.profile = profile
        self.instant = False
        self.activate = bool(activate)
        self.keep_on_top = True   # 窗口置顶，见 set_topmost：被盖住会导致页面 hidden、注入失效
        self.keeper = None              # 前台模式不需要捕获守卫
        self.capture_guard = False
        self.lock = threading.RLock()
        self.last_activity = 0.0
        self._x = None
        self._y = None
        self._btn = 0

    # ---------------- 内部 ---------------- #
    def _touch(self):
        self.last_activity = time.time()

    def _ensure_fg(self):
        if self.activate and not owns_foreground_lease(self.t.hwnd):
            force_foreground(self.t.hwnd, topmost=self.keep_on_top)

    @contextmanager
    def _act(self, touch=True):
        # 窗口锁 + 切前台：同一窗口的多个 tab 共用一个 HWND，必须串行且切到可见 tab，
        # 否则按键会打到别的 tab 上。详见 session.ensure_front。
        with self.t.win_lock, self.lock:
            self.t.ensure_front()
            try:
                with foreground_lease(
                    self.t.hwnd,
                    owner="foreground-mouse",
                    keep_on_top=self.keep_on_top,
                ):
                    if touch:
                        self._touch()
                    try:
                        yield
                    finally:
                        if touch:
                            self._touch()
            finally:
                _reassert_focus_emulation(self.t)

    def _goto(self, x, y, modifiers=0):
        x, y = int(round(x)), int(round(y))
        if self._x == x and self._y == y:
            return False
        self._x, self._y = x, y
        sx, sy = W.client_to_screen(self.t.hwnd, x, y)
        ax, ay = _abs_xy(sx, sy)
        return bool(_send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                                 | MOUSEEVENTF_VIRTUALDESK, ax, ay)))

    def _travel(self, tx, ty, modifiers=0, humanize=True, drag=False,
                target_w=None):
        if travel_mobile(self, tx, ty, modifiers, drag=drag):
            return
        if self._x is None or not humanize:
            self._goto(tx, ty, modifiers)
            return
        if drag:
            points = hz.drag_path(self._x, self._y, tx, ty,
                                  rng=self.rng, profile=self.profile)
        else:
            points = hz.path(self._x, self._y, tx, ty, rng=self.rng,
                             profile=self.profile, target_w=target_w)
        next_sample_at = time.perf_counter()
        for px, py in points:
            if self._goto(px, py, modifiers):
                next_sample_at += hz.move_interval(self.rng, self.profile)
                remaining = next_sample_at - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
                elif remaining < -0.05:
                    next_sample_at = time.perf_counter()

    def _down(self, button='left', modifiers=0):
        dn, _, bit, data = self._BTN[button]
        self._btn |= bit
        return bool(_send(_mouse(dn, data=data)))

    def _up(self, button='left', modifiers=0):
        _, up, bit, data = self._BTN[button]
        self._btn &= ~bit
        return bool(_send(_mouse(up, data=data)))

    # ---------------- 公开（与 BgMouse 同签名） ---------------- #
    @property
    def pos_client(self):
        return (self._x, self._y)

    @property
    def pressed(self):
        return self._btn != 0

    def move_to(self, cx, cy, modifiers=0, humanize=True, target_w=None):
        with self._act():
            self._travel(*self.t.css_to_client(cx, cy), modifiers=modifiers,
                         humanize=humanize, target_w=target_w)

    def ensure_at(self, cx, cy, modifiers=0):
        with self._act():
            tx, ty = self.t.css_to_client(cx, cy)
            if self._x != int(round(tx)) or self._y != int(round(ty)):
                self._travel(tx, ty, modifiers)

    def down(self, button='left', modifiers=0):
        with self._act():
            return self._down(button, modifiers)

    def up(self, button='left', modifiers=0):
        with self._act():
            return self._up(button, modifiers)

    def click(self, button='left', modifiers=0, hold=None):
        with self._act():
            self._down(button, modifiers)
            time.sleep(hold if hold is not None else hz.click_hold(self.rng, self.profile))
            self._up(button, modifiers)

    def double_click(self, modifiers=0):
        with self._act():
            self._down('left', modifiers)
            time.sleep(hz.click_hold(self.rng, self.profile))
            self._up('left', modifiers)
            time.sleep(self.rng.uniform(0.06, 0.12))
            self._down('left', modifiers)
            time.sleep(hz.click_hold(self.rng, self.profile))
            self._up('left', modifiers)

    def wheel(self, notches, modifiers=0):
        with self._act():
            step = 120 if notches > 0 else -120
            for _ in range(abs(int(notches))):
                _send(_mouse(MOUSEEVENTF_WHEEL, data=step))
                time.sleep(hz.wheel_gap(self.rng, self.profile))

    def drag(self, to_cx, to_cy, button='left', modifiers=0, settle=0.05):
        with self._act():
            if self._x is None:
                return False
            self._down(button, modifiers)
            time.sleep(self.rng.uniform(0.02, 0.06))
            self._travel(*self.t.css_to_client(to_cx, to_cy),
                         modifiers=modifiers, drag=True)
            time.sleep(settle)
            self._up(button, modifiers)
            return True

    def micro_move(self, max_px=3.0, steps=None):
        with self._act(touch=False):
            if self._x is None or self._btn:
                return False
            cw, ch = W.client_size(self.t.hwnd)
            bx, by = self._x, self._y
            next_sample_at = time.perf_counter()
            for _ in range(steps or self.rng.randint(2, 5)):
                nx = min(max(bx + self.rng.uniform(-max_px, max_px), 2), cw - 3)
                ny = min(max(by + self.rng.uniform(-max_px, max_px),
                             self.t.off[1] + 2), ch - 3)
                if self._goto(nx, ny):
                    next_sample_at += hz.move_interval(self.rng, self.profile)
                    remaining = next_sample_at - time.perf_counter()
                    if remaining > 0:
                        time.sleep(remaining)
                    elif remaining < -0.05:
                        next_sample_at = time.perf_counter()
            return True

    def idle_drift(self, max_px=120.0):
        with self._act(touch=False):
            if self._x is None or self._btn:
                return False
            cw, ch = W.client_size(self.t.hwnd)
            top = self.t.off[1] + 4
            tx = min(max(self._x + self.rng.uniform(-max_px, max_px), 4), cw - 5)
            ty = min(max(self._y + self.rng.uniform(-max_px, max_px), top), ch - 5)
            self._travel(tx, ty, humanize=True)
            return True

    def release_all(self):
        with self._act():
            for b in ('left', 'right', 'middle', 'back', 'forward'):
                _, up, bit, data = self._BTN[b]
                if self._btn & bit:
                    self._btn &= ~bit
                    _send(_mouse(up, data=data))


# --------------------------------------------------------------------------- #
class FgKeyboard(object):
    """
    前台键盘。接口与 _input.BgKeyboard 一致。

    走扫描码（KEYEVENTF_SCANCODE）而不是虚拟键 —— Chromium 的 KeyboardEvent.code
    是从扫描码推导的（events_win_utils.cc:369-372）。本布局打不出的字符退回
    KEYEVENTF_UNICODE。修饰键直接按住真键，不需要 SetKeyboardState 那套。
    """

    def __init__(self, target, rng=None, profile=None, activate=True, **_):
        self.t = target
        self.rng = rng or random.Random()
        self.profile = profile
        self.activate = bool(activate)
        self.keep_on_top = True   # 窗口置顶，见 set_topmost：被盖住会导致页面 hidden、注入失效
        self.lock = threading.RLock()

    def _ensure_fg(self):
        if self.activate and not owns_foreground_lease(self.t.hwnd):
            force_foreground(self.t.hwnd, topmost=self.keep_on_top)

    @contextmanager
    def _ascii_layout(self):
        """
        打字期间把目标窗口的输入语言切成 en-US，打完切回去。

        前台注入的按键会经过当前输入法 —— 中文输入法开着时，'Ab0!' 会被联想成
        'About！'（联想词 + 全角标点），实测如此。统一底层只向目标 HWND 投递
        WM_INPUTLANGCHANGEREQUEST，并在动作结束后恢复目标线程原布局。
        """
        with W.temporary_keyboard_layout(self.t.hwnd) as ready:
            yield ready

    @contextmanager
    def _act(self, ascii_layout=False):
        """按窗口串行 + 把本 tab 切到前面 + 窗口切前台，再敲键。"""
        with self.t.win_lock, self.lock:
            self.t.ensure_front()
            try:
                with foreground_lease(
                    self.t.hwnd,
                    owner="foreground-keyboard",
                    keep_on_top=self.keep_on_top,
                ):
                    if ascii_layout:
                        with self._ascii_layout():
                            yield
                    else:
                        yield
            finally:
                _reassert_focus_emulation(self.t)

    def _tap_vk(self, vk, ch=None, hold=None):
        scan = user32.MapVirtualKeyW(vk, W.MAPVK_VK_TO_VSC)
        ext = KEYEVENTF_EXTENDEDKEY if vk in W.EXTENDED_VKS else 0
        _send(_key(vk, scan, KEYEVENTF_SCANCODE | ext))
        time.sleep(hold if hold is not None else hz.key_hold(self.rng, self.profile))
        _send(_key(vk, scan, KEYEVENTF_SCANCODE | ext | KEYEVENTF_KEYUP))

    def press(self, vk, hold=None, char=None):
        with self._act():
            self._ensure_fg()
            self._tap_vk(vk, hold=hold)
            return True

    def type_modified(self, text, modifier_bits, special_vks=None, interval=0):
        """用 SendInput 发送真实组合键；不再把 Ctrl/Alt/Shift 交给 CDP。"""
        special_vks = special_vks or {}
        modifier_vks = []
        if modifier_bits & 8:
            modifier_vks.append(W.VK_SHIFT)
        if modifier_bits & 2:
            modifier_vks.append(W.VK_CONTROL)
        if modifier_bits & 1:
            modifier_vks.append(W.VK_MENU)
        if modifier_bits & 4:
            modifier_vks.append(W.VK_LWIN)
        with self._act(ascii_layout=True):
            self._ensure_fg()
            for vk in modifier_vks:
                scan = user32.MapVirtualKeyW(vk, W.MAPVK_VK_TO_VSC)
                _send(_key(vk, scan, KEYEVENTF_SCANCODE))
            try:
                for ch in text:
                    vk = special_vks.get(ch)
                    if vk is not None:
                        self._tap_vk(vk)
                    else:
                        vk, _ = W.vk_scan(ch)
                        if vk is None:
                            self._type_char(ch)
                        else:
                            self._tap_vk(vk)
                    if interval:
                        time.sleep(interval)
            finally:
                for vk in reversed(modifier_vks):
                    scan = user32.MapVirtualKeyW(vk, W.MAPVK_VK_TO_VSC)
                    ext = KEYEVENTF_EXTENDEDKEY if vk in W.EXTENDED_VKS else 0
                    _send(_key(vk, scan, KEYEVENTF_SCANCODE | ext | KEYEVENTF_KEYUP))
        return True

    def _type_char(self, ch):
        caps_lock = W.target_caps_lock(self.t.tid) if ('a' <= ch <= 'z' or 'A' <= ch <= 'Z') else False
        vk, need_shift = W.text_vk_scan(ch, caps_lock)
        if vk is None:                      # 本布局打不出（中文等）-> Unicode 路径
            code = ord(ch)
            _send(_key(0, code, KEYEVENTF_UNICODE))
            time.sleep(hz.key_hold(self.rng, self.profile))
            _send(_key(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            return
        sh_scan = user32.MapVirtualKeyW(W.VK_SHIFT, W.MAPVK_VK_TO_VSC)
        if need_shift:
            _send(_key(W.VK_SHIFT, sh_scan, KEYEVENTF_SCANCODE))
            time.sleep(self.rng.uniform(0.012, 0.03))
        try:
            self._tap_vk(vk)
        finally:
            if need_shift:
                time.sleep(self.rng.uniform(0.012, 0.03))
                _send(_key(W.VK_SHIFT, sh_scan, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP))

    def _read_back(self, verify):
        try:
            return verify()
        except Exception:
            return None

    def type_text(self, text, verify=None, clear=None, retries=2):
        with self._act(ascii_layout=True):
            self._ensure_fg()
            for attempt in range(retries + 1):
                if attempt and clear:
                    try:
                        clear()
                    except Exception:
                        pass
                # 停顿在敲之前：间隔由「上一个键 → 这个键」决定，不是单个键。
                previous_char = None
                for ch in text:
                    if previous_char is not None:
                        time.sleep(
                            hz.key_gap(self.rng, self.profile, ch, previous_char))
                    self._type_char(ch)
                    previous_char = ch
                if verify is None:
                    return True
                got = self._read_back(verify)
                if got is None or got == text:
                    return True
            return False

    def backspace(self, n=1):
        with self._act():
            self._ensure_fg()
            for _ in range(n):
                self._tap_vk(W.VK_BACK)
                time.sleep(hz.key_gap(self.rng, self.profile))
            return True

    def select_all_delete(self):
        with self._act():
            self._ensure_fg()
            ctrl_scan = user32.MapVirtualKeyW(W.VK_CONTROL, W.MAPVK_VK_TO_VSC)
            _send(_key(W.VK_CONTROL, ctrl_scan, KEYEVENTF_SCANCODE))
            time.sleep(0.02)
            self._tap_vk(0x41)                       # 'A'
            time.sleep(0.02)
            _send(_key(W.VK_CONTROL, ctrl_scan, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP))
            time.sleep(0.03)
            self._tap_vk(W.VK_DELETE)
            return True
