# -*- coding: utf-8 -*-
"""
后台鼠标 / 键盘注入。

坐标一律用 CSS 像素（与 DP 的 rect 单位一致），内部换算成内容窗口客户区物理像素。

线程安全
--------
公开方法全部持 self.lock。空闲微动线程（behavior.IdleMotion）在同一把锁上运行，
两边不会交错投递消息。所有会改变指针状态的操作都会更新 last_activity，
微动线程据此判断「现在是不是真的空闲」。
"""
import math
import random
import threading
import time
from contextlib import contextmanager

from . import cursor_trajectory as hz
from . import win32_native as W
from .cursor_overlay import CursorObserver
from .mobile_motion import travel_mobile


def _next_sample_deadline(deadline, now, period):
    """压力调度错过采样点时跳到下一个硬件时钟刻度，禁止短间隔追发。"""

    period = max(0.001, float(period))
    deadline = float(deadline)
    now = float(now)
    if now <= deadline:
        return deadline
    missed = int(math.floor((now - deadline) / period)) + 1
    return deadline + missed * period


def _wait_for_sample_deadline(deadline, period):
    """在固定采样网格上等待；最后 0.8ms 忙等以减少 Windows sleep 漂移。"""

    now = time.perf_counter()
    deadline = _next_sample_deadline(deadline, now, period)
    remaining = deadline - now
    if remaining > 0.0012:
        time.sleep(remaining - 0.0008)
    while time.perf_counter() < deadline:
        pass
    return deadline


class BackgroundFocusStolenError(RuntimeError):
    """后台输入意外激活了目标浏览器；已停止后续键盘消息。"""


def _raise_if_target_stole_foreground(previous_hwnd, target_hwnd, baseline_rect=None,
                                      session=None):
    if W.wait_for_user_window_move(previous_hwnd, target_hwnd, baseline_rect):
        # 用户主动拖动目标浏览器：接受新的前台归属。本动作继续使用客户区坐标；
        # 松手后立即刷新 DPI/客户区映射，跨显示器移动也不能沿用旧偏移。
        refresh = getattr(session, 'recalibrate_after_window_move', None)
        if callable(refresh) and not refresh():
            raise RuntimeError('用户移动浏览器后 Win32 坐标重新标定失败')
        return int(W.foreground_window() or target_hwnd)
    stolen, restored = W.restore_foreground_if_target_stole(
        previous_hwnd,
        target_hwnd,
    )
    if stolen:
        suffix = '，已恢复用户原窗口' if restored else '，恢复用户原窗口失败'
        raise BackgroundFocusStolenError('后台输入意外激活目标浏览器' + suffix)
    return previous_hwnd


def _restore_after_mouse_action(previous_hwnd, target_hwnd, baseline_rect=None,
                                session=None):
    """鼠标 down/up 原子完成后恢复用户窗口；恢复成功不把已完成动作判失败。"""

    if W.wait_for_user_window_move(previous_hwnd, target_hwnd, baseline_rect):
        refresh = getattr(session, 'recalibrate_after_window_move', None)
        if callable(refresh) and not refresh():
            raise RuntimeError('用户移动浏览器后 Win32 坐标重新标定失败')
        return
    stolen, restored = W.restore_foreground_if_target_stole(
        previous_hwnd,
        target_hwnd,
    )
    if stolen and not restored:
        raise BackgroundFocusStolenError('后台鼠标操作后恢复用户原窗口失败')


class BgMouse(object):
    """
    鼠标：全部走 PostMessage。

    * 移动 / 按下 / 抬起 的 lParam 用客户区坐标，Chromium 自己 ClientToScreen 得到
      screenX（events_win_utils.cc:356-363），所以 screenX != clientX 天然成立。
    * 抬起时 wParam 不带该按钮位 —— 与真实鼠标一致，于是 mouseup.buttons == 0。
    * 滚轮的 lParam 是「屏幕坐标」（events_win_utils.cc:340-350 的 else 分支）。
    """

    _BTN = {'left': (W.WM_LBUTTONDOWN, W.WM_LBUTTONUP, W.MK_LBUTTON, W.WM_LBUTTONDBLCLK),
            'right': (W.WM_RBUTTONDOWN, W.WM_RBUTTONUP, W.MK_RBUTTON, W.WM_RBUTTONDBLCLK),
            'middle': (W.WM_MBUTTONDOWN, W.WM_MBUTTONUP, W.MK_MBUTTON, None),
            'back': (0x020B, 0x020C, W.MK_XBUTTON1, None),
            'forward': (0x020B, 0x020C, W.MK_XBUTTON2, None)}
    def __init__(self, target, rng=None, profile=None, capture_guard=True):
        self.t = target
        self.rng = rng or random.Random()
        self.profile = profile
        self.instant = False
        # 每次鼠标动作临时捕获；动作结束立即释放，避免常驻 SetCapture 干扰用户。
        self.capture_guard = bool(capture_guard)
        # 输入 Desktop 保持 None；独立 Desktop 槽由 Session 安装固定线程 keeper。
        self.keeper = None
        self.lock = threading.RLock()
        self.last_activity = 0.0
        self._x = None          # 当前物理坐标（客户区）
        self._y = None
        self._btn = 0
        self._capture_owned = False
        self._physical_mouse_state = None
        self._physical_mouse_changed_at = 0.0
        self._foreground_before = 0
        self._window_rect_before = None
        # VirtualMouse 会把“移动到元素 + ensure + down/up”包成一个逻辑动作。
        # 嵌套的底层入口必须共用同一捕获线程，否则每段轨迹结束时释放捕获，
        # Windows 会补发可信 pointerleave，正是检测站 N1 的来源。
        self._logical_depth = 0
        self._cursor_observer = CursorObserver()

    # ------------------------------------------------------------------ #
    # 内部（调用方必须已持锁）
    # ------------------------------------------------------------------ #
    def _touch(self):
        self.last_activity = time.time()

    def _sample_user_mouse_busy(self):
        """只观察真实光标/物理按钮；用户打字不会阻塞后台鼠标。"""
        state = W.physical_mouse_state()
        if state is None:
            return False
        now = time.monotonic()
        if state != self._physical_mouse_state:
            self._physical_mouse_state = state
            self._physical_mouse_changed_at = now
        if any(state[2]):
            self._physical_mouse_changed_at = now
            return True
        return now - self._physical_mouse_changed_at < 0.18

    def _wait_and_capture(self, timeout=2.5):
        if not self.capture_guard:
            return True
        keeper = self.keeper
        if keeper is not None:
            if keeper.ok and not keeper.degraded and keeper.held.is_set():
                return True
            raise RuntimeError('独立 Desktop 鼠标捕获动作租约已经失效')
        # down/up 分离调用时必须尽快完成抬起，不能因用户开始移动而长期扣住捕获。
        if self._capture_owned and self._btn:
            return True
        end = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < end:
            if self._sample_user_mouse_busy():
                time.sleep(self.rng.uniform(0.045, 0.095))
                continue
            if W.arm_capture(self.t.hwnd, self.t.tid):
                self._capture_owned = True
                return True
            time.sleep(self.rng.uniform(0.045, 0.095))
        raise RuntimeError('用户正在操作鼠标或系统捕获被占用，后台鼠标动作已延后')

    def _raise_if_user_mouse_active(self):
        if (self.capture_guard and self._capture_owned
                and self._sample_user_mouse_busy()):
            raise RuntimeError('检测到用户正在操作鼠标，已中止本次后台鼠标动作')

    def _post(self, msg, wparam):
        if msg not in {W.WM_LBUTTONUP, W.WM_RBUTTONUP, W.WM_MBUTTONUP}:
            self._raise_if_user_mouse_active()
        # 一次点击的 down/up 必须完整投递。Chrome 可能在 down 后短暂激活自身；
        # 若在 up 前恢复并抛错，会人为制造只有按下没有抬起的残缺点击。
        post_mouse_move = getattr(self.t, 'post_mouse_move', None)
        if msg == W.WM_MOUSEMOVE and callable(post_mouse_move):
            result = post_mouse_move(self._x, self._y, wparam)
        else:
            deliver = getattr(self.t, 'deliver_mouse_message', None)
            if callable(deliver):
                result = deliver(msg, wparam, W.lparam_xy(self._x, self._y))
            else:
                result = W.post(
                    self.t.hwnd, msg, wparam, W.lparam_xy(self._x, self._y)
                )
        if result is not False:
            self._cursor_observer.notify(self.t, self._x, self._y, msg, wparam)
        return result

    def _release_operation_capture(self, settle=True):
        if not self._capture_owned:
            return
        try:
            # PostMessage 是异步的，给 Chromium 一个很短的处理窗口；期间不再投递移动。
            if settle:
                time.sleep(0.025)
            W.release_capture(self.t.hwnd, self.t.tid)
        finally:
            self._capture_owned = False

    def _release_pressed_after_abort(self):
        if self._x is None or self._y is None:
            self._btn = 0
            return
        for button in ('left', 'right', 'middle', 'back', 'forward'):
            _, up, bit, _ = self._BTN[button]
            if self._btn & bit:
                self._btn &= ~bit
                extra = (
                    (1 << 16)
                    if button == 'back'
                    else ((2 << 16) if button == 'forward' else 0)
                )
                result = W.post(
                    self.t.hwnd,
                    up,
                    self._btn | extra,
                    W.lparam_xy(self._x, self._y),
                )
                if result is not False:
                    self._cursor_observer.notify(self.t, self._x, self._y, up, self._btn | extra)

    @contextmanager
    def logical_action(self):
        """把一次上层鼠标意图合并成一个捕获区间。

        例如点击元素在上层实际包含 ``move_to -> ensure_at -> click``。这些步骤
        若各自创建/释放 CaptureKeeper，页面会在每段轨迹后收到系统补发的
        ``pointerleave``。外层只建立一次租约，嵌套 ``_act`` 只复用它。
        """
        with self._act():
            self._logical_depth += 1
            try:
                yield
            finally:
                self._logical_depth -= 1

    @contextmanager
    def _act(self, touch=True):
        """
        所有公开入口统一走这里：按独立窗口串行 -> 记活动。

        后台模式只向目标内容 HWND 投递消息，不调用 SetForegroundWindow。这样自动化
        运行时不会抢走用户当前应用的键盘焦点；网页是否处于真实系统前台由浏览器
        自己反映，不做焦点模拟。

        项目固定一窗口一 tab；窗口锁仍保留，用于阻止同一窗口内的鼠标、键盘和
        空闲微动互相交织。不同 HWND 使用不同锁。

        捕获只覆盖本次动作。用户真实鼠标活动时先等待；动作结束或异常时立即释放。

        touch=False 供空闲微动使用 —— 它不能把自己算成"活动"，
        否则 IdleMotion 永远等不到真正的空闲。
        """
        # 外层 ``serialized_input`` 会先取得窗口锁。空闲微动若反过来先拿设备锁，
        # 会形成「业务线程持窗口锁等设备锁、空闲线程持设备锁等窗口锁」的死锁。
        # 所有入口统一窗口锁 -> 设备锁，保持同一加锁顺序。
        with self.t.win_lock, self.lock:
            if self._logical_depth:
                if touch:
                    self._touch()
                try:
                    yield
                finally:
                    if touch:
                        self._touch()
                return
            foreground_before = W.foreground_window()
            self._foreground_before = foreground_before
            try:
                root = int(W.root_of(self.t.hwnd) or self.t.hwnd)
                self._window_rect_before = W.window_rect(root)
            except Exception:
                self._window_rect_before = None
            if touch:
                self._touch()
            failed = True
            try:
                # 后台虚拟鼠标只投递目标 HWND 消息，不依赖用户真实键盘或光标。
                # 不因用户同时操作电脑而暂停独立窗口轨迹。
                self._wait_and_capture()
                self._foreground_before = _raise_if_target_stole_foreground(
                    foreground_before,
                    self.t.hwnd,
                    self._window_rect_before,
                    self.t,
                )
                yield
                failed = False
            finally:
                try:
                    if failed:
                        self._release_pressed_after_abort()
                    if failed or not self._btn:
                        self._release_operation_capture()
                    if touch:
                        self._touch()
                    _restore_after_mouse_action(
                        self._foreground_before,
                        self.t.hwnd,
                        self._window_rect_before,
                        self.t,
                    )
                finally:
                    self._foreground_before = 0
                    self._window_rect_before = None

    def _goto(self, x, y, modifiers=0):
        x, y = int(round(x)), int(round(y))
        if self._x == x and self._y == y:
            return False        # 相同坐标会被 Chromium 静默丢弃，别浪费往返
        self._x, self._y = x, y
        return self._post(W.WM_MOUSEMOVE, self._btn | modifiers)

    def _travel(self, tx, ty, modifiers=0, humanize=True, drag=False,
                target_w=None):
        if travel_mobile(self, tx, ty, modifiers, drag=drag):
            return
        if self._x is None or not humanize:
            self._goto(tx, ty, modifiers)
            return
        if drag:
            pts = hz.drag_path(self._x, self._y, tx, ty,
                               rng=self.rng, profile=self.profile)
        else:
            pts = hz.path(self._x, self._y, tx, ty, rng=self.rng,
                          profile=self.profile, target_w=target_w)
        # 使用固定 4/8ms 硬件时钟网格。相对 sleep 会把消息投递耗时与调度
        # 误差逐点累加；旧绝对时钟在错过刻度后又用很短的剩余时间追发，也会
        # 形成连续分布。现在错过刻度就丢掉该时间片，等待下一个硬件采样点。
        period = max(
            0.001,
            float(getattr(self.profile, 'sample_period', 0.008)),
        )
        next_sample_at = time.perf_counter()
        first = True
        for px, py in pts:
            if not first:
                next_sample_at = _wait_for_sample_deadline(next_sample_at, period)
            if self._goto(px, py, modifiers):
                interval = hz.move_interval(self.rng, self.profile)
                ticks = max(1, int(round(interval / period)))
                next_sample_at += ticks * period
                first = False

    def _down(self, button='left', modifiers=0):
        dn, _, bit, _ = self._BTN[button]
        self._btn |= bit
        extra = (1 << 16) if button == 'back' else ((2 << 16) if button == 'forward' else 0)
        return self._post(dn, self._btn | modifiers | extra)

    def _up(self, button='left', modifiers=0):
        _, up, bit, _ = self._BTN[button]
        self._btn &= ~bit
        extra = (1 << 16) if button == 'back' else ((2 << 16) if button == 'forward' else 0)
        r = self._post(up, self._btn | modifiers | extra)  # 低位已不含该位 -> buttons==0
        keeper = self.keeper
        if keeper is not None and not keeper.after_release():
            raise RuntimeError('独立 Desktop 鼠标捕获在抬键后恢复失败')
        return r

    # ------------------------------------------------------------------ #
    # 公开
    # ------------------------------------------------------------------ #
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

    def press_for(self, seconds, button='left', modifiers=0):
        """在一个窗口动作上下文内完成按下、保持和抬起。

        ``down()`` 和 ``up()`` 分开调用时，每个入口都会单独执行前台状态恢复。
        Chrome 若在按下后短暂激活自身，夹在两者之间的恢复会向页面产生
        pointerleave，长按计时器随即失效。长按必须是一个原子 Win32 消息段；
        本实现仍只向目标内容 HWND 投递消息，不移动系统真实光标。
        """
        duration = max(0.0, float(seconds))
        with self._act():
            self._down(button, modifiers)
            try:
                end = time.monotonic() + duration
                while True:
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        break
                    # 分段等待让异常清理仍能及时抬键，但不离开本次窗口动作上下文。
                    time.sleep(min(0.1, remaining))
            finally:
                self._up(button, modifiers)

    def double_click(self, modifiers=0):
        with self._act():
            dn, up, bit, dbl = self._BTN['left']
            self._down('left', modifiers)
            time.sleep(hz.click_hold(self.rng, self.profile))
            self._up('left', modifiers)
            time.sleep(self.rng.uniform(0.06, 0.12))
            if self.capture_guard and self.keeper is None:
                if self._sample_user_mouse_busy():
                    raise RuntimeError('检测到用户正在操作鼠标，已中止本次后台双击')
                if not W.rearm_capture_after_release(self.t.hwnd, self.t.tid):
                    raise RuntimeError('后台双击无法重新取得短时鼠标捕获')
                self._capture_owned = True
            self._btn |= bit
            self._post(dbl, self._btn | modifiers)
            time.sleep(hz.click_hold(self.rng, self.profile))
            self._btn &= ~bit
            self._post(up, self._btn | modifiers)

    def wheel(self, notches, modifiers=0):
        """
        notches > 0 向上，< 0 向下。每格一条 WM_MOUSEWHEEL（wheelDelta = ±120），
        与真实滚轮驱动一致 -> wheelDeltaY 与 deltaY 的比值正常。
        """
        with self._act():
            if self._x is None:
                return
            sx, sy = W.client_to_screen(self.t.hwnd, self._x, self._y)
            step = 120 if notches > 0 else -120
            for _ in range(abs(int(notches))):
                self._raise_if_user_mouse_active()
                wp = ((step & 0xFFFF) << 16) | (modifiers & 0xFFFF)
                deliver = getattr(self.t, 'deliver_mouse_message', None)
                if callable(deliver):
                    result = deliver(W.WM_MOUSEWHEEL, wp, W.lparam_xy(sx, sy))
                else:
                    result = W.post(
                        self.t.hwnd, W.WM_MOUSEWHEEL, wp, W.lparam_xy(sx, sy)
                    )
                if result is not False:
                    self._cursor_observer.notify(self.t, self._x, self._y, W.WM_MOUSEWHEEL, wp)
                time.sleep(hz.wheel_gap(self.rng, self.profile))

    def close_visual(self):
        self._cursor_observer.close()

    def drag(self, to_cx, to_cy, button='left', modifiers=0, settle=0.05):
        """
        从当前位置拖到 (to_cx, to_cy)。

        调用方必须先保证不会触发原生拖放（清空选区、且起点不在可拖拽内容上），
        否则 Chrome 会进入 DoDragDrop 模态循环，把后续 Post 的消息全部吞掉，
        页面将看到 pointerdown -> 1 个 move -> 永远没有 pointerup 的异常序列。
        """
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
        """空闲微动：在当前位置附近做几步小抖动。由 IdleMotion 调用。"""
        with self._act(touch=False):
            if self._x is None or self._btn:
                return False
            cw, ch = W.client_size(self.t.hwnd)
            n = steps or self.rng.randint(2, 5)
            bx, by = self._x, self._y
            next_sample_at = time.perf_counter()
            for _ in range(n):
                nx = bx + self.rng.uniform(-max_px, max_px)
                ny = by + self.rng.uniform(-max_px, max_px)
                nx = min(max(nx, 2), cw - 3)
                ny = min(max(ny, self.t.off[1] + 2), ch - 3)
                if self._goto(nx, ny):
                    next_sample_at += hz.move_interval(self.rng, self.profile)
                    remaining = next_sample_at - time.perf_counter()
                    if remaining > 0:
                        time.sleep(remaining)
                    elif remaining < -0.05:
                        next_sample_at = time.perf_counter()
            return True

    def idle_drift(self, max_px=120.0):
        """空闲漂移：像人无意识地把鼠标挪到附近某处。"""
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
        """卡死恢复：补一次抬起。"""
        with self.t.win_lock, self.lock:
            try:
                self._release_pressed_after_abort()
            finally:
                self._release_operation_capture(settle=False)


class BgKeyboard(object):
    """
    键盘：输入桌面走 SendMessageTimeout，独立 Desktop 走 PostMessage。

    PostMessage 的 WM_KEYDOWN 会被 Chrome 消息泵的 TranslateMessage 自动翻译出一条
    WM_CHAR，因此独立 Desktop 的普通字符不能再手工补 WM_CHAR；输入桌面使用同步
    Send 时仍由本实现显式发送。不可映射 Unicode 保留 VK_PACKET + WM_CHAR 边界。
    两条路径的最终结果都由上层 DP 回读确认。

    需要 Shift 的字符按连续段共用一次 AttachThreadInput（实测 10/10 形态正确）。
    """

    def __init__(self, target, rng=None, profile=None):
        self.t = target
        self.rng = rng or random.Random()
        self.profile = profile
        self.lock = threading.RLock()
        self._foreground_before = 0
        self._window_rect_before = None
        self._queue_mode = not W.is_input_desktop()
        self.human_timing = True

    def _send(self, msg, wparam, lparam):
        """输入 Desktop 限时同步投递；隔离 Desktop 使用独立窗口消息队列。"""
        self._foreground_before = _raise_if_target_stole_foreground(
            self._foreground_before, self.t.hwnd, self._window_rect_before, self.t)
        sender = W.post if self._queue_mode else W.send
        if not sender(self.t.hwnd, msg, wparam, lparam):
            raise RuntimeError('后台键盘消息发送超时或目标窗口无响应')
        try:
            self._foreground_before = _raise_if_target_stole_foreground(
                self._foreground_before, self.t.hwnd, self._window_rect_before, self.t)
        except BackgroundFocusStolenError:
            # 若激活发生在 keydown 后，先给目标补 keyup，避免页面残留按下态。
            if msg == W.WM_KEYDOWN:
                try:
                    sender = W.post if self._queue_mode else W.send
                    sender(self.t.hwnd, W.WM_KEYUP, wparam, W.key_lparam(wparam, True))
                except Exception:
                    pass
            raise

    def _release_vk(self, vk):
        """可靠释放按键；同步窗口过程短暂繁忙时退为排队释放。

        ``SendMessageTimeout`` 在 Chromium 主线程忙于页面事件时可能只让 KEYUP
        超时。KEYDOWN/WM_CHAR 已经执行，此时直接抛错会跳过上层内容回读，还可能
        把修饰键留在按下态。KEYUP 本身没有输入副作用，允许用 ``PostMessage``
        补投；只有同步和异步两条释放路径都失败时才判定本次输入失败。
        """
        lparam = W.key_lparam(vk, True)
        try:
            self._send(W.WM_KEYUP, vk, lparam)
            return
        except RuntimeError:
            # Chromium 正在处理高频 pointer/event 探针时消息队列可能短暂满载，
            # PostMessage 会返回 0。KEYUP 可安全重投，给目标线程最多 0.4 秒排空
            # 队列；不重发 KEYDOWN/CHAR，避免重复输入。
            for _ in range(8):
                if W.post(self.t.hwnd, W.WM_KEYUP, vk, lparam):
                    return
                time.sleep(0.05)
            raise

    @contextmanager
    def _act(self, ascii_layout=False):
        """与 BgMouse._act 同理：只锁定本会话的独立窗口，不切 tab、不抢前台。"""
        with self.t.win_lock, self.lock:
            foreground_before = W.foreground_window()
            self._foreground_before = foreground_before
            try:
                root = int(W.root_of(self.t.hwnd) or self.t.hwnd)
                self._window_rect_before = W.window_rect(root)
            except Exception:
                self._window_rect_before = None
            completed = False
            try:
                # 后台消息只进入目标 HWND，不使用系统硬件键盘。用户在 Hi 或其它
                # 应用真实打字时无需让行；旧的桌面级 GetAsyncKeyState 等待会让每个
                # 后台窗口都无故暂停，甚至在持续输入时失败。
                self._foreground_before = _raise_if_target_stole_foreground(
                    foreground_before,
                    self.t.hwnd,
                    self._window_rect_before,
                    self.t,
                )
                prepare = getattr(self.t, 'prepare_keyboard_focus', W.prepare_background_keyboard)
                if not prepare(self.t.hwnd):
                    raise RuntimeError('目标窗口 Win32 键盘焦点刷新失败')
                self._foreground_before = _raise_if_target_stole_foreground(
                    self._foreground_before,
                    self.t.hwnd,
                    self._window_rect_before,
                    self.t,
                )
                if ascii_layout:
                    with W.temporary_keyboard_layout(self.t.hwnd):
                        yield
                else:
                    yield
                completed = True
            finally:
                try:
                    if completed:
                        self._foreground_before = _raise_if_target_stole_foreground(
                            self._foreground_before,
                            self.t.hwnd,
                            self._window_rect_before,
                            self.t,
                        )
                finally:
                    self._foreground_before = 0
                    self._window_rect_before = None

    # ---------------- 单键 ----------------
    def _tap_vk(self, vk, ch=None, hold=None):
        lp = W.key_lparam(vk)
        self._send(W.WM_KEYDOWN, vk, lp)
        if ch is not None and (not self._queue_mode or vk == W.VK_PACKET):
            self._send(W.WM_CHAR, ord(ch), lp)
        hold_time = hold if hold is not None else (hz.key_hold(self.rng, self.profile) if self.human_timing else 0.04)
        # 非输入 Desktop 的 PostMessage 会立即返回。Chrome 在隐藏 Desktop 中
        # 可能隔一小段时间才处理已发送消息；在 keyup 和 SharedKeyState 恢复之前保留
        # 足够窗口，避免 Enter 的 WM_CHAR 丢失或大写字符处理时 Shift 已被还原。
        if self._queue_mode:
            hold_time = max(float(hold_time), 0.18)
        time.sleep(hold_time)
        self._release_vk(vk)
        if self._queue_mode:
            time.sleep(0.06)

    def press(self, vk, hold=None, char=None):
        """功能键。Enter 可显式补 ``char='\r'``，供 textarea 产生真实换行。"""
        with self._act():
            self._tap_vk(vk, char, hold)

    @staticmethod
    def _modifier_names(bits):
        names = []
        if bits & 8:
            names.append('shift')
        if bits & 2:
            names.append('ctrl')
        if bits & 1:
            names.append('alt')
        if bits & 4:
            names.append('meta')
        return tuple(names)

    def type_modified(self, text, modifier_bits, special_vks=None, interval=0):
        """在目标窗口线程的真实修饰键状态下输入，避免 Ctrl+A 等回退 CDP。"""
        special_vks = special_vks or {}
        names = self._modifier_names(modifier_bits)
        vks = [W.SharedKeyState.MODS[name][0] for name in names]
        with self._act(ascii_layout=True):
            with W.SharedKeyState(self.t.tid, mods=names) as ks:
                if not ks.ok:
                    raise RuntimeError('目标窗口组合键状态准备失败')
                try:
                    for vk in vks:
                        self._send(W.WM_KEYDOWN, vk, W.key_lparam(vk))
                    for ch in text:
                        vk = special_vks.get(ch)
                        if vk is not None:
                            self._tap_vk(vk, '\r' if vk == W.VK_RETURN else None)
                        else:
                            vk, _ = W.vk_scan(ch)
                            if vk is None:
                                self._char_only(ch)
                            else:
                                # Ctrl/Alt/Meta 组合依赖 keydown，不额外伪造 WM_CHAR。
                                emit_char = ch if not (modifier_bits & 7) else None
                                self._tap_vk(vk, emit_char)
                        if interval:
                            time.sleep(interval)
                finally:
                    if ks.ok:
                        for vk in reversed(vks):
                            self._release_vk(vk)
                        if self._queue_mode:
                            time.sleep(0.08)

    def _char_only(self, ch):
        """发送当前布局无法直接映射的 Unicode 字符。

        单独 WM_CHAR 会形成“可信 input、附近却完全没有 keydown”的异常序列。
        Windows 的 Unicode 键盘注入以 VK_PACKET 表示不可映射字符；保持
        keydown -> char -> keyup 的消息边界，同时不占用系统真实输入法状态。
        """
        self._tap_vk(W.VK_PACKET, ch)

    # ---------------- 文本 ----------------
    def _segments(self, text):
        """按「是否需要 Shift」切成连续段，减少 attach 次数。"""
        segs, cur, cur_kind = [], '', None
        for ch in text:
            if ch in ('\r', '\n'):
                kind = 'enter'
            else:
                vk, ns = W.vk_scan(ch)
                kind = 'cjk' if vk is None else ('shift' if ns else 'plain')
            if cur_kind is None or kind == cur_kind:
                cur += ch
                cur_kind = kind
            else:
                segs.append((cur_kind, cur))
                cur, cur_kind = ch, kind
        if cur:
            segs.append((cur_kind, cur))
        return segs

    def _type_raw(self, text):
        # 键间隔由「上一个键 → 这个键」决定（交替手最快、同指换键最慢），所以
        # 停顿必须在敲之前，而且要跨段带着上一个字符走 —— 大小写切换会把文本
        # 切成好几段，段边界正是最常见的「换手」位置，断在这里等于白算。
        previous_char = None

        def gap_before(ch):
            nonlocal previous_char
            if previous_char is not None and self.human_timing:
                time.sleep(hz.key_gap(self.rng, self.profile, ch, previous_char))
            previous_char = ch

        for kind, seg in self._segments(text):
            if kind == 'enter':
                # textarea/contenteditable 的换行是键盘 Enter，不是一个可直接投递的
                # LF 字符。CRLF 只发送一次，避免 Windows 文本被插入双换行。
                previous_cr = False
                for ch in seg:
                    if ch == '\n' and previous_cr:
                        previous_cr = False
                        continue
                    gap_before(ch)
                    self._tap_vk(W.VK_RETURN, '\r')
                    previous_cr = ch == '\r'
            elif kind == 'cjk':
                for ch in seg:
                    gap_before(ch)
                    self._char_only(ch)
            else:                       # 按目标窗口当前 Caps Lock 决定 Shift
                # Windows 的目标线程只有在真实键态表含 Shift 时，才会生成
                # KeyboardEvent.shiftKey=true。系统修饰键态是唯一需要跨进程协调的
                # 资源，因此锁粒度严格收窄到“单个字符的 Shift down -> 字符 -> up”。
                # 拟人键间停顿必须在锁外，避免一串连续大写让其它浏览器看起来串行。
                shift_vk = W.SharedKeyState.MODS['shift'][0]
                for ch in seg:
                    gap_before(ch)
                    with W.SharedKeyState(self.t.tid, mods=(), text_char=ch) as ks:
                        if not ks.ok:
                            raise RuntimeError('目标窗口大小写与 Shift 键态准备失败')
                        if ks.shift_needed:
                            self._send(W.WM_KEYDOWN, shift_vk, W.key_lparam(shift_vk))
                        try:
                            vk, _ = W.vk_scan(ch)
                            if vk is None:
                                self._char_only(ch)
                            else:
                                self._tap_vk(vk, ch)
                        finally:
                            if ks.shift_needed:
                                self._release_vk(shift_vk)
                            if self._queue_mode:
                                time.sleep(0.08)

    def type_text(self, text, verify=None, clear=None, retries=2):
        """
        输入文本。

        verify() -> bool  由调用方提供，返回当前内容是否等于预期。
        clear()           由调用方提供，用于重试前清空。
        AttachThreadInput 存在偶发竞态（首字符可能丢、键态可能残留），所以这里
        必须校验重试；实测加了校验后可靠。
        """
        with self._act(ascii_layout=True):
            for attempt in range(retries + 1):
                self._type_raw(text)
                if verify is None:
                    return True
                time.sleep(0.12)
                try:
                    if verify():
                        return True
                except Exception:
                    return True         # 页面已变化，无法校验就不纠缠
                if attempt < retries and clear is not None:
                    try:
                        clear()
                    except Exception:
                        break
            return False

    def backspace(self, n=1):
        with self._act():
            for _ in range(max(0, int(n))):
                self._tap_vk(W.VK_BACK)
                time.sleep(self.rng.uniform(0.03, 0.08))

    def clear_by_length(self, length):
        """不使用 Ctrl/SharedKeyState，按当前已读长度从末尾逐字删除。"""

        with self._act():
            self._tap_vk(0x23)  # VK_END
            for _ in range(max(0, int(length))):
                self._tap_vk(W.VK_BACK)
                time.sleep(self.rng.uniform(0.018, 0.055))

    def select_all_delete(self):
        """Ctrl+A + Delete。Ctrl 同样要靠共享键态才能被 Chromium 读到。"""
        with self._act():
            with W.SharedKeyState(self.t.tid, mods=('ctrl',)) as ks:
                if not ks.ok:
                    raise RuntimeError('目标窗口 Ctrl 键态准备失败')
                try:
                    self._send(W.WM_KEYDOWN, W.VK_CONTROL,
                               W.key_lparam(W.VK_CONTROL))
                    vk, _ = W.vk_scan('a')
                    self._tap_vk(vk)
                finally:
                    self._release_vk(W.VK_CONTROL)
                    if self._queue_mode:
                        time.sleep(0.08)
            time.sleep(self.rng.uniform(0.05, 0.12))
            self._tap_vk(W.VK_DELETE)
