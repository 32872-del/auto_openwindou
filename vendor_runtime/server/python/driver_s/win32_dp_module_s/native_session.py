# -*- coding: utf-8 -*-
"""
会话：把一个 DP tab 绑定到内容窗口 HWND，并维护
  * 隔离世界（DP 内部 JS 的执行上下文，页面钩子看不到）
  * 视口 <-> 客户区 的坐标偏移（注入回读自校准，不猜浏览器 UI 高度）
"""
import random
import string
import threading
import time
from contextlib import nullcontext

from . import cursor_trajectory as _hz
from . import win32_native as W
from .background_input import BgKeyboard, BgMouse
from .coordinate_transform import (
    calibration_offset as _calibration_offset,
    calibration_pair_matches as _calibration_pair_matches,
    calibration_pair_scale as _calibration_pair_scale,
    find_calibration_pair as _find_calibration_pair,
    screen_css_to_physical as _screen_css_to_physical,
)
from .dpi_context import ensure_dpi_context

_LOCK = threading.RLock()
_SESSIONS = {}          # tab_id -> 已完成 HWND 标定的原生输入 Session
_WORLD_SESSIONS = {}    # tab_id -> {(frame_id, target_id): 只读隔离会话}

# 项目硬约束：一个 Chrome 窗口只运行一个自动化 tab。多个 tab 共用同一个内容
# HWND，后台消息无法指定投给其中哪个隐藏 tab，因此发现同 HWND 已被其它 tab 会话
# 占用时直接拒绝绑定，不用 Page.bringToFront 偷偷切 tab。
_WIN_LOCKS = {}         # hwnd -> RLock
_WIN_USERS = {}         # hwnd -> set(tab_id)
_BIND_GENERATIONS = {}  # tab_id -> 单调递增绑定代数


def win_lock(hwnd):
    with _LOCK:
        lk = _WIN_LOCKS.get(hwnd)
        if lk is None:
            lk = _WIN_LOCKS[hwnd] = threading.RLock()
        return lk


def _win_register(hwnd, tab_id):
    with _LOCK:
        _WIN_USERS.setdefault(hwnd, set()).add(tab_id)


def _win_unregister(hwnd, tab_id):
    with _LOCK:
        s = _WIN_USERS.get(hwnd)
        if s:
            s.discard(tab_id)
            if not s:
                _WIN_USERS.pop(hwnd, None)


def win_shared(hwnd):
    """该窗口是否已被多个 tab 会话共用（违反一窗口一 tab 约束）。"""
    with _LOCK:
        return len(_WIN_USERS.get(hwnd, ())) > 1


def win_claimed_by_other(hwnd, tab_id):
    """目标 HWND 是否已属于另一个 tab 会话。"""
    with _LOCK:
        users = _WIN_USERS.get(hwnd, ())
        return bool(users and tab_id not in users)


def _next_binding_generation(tab_id):
    with _LOCK:
        generation = int(_BIND_GENERATIONS.get(tab_id, 0)) + 1
        _BIND_GENERATIONS[tab_id] = generation
        return generation

_CTX_ERR = ('Cannot find context', 'Execution context was destroyed',
            'Inspected target navigated', 'Session with given id not found',
            'uniqueContextId not found')


def _rand_world():
    return '__' + ''.join(random.choice(string.ascii_letters) for _ in range(12))


class Session(object):
    """一个 tab 的原生输入上下文；绑定失败只返回不可用，不触发协议键鼠。"""

    keeper = None                # 仅用于清理旧会话；新会话不再安装常驻捕获线程
    #: 新实现按单次鼠标动作临时捕获；无法捕获时该动作直接延后/失败，不带病注入。
    capture_degraded = False
    _no_keeper = False           # 兼容旧诊断字段
    keeper_fail_reason = ''

    def __init__(self, tab, cfg, expected_top_hwnd=None):
        self.tab = tab
        self.cfg = cfg
        self.ok = False
        self.reason = ''
        self.hwnd = None
        self.pid = None
        self.kind = None
        self.tid = None
        self.binding_generation = 0
        self.dpr = 1.0
        self.off = (0.0, 0.0)
        self._world_name = _rand_world()
        self._world_ctx = None
        self._world_frame = None
        self.rng = random.Random()
        #: 本会话固定的运动人格。真人的运动特征是稳定的，每次移动重新随机
        #: 全部参数会让「同一账号每次运动特征都不同」，这本身就是异常。
        self.profile = _hz.MotionProfile(self.rng)
        self.mouse = None
        self.keyboard = None
        self.cursor_hook = None
        self._cal_window_leaves = 0
        # 默认使用异步 PostMessage。只有页面校准明确证明目标 Chromium 不消费
        # PostMessage 鼠标消息时，才切到带 500ms 上限的 SendMessageTimeout。
        # 两者都是窗口级 Win32 消息，不涉及 CDP/DP 键鼠兜底。
        self._mouse_message_transport = 'post'
        # 浏览器启动层已经按调试端口锁定的顶层 HWND。该值存在时，原生输入
        # 只能在这棵窗口树里寻找页面事件目标，禁止从同一进程族的其它 Chrome
        # 窗口中“猜一个”候选。
        self.expected_top_hwnd = int(expected_top_hwnd or 0) or None
        self._lock = threading.RLock()
        self._bound_root_rect = None

    # ------------------------------------------------------------------ #
    # 隔离世界
    # ------------------------------------------------------------------ #
    def world_ctx(self, force=False, timeout=None):
        """
        返回隔离世界的 executionContextId。

        隔离世界与页面共享 DOM 但 JS 全局完全隔离 —— 页面对 document.evaluate /
        Element.prototype.* / JSON.stringify 的任何劫持都看不到我们的调用。
        导航后上下文会失效，这里按需重建。
        """
        with self._lock:
            fid = getattr(self.tab, '_frame_id', None)
            if not force and self._world_ctx and self._world_frame == fid:
                return self._world_ctx
            if not fid:
                return None
            try:
                args = {
                    'frameId': fid,
                    'worldName': self._world_name,
                    'grantUniveralAccess': False,
                }
                if timeout is not None:
                    args['_timeout'] = max(0.1, float(timeout))
                r = self.tab._run_cdp('Page.createIsolatedWorld', **args)
                self._world_ctx = r.get('executionContextId')
                self._world_frame = fid
                return self._world_ctx
            except Exception:
                self._world_ctx = None
                return None

    def world_value(self, expression, default=None):
        """在隔离世界取一个值；失败返回 default。

        ``eval_world`` 返回的是 ``(ok, 值)`` 二元组，直接拿它跟字符串比较永远
        为假。这个薄封装存在的唯一目的就是避免那类误用。
        """
        ok, value = self.eval_world(expression)
        return value if ok else default

    def eval_world(self, expression, by_value=True, retry=True):
        """在隔离世界执行 JS。失败返回 (False, None)。"""
        ctx = self.world_ctx()
        if ctx is None:
            return False, None
        try:
            r = self.tab._run_cdp('Runtime.evaluate', expression=expression,
                                  contextId=ctx, returnByValue=by_value,
                                  awaitPromise=True, userGesture=False)
        except Exception as e:
            if retry and any(k in str(e) for k in _CTX_ERR):
                self.world_ctx(force=True)
                return self.eval_world(expression, by_value, retry=False)
            return False, None
        if 'exceptionDetails' in r:
            return False, None
        return True, (r.get('result') or {}).get('value')

    def resolve_in_world(self, backend_node_id, timeout=None):
        """把元素解析成隔离世界里的 objectId。"""
        ctx = self.world_ctx(timeout=timeout)
        if ctx is None:
            return None
        try:
            args = {
                'backendNodeId': backend_node_id,
                'executionContextId': ctx,
            }
            if timeout is not None:
                args['_timeout'] = max(0.1, float(timeout))
            r = self.tab._run_cdp('DOM.resolveNode', **args)
            return (r.get('object') or {}).get('objectId')
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 坐标
    # ------------------------------------------------------------------ #
    def css_to_client(self, cx, cy):
        return (cx * self.dpr + self.off[0], cy * self.dpr + self.off[1])

    def client_to_css(self, px, py):
        return ((px - self.off[0]) / self.dpr, (py - self.off[1]) / self.dpr)

    # ------------------------------------------------------------------ #
    # 绑定
    # ------------------------------------------------------------------ #
    def bind(self, attempts=None):
        """
        绑定。首次校准偶尔拿不到 pointermove（页面刚加载、监听器尚未生效），
        所以要重试；实测第 2 次必定成功。

        捕获守卫的退路只在【所有重试都失败之后】才启用 —— 早期版本把它放在单次
        尝试里，结果一次良性的首轮失败就把守卫永久关掉了（实测 4 开时第 4 个
        浏览器因此丢了守卫、泄漏 49 个事件）。守卫关掉意味着真实光标位置会泄漏，
        代价太大，必须先把重试用尽。
        """
        if attempts is None:
            attempts = max(1, int(getattr(self.cfg, 'bind_attempts', 3) or 3))
        with self._lock:
            last = ''
            self._no_keeper = False
            for i in range(attempts):
                try:
                    if self._bind():
                        return True
                    last = self.reason
                except Exception as e:
                    last = 'bind exception: %r' % (e,)
                self._world_ctx = None
                time.sleep(0.25 * (i + 1))

            # 严格后台模式不再退回“无捕获鼠标注入”。短时捕获取不到时宁可让
            # 本次绑定失败并稍后重试，也不能泄漏真实系统光标位置。
            if getattr(self.cfg, 'mouse_capture', True) and \
                    getattr(self.cfg, 'input_mode', 'background') != 'foreground':
                self.keeper_fail_reason = last

            # 前台 SendInput 模式最后可把页面带到前台再试；后台模式禁止这样做，
            # 否则会抢走用户当前应用的焦点。一窗口一 tab + 启动参数应保证后台页
            # 仍可见，条件不满足就明确失败，不偷偷改变桌面焦点。
            try:
                if self.world_value('document.visibilityState') != 'visible':
                    if getattr(self.cfg, 'input_mode', 'background') != 'foreground':
                        last = '%s / background page hidden; refused Page.bringToFront' % last
                    else:
                        self.tab._run_cdp('Page.bringToFront')
                        time.sleep(0.4)
                        if self.world_value('document.visibilityState') == 'visible':
                            self._world_ctx = None
                            if self._bind():
                                return True
                            last = self.reason
            except Exception as e:
                last = '%s / bringToFront 兜底失败: %r' % (last, e)

            self.ok = False
            self.reason = last
            return False

    def _fail(self, why):
        self.ok = False
        self.reason = why
        return False

    def _bind(self):
        if not W.IS_WINDOWS:
            return self._fail('not windows')
        ensure_dpi_context()

        env_ok, env = self.eval_world(
            '({dpr:devicePixelRatio,w:innerWidth,h:innerHeight})')
        if not env_ok or not env:
            return self._fail('cannot read viewport (page not ready?)')
        self.dpr = float(env['dpr']) or 1.0
        want = (int(round(env['w'] * self.dpr)), int(round(env['h'] * self.dpr)))

        pid = None
        try:
            pid = self.tab.browser.process_id
        except Exception:
            pass
        if not pid:
            return self._fail('no browser pid')

        cands = self._candidates(pid, want)
        if not cands:
            return self._fail('no candidate window (pid=%s)' % pid)

        # 逐个候选试，用 screenX 交叉验证证明「这个 HWND 确实在渲染这个页面」。
        # 只按类名/尺寸打分选一个是不够的 —— 同一进程可能有多个窗口。
        tried = []
        fg = getattr(self.cfg, 'input_mode', 'background') == 'foreground'
        # 输入 Desktop 上多窗口只维护每 HWND 虚拟坐标，不争抢全局捕获。
        # 鼠标动作由 BgMouse 的短租约与 CursorStateHook 共同控制 N1 泄漏。
        guard = False
        if fg:
            from .foreground_input import FgMouse, FgKeyboard
            mk_mouse, mk_kbd = FgMouse, FgKeyboard
        else:
            mk_mouse, mk_kbd = BgMouse, BgKeyboard
        current_tab_id = getattr(self.tab, 'tab_id', id(self))
        for hwnd, kind in cands:
            self._close_cursor_hook()
            self._stop_keeper()
            if win_claimed_by_other(hwnd, current_tab_id):
                tried.append('0x%X(%s):window already owns another tab' % (hwnd, kind))
                continue
            self.hwnd, self.kind = hwnd, kind
            self._mouse_message_transport = 'post'
            self.pid = W.process_of(hwnd)
            self.tid = W.thread_of(hwnd)
            self.mouse = mk_mouse(self, rng=self.rng, profile=self.profile,
                                  capture_guard=guard)
            self.keyboard = mk_kbd(self, rng=self.rng, profile=self.profile)
            if fg:
                top = getattr(self.cfg, 'keep_on_top', True)
                self.mouse.keep_on_top = top
                self.keyboard.keep_on_top = top

            # 前台模式使用真实光标，无需后台短时捕获。
            if fg:
                # 前台模式：标定前先把窗口置顶+前台、标签页切到最前。
                # 页面处于 hidden 时 Chromium 不会把输入交给渲染进程，标定必然失败。
                try:
                    from .foreground_input import force_foreground
                    force_foreground(hwnd)
                    self.tab._run_cdp('Page.bringToFront')
                    time.sleep(0.3)
                except Exception:
                    pass
            use_capture = False
            calibrated = self._calibrate(env)
            initial_window_leaves = self._cal_window_leaves
            # 后台会话不能用一次短校准决定是否安装 N1 防护。校准时真实光标
            # 偶然位于目标窗口内，就可能暂时没有 WM_MOUSELEAVE；后续真实动作
            # 仍会出现 pointerout 几乎一比一。严格后台协议因此一律安装受控、
            # HWND/线程/PID 限定的短租约 Hook，安装失败就拒绝绑定，不能随机放行。
            if calibrated and not fg:
                if not self._ensure_cursor_hook():
                    tried.append(
                        '0x%X(%s,cursor_hook_failed):%s'
                        % (hwnd, kind, self.reason)
                    )
                    continue
                if initial_window_leaves >= 2:
                    calibrated = self._calibrate(env)
                    if calibrated and self._cal_window_leaves >= 2:
                        calibrated = self._fail(
                            'TrackMouseEvent hook did not stop repeated window leave'
                        )
            if calibrated:
                self.ok = True
                self.reason = ''
                self.capture_degraded = False
                self.keeper_fail_reason = ''
                self.binding_generation = _next_binding_generation(current_tab_id)
                try:
                    root = int(W.root_of(hwnd) or hwnd)
                    self._bound_root_rect = W.window_rect(root)
                except Exception:
                    self._bound_root_rect = None
                _win_register(hwnd, current_tab_id)
                return True
            tried.append('0x%X(%s,per_operation_capture=%s):%s'
                         % (hwnd, kind, use_capture, self.reason))
        self._stop_keeper()
        self.hwnd = self.pid = self.kind = self.tid = None
        return self._fail('no window passed verification; tried ' + '; '.join(tried))

    def _upgrade_keeper(self, env):
        """旧入口兼容：按操作捕获不需要补装常驻守卫。"""
        self.capture_degraded = False
        self.keeper_fail_reason = ''
        return True

    def _ensure_cursor_hook(self):
        """为已证实存在 N1 的目标 HWND 安装受控短租约 Hook。"""

        if self.cursor_hook is not None:
            return True
        hook = None
        try:
            from .cursor_state_hook import CursorStateHook

            hook = CursorStateHook(
                target_pid=int(self.pid),
                target_tid=int(self.tid),
                target_hwnd=int(self.hwnd),
            )
            hook.install()
            self.cursor_hook = hook
            return True
        except Exception as exc:
            if hook is not None:
                try:
                    hook.close()
                except Exception:
                    pass
            self.reason = 'cursor state hook unavailable: %s' % type(exc).__name__
            return False

    def post_mouse_move(self, x, y, wparam=0):
        """在目标 UI 线程短租约内投递一个后台鼠标移动。"""

        if self.cursor_hook is not None:
            sx, sy = W.client_to_screen(self.hwnd, int(x), int(y))
            self.cursor_hook.arm(sx, sy)
        return self.deliver_mouse_message(
            W.WM_MOUSEMOVE, wparam, W.lparam_xy(x, y)
        )

    def deliver_mouse_message(self, msg, wparam, lparam):
        """按本会话已验证的 Win32 消息传输方式投递鼠标消息。"""

        if self._mouse_message_transport == 'send':
            return W.send(self.hwnd, msg, wparam, lparam)
        return W.post(self.hwnd, msg, wparam, lparam)

    def promote_mouse_message_transport(self):
        """滚轮长时间无页面变化时，后续鼠标消息改用有界同步投递。"""

        if self._mouse_message_transport == 'send':
            return False
        self._mouse_message_transport = 'send'
        return True

    def _stop_keeper(self):
        k = getattr(self, 'keeper', None)
        if k is not None:
            try:
                k.stop()
            except Exception:
                pass
        self.keeper = None
        if getattr(self, 'mouse', None) is not None:
            self.mouse.keeper = None

    def _close_cursor_hook(self):
        hook, self.cursor_hook = self.cursor_hook, None
        if hook is not None:
            try:
                hook.close()
            except Exception:
                pass

    def close(self):
        """会话结束：释放本窗口按键状态，并取消前台模式置顶。"""
        if self.hwnd:
            _win_unregister(self.hwnd, getattr(self.tab, 'tab_id', id(self)))
            # 前台模式把窗口钉在了最上层，收工要还回去，否则用户的浏览器会一直压在最前
            if getattr(self.cfg, 'input_mode', '') == 'foreground':
                try:
                    from .foreground_input import set_topmost
                    set_topmost(self.hwnd, False)
                except Exception:
                    pass
        if (getattr(self.cfg, 'input_mode', '') != 'foreground'
                and getattr(self, 'mouse', None) is not None):
            try:
                self.mouse.release_all()
            except Exception:
                pass
            # 多窗口后台协议从未取得系统鼠标捕获。这里不能“保险性”调用
            # ReleaseCapture：它会临时附加目标线程输入队列，并触碰其它进程正在
            # 使用的桌面级鼠标状态。只补齐本 HWND 可能残留的按钮抬起消息即可。
        self._stop_keeper()
        self._close_cursor_hook()

        close_visual = getattr(getattr(self, 'mouse', None), 'close_visual', None)
        if callable(close_visual):
            close_visual()

    # ------------------------------------------------------------------ #
    @property
    def win_lock(self):
        """按 HWND 的锁：同一窗口的多个 tab 必须串行注入。"""
        return win_lock(self.hwnd)

    def ensure_front(self):
        """
        前台模式兼容入口：必要时把本 tab 切到窗口最前面。

        项目规定一窗口一 tab，正常路径永远直接返回。保留该方法只供前台模式和
        旧入口诊断；若检测到共享 HWND，才尝试兼容切换。
        """
        if not self.ok or not self.hwnd or not win_shared(self.hwnd):
            return True
        # 多窗口协议绝不允许借“共享 HWND 兼容”切 tab。项目契约是一窗口一 tab；
        # 发现共享时应失败并重新绑定，而不是 Page.bringToFront 抢用户前台。
        if getattr(self.cfg, 'input_mode', 'background') != 'foreground':
            return False
        try:
            if self.world_value('document.visibilityState') == 'visible':
                return True
            self.tab._run_cdp('Page.bringToFront')
        except Exception:
            return False
        end = time.time() + 1.0
        while time.time() < end:
            try:
                if self.world_value('document.visibilityState') == 'visible':
                    return True
            except Exception:
                break
            time.sleep(0.02)
        return False

    def _candidates(self, pid, want_client):
        """
        列出该浏览器进程所有可能的内容窗口，按「最可能」排序。

        不硬依赖窗口类名 —— 指纹浏览器可能改名。类名匹配只用来加分，
        真正的判定交给 _calibrate 的 screenX 交叉验证。
        """
        def collect(tops):
            found = []
            for top in tops:
                if not W.user32.IsWindowVisible(top):
                    continue
                cw, ch = W.client_size(top)
                if cw < 200 or ch < 200:      # 排除消息窗口/托盘窗口
                    continue
                cls_ok = W.get_class(top).startswith(W.BROWSER_TOPLEVEL_PREFIX)
                # Chrome 151 起 RenderWidgetHost 可能不再是顶层窗口的直接第一个
                # 子窗口；同一窗口里也可能短暂并存多个旧/新渲染宿主。只取一次
                # FindWindowExW 会漏掉真正接收 WM_MOUSEMOVE 的 HWND。完整枚举后代，
                # 最终仍由 _calibrate 的页面可信 pointermove 做身份验证。
                for child in W.descendant_windows(top):
                    if W.get_class(child) != W.RENDER_WIDGET_CLASS:
                        continue
                    cw_child, ch_child = W.client_size(child)
                    if cw_child <= 1 or ch_child <= 1:
                        continue
                    found.append((child, 'legacy', top, cls_ok))
                found.append((top, 'toplevel', top, cls_ok))
            return found

        # DrissionPage 暴露的 browser.process_id 在 O/Octo 实测就是 Octium.exe
        # 主进程。必须先只枚举这个精确 PID；旧实现一开始就把四层父进程全部混入，
        # 会把 Octo 管理器、explorer.exe 的 ApplicationFrameWindow 当成网页窗口。
        expected_top = int(self.expected_top_hwnd or 0)
        exact_tops = list(W.toplevels_of_pid(pid))
        if expected_top:
            # 启动层给出的句柄可能是内容子窗口，也可能已经失效/被复用。先归一到
            # 顶层根窗口，再校验它属于目标 PID 的有限进程族且确实长得像浏览器。
            # 通过后严格只在这棵窗口树里找事件目标，防止同一厂商多开时跨窗口绑定。
            expected_top = int(W.root_of(expected_top) or expected_top)
            expected_pid = int(W.process_of(expected_top) or 0)
            expected_class = str(W.get_class(expected_top) or '')
            expected_has_render = any(
                W.get_class(child) == W.RENDER_WIDGET_CLASS
                for child in W.descendant_windows(expected_top)
            )
            family_ids = W.process_family_ids(pid)
            expected_valid = bool(
                W.is_alive(expected_top)
                and expected_pid in family_ids
                and (
                    expected_class.startswith(W.BROWSER_TOPLEVEL_PREFIX)
                    or expected_has_render
                )
            )
            if expected_valid:
                ordered_tops = [expected_top]
            else:
                expected_top = 0
                ordered_tops = list(exact_tops)
        else:
            ordered_tops = list(exact_tops)
        raw = collect(ordered_tops)
        if not raw:
            process_ids = W.process_family_ids(pid)
            process_ids.discard(int(pid))
            raw = collect(W.toplevels_of_pids(process_ids))

        # Chrome 窗口类存在时，丢掉同一进程族里的 WinForms/ApplicationFrame 等
        # 明确无关窗口。某些改过类名的指纹浏览器仍可在没有强候选时走校准退路。
        strong = [candidate for candidate in raw if candidate[3]]
        if strong:
            raw = strong

        if not raw:
            return []

        bounds = None
        try:
            r = self.tab.browser._run_cdp('Browser.getWindowForTarget',
                                          targetId=self.tab.tab_id)
            bounds = r.get('bounds')
        except Exception:
            pass

        def score(c):
            hwnd, kind, top, cls_ok = c
            s = 0.0 if cls_ok else 500.0
            # 只有通过 PID/类名/渲染子树校验的 expected_top 才能走到这里；存在时
            # 候选已经被严格限制在这棵窗口树。分数只决定该树内部顶层与内容宿主
            # 的先后顺序，不允许同厂商多开时跨窗口猜测。
            if expected_top and int(top) == expected_top:
                s -= 200.0
            if kind == 'toplevel':
                # 顶层 Chrome_WidgetWin 可以转发普通页面消息，但 OOPIF/frame 的
                # 命中路由并不可靠：PostMessage 返回成功，frame 内元素却收不到
                # pointer/click。优先选择与页面视口尺寸吻合的 RenderWidgetHost；
                # 顶层 HWND 只作没有可用内容宿主时的兼容候选。内容宿主重建后由
                # healthy()/重新绑定恢复，不能为了跨导航稳定牺牲 frame 正确性。
                s += 40.0
            if bounds:
                l, t, r_, b = W.window_rect(top)
                s += abs(l - bounds.get('left', l)) + abs(t - bounds.get('top', t))
                s += abs((r_ - l) - bounds.get('width', r_ - l))
                s += abs((b - t) - bounds.get('height', b - t))
            cw, ch = W.client_size(hwnd)
            s += abs(cw - want_client[0]) * 2.0
            s += max(0, ch - want_client[1]) * 0.05
            s += max(0, want_client[1] - ch) * 5.0   # 客户区比视口还矮 -> 基本不可能
            return s

        raw.sort(key=score)
        return [(c[0], c[1]) for c in raw[:4]]

    # ------------------------------------------------------------------ #
    # 自校准
    # ------------------------------------------------------------------ #
    _CAL_INSTALL = """
    (function(){
      globalThis.__dpsCal = [];
      globalThis.__dpsCalLeave = 0;
      globalThis.__dpsH = function(e){
        globalThis.__dpsCal.push({x:e.clientX, y:e.clientY, sx:e.screenX, sy:e.screenY,
                                  trusted:e.isTrusted});
        if(globalThis.__dpsCal.length > 12) globalThis.__dpsCal.shift();
      };
      globalThis.__dpsLeaveH = function(e){
        if(e.target===document.documentElement && e.isTrusted && !e.relatedTarget){
          globalThis.__dpsCalLeave += 1;
        }
      };
      document.addEventListener('pointermove', globalThis.__dpsH, {capture:true, passive:true});
      document.addEventListener('pointerleave', globalThis.__dpsLeaveH, {capture:true, passive:true});
      return 1;
    })()
    """
    _CAL_READ = "globalThis.__dpsCal"
    _CAL_LEAVE_READ = "globalThis.__dpsCalLeave"
    _CAL_REMOVE = """
    (function(){
      try{ document.removeEventListener('pointermove', globalThis.__dpsH, true); }catch(e){}
      try{ document.removeEventListener('pointerleave', globalThis.__dpsLeaveH, true); }catch(e){}
      globalThis.__dpsH = null; globalThis.__dpsLeaveH = null;
      globalThis.__dpsCal = null; return 1;
    })()
    """

    def _calibrate(self, env):
        """
        注入一组已知客户区坐标的 mousemove，从页面回读 clientX/Y 反推比例与偏移，
        并做三重交叉验证，确保绑定到的确实是渲染本页面的那个窗口：

          1. 两次探测推出的偏移必须一致（±1px）
          2. 页面报告的 screenX/Y 必须等于 ClientToScreen(hwnd, 投递点)
             —— 这一条最强，它证明事件确实是经由这个 HWND 进来的
          3. screenX 必须不等于 clientX（否则说明走的还是 CDP 路径）

        偏移量不硬算浏览器 UI 高度 —— 书签栏/侧边栏/缩放都会改变它。
        """
        fg = getattr(self.cfg, 'input_mode', 'background') == 'foreground'
        self._cal_window_leaves = 0
        ok, _ = self.eval_world(self._CAL_INSTALL)
        if not ok:
            return self._fail('cannot install calibration hook')
        try:
            cw, ch = W.client_size(self.hwnd)
            vh = int(round(env['h'] * self.dpr))
            base_y = max(1, ch - vh)
            base_x = int(cw * 0.5)
            probe_y = base_y + int(vh * 0.45)
            # pointermove 不会从跨域 iframe 冒泡到外层校准监听器。优先选择
            # 外层文档可观测到的区域，避免固定中心点恰好全部落入 iframe。
            anchor_ok, anchor = self.eval_world("""(() => {
              const shifts=[[0,0],[23,19],[-17,31],[29,-13]],dpr=devicePixelRatio||1;
              const sites=[[.5,.45],[.5,.12],[.25,.12],[.75,.12],
                           [.12,.5],[.88,.5],[.5,.85]];
              for(const [fx,fy] of sites){
                const x=innerWidth*fx,y=innerHeight*fy;
                if(shifts.every(([dx,dy])=>{
                  const px=x+dx/dpr,py=y+dy/dpr;
                  let node=document.elementFromPoint(px,py);
                  while(node&&node.shadowRoot){
                    const child=node.shadowRoot.elementFromPoint(px,py);
                    if(!child||child===node)break;node=child;
                  }
                  return node&&!['IFRAME','FRAME','OBJECT','EMBED'].includes(node.tagName);
                }))return [x,y];
              }
              return null;
            })()""")
            if anchor_ok and isinstance(anchor, (list, tuple)) and len(anchor) == 2:
                base_x = int(round(float(anchor[0]) * self.dpr))
                probe_y = base_y + int(round(float(anchor[1]) * self.dpr))
            # 后两点是校准冗余：首轮偶尔会被真实光标/捕获守卫的启动杂讯占掉，
            # 至少再保留一组成对位移，避免把一次无关事件永久当成第一个探针。
            probes = [(base_x, probe_y),
                      (base_x + 23, probe_y + 19),
                      (base_x - 17, probe_y + 31),
                      (base_x + 29, probe_y - 13)]
            samples_by_probe = []
            results = None
            last_seen = None
            # 多窗口并发时，其他窗口可能正在持有短时鼠标捕获。校准若裸发
            # WM_MOUSEMOVE，消息会被已有捕获窗口接走，当前页面一条事件也收不到，
            # 随后整个会话被误判为“无法绑定”。后台校准与普通鼠标动作使用同一套
            # 短时捕获/让行逻辑；前台模式保持原路径。
            calibration_guard = (
                self.mouse._act(touch=False)
                if getattr(self.cfg, 'input_mode', 'background') != 'foreground'
                else nullcontext()
            )
            with calibration_guard:
                for probe_index, (px, py) in enumerate(probes):
                    self.eval_world('globalThis.__dpsCal=[]')
                    self.mouse._x = self.mouse._y = None
                    if fg:
                        # 前台协议必须用真实 SendInput 标定；旧实现虽然已经把窗口
                        # 切到前台，却仍固定 PostMessage，Chrome 151 会完全忽略该
                        # 后台消息，导致“单窗口协议”在输入前就错误判定无法绑定。
                        if not self.mouse._goto(px, py):
                            return self._fail('SendInput calibration failed')
                    elif not self.post_mouse_move(px, py, 0):
                        return self._fail('PostMessage failed')
                    trusted_samples = []
                    send_probe_attempted = False
                    for read_index in range(16):
                        time.sleep(0.03)
                        ok2, v = self.eval_world(self._CAL_READ)
                        if ok2 and v:
                            samples = v if isinstance(v, list) else [v]
                            last_seen = samples[-1]
                            trusted_samples = [sample for sample in samples
                                               if isinstance(sample, dict)
                                               and sample.get('trusted') is not False]
                            current = samples_by_probe + [trusted_samples]
                            results = _find_calibration_pair(current, probes, self.dpr)
                            if results:
                                break
                            # 第一轮只负责收集锚点；拿到样本后多读一次，既吸收同一批
                            # 合并事件，也避免无意义地固定等待完整超时。
                            if probe_index == 0 and trusted_samples:
                                time.sleep(0.03)
                                ok3, extra = self.eval_world(self._CAL_READ)
                                if ok3 and extra:
                                    extra = extra if isinstance(extra, list) else [extra]
                                    trusted_samples = [sample for sample in extra
                                                       if isinstance(sample, dict)
                                                       and sample.get('trusted') is not False]
                                break
                        # BitBrowser 等部分 Chromium 分支会接受有界的
                        # SendMessageTimeout，却完全忽略后台 PostMessage。并发启动时
                        # Chromium UI 线程可能超过 240ms 才消费异步队列；过早切换到
                        # send 会让之后每个 move 都逐条同步派发，丢失浏览器正常的事件
                        # 合并特征。先给 PostMessage 约 390ms，再清空页面采样并做一次
                        # 同步验证，不能靠厂商名称或一次调度延迟猜测输入方式。
                        if (
                            read_index == 12
                            and not trusted_samples
                            and self._mouse_message_transport == 'post'
                        ):
                            self.eval_world('globalThis.__dpsCal=[]')
                            send_probe_attempted = bool(W.send(
                                self.hwnd,
                                W.WM_MOUSEMOVE,
                                0,
                                W.lparam_xy(px, py),
                            ))
                    if trusted_samples and send_probe_attempted:
                        self._mouse_message_transport = 'send'
                    samples_by_probe.append(trusted_samples)
                    if results:
                        break

            if not results:
                return self._fail(
                    'no matching pointermove pattern from this window; '
                    'last=%s probesTried=%s'
                    % (last_seen, probes))

            (p0x, p0y, g0), (p1x, p1y, g1) = results
            input_scale = _calibration_pair_scale(
                g0, g1, (p0x, p0y), (p1x, p1y),
            )
            if input_scale is None:
                return self._fail('matched calibration pair lost its input scale')
            off0 = (_calibration_offset(p0x, g0['x'], input_scale),
                    _calibration_offset(p0y, g0['y'], input_scale))
            off1 = (_calibration_offset(p1x, g1['x'], input_scale),
                    _calibration_offset(p1y, g1['y'], input_scale))
            if abs(off0[0] - off1[0]) > 1.5 or abs(off0[1] - off1[1]) > 1.5:
                return self._fail('offset unstable %s vs %s' % (off0, off1))

            # 页面 screen 坐标原点可由指纹环境伪装，但两次探针反推出的原点偏差
            # 必须稳定。保存该值用于诊断，不把第二屏/虚拟屏的正常伪装误判为失败。
            origins = []
            for px, py, got_event in results:
                exp_sx, exp_sy = W.client_to_screen(self.hwnd, px, py)
                origins.append((
                    exp_sx - _screen_css_to_physical(got_event['sx'], input_scale),
                    exp_sy - _screen_css_to_physical(got_event['sy'], input_scale),
                ))
            if abs(origins[0][0] - origins[1][0]) > 2.5 or \
                    abs(origins[0][1] - origins[1][1]) > 2.5:
                return self._fail('screen origin unstable %s vs %s' % tuple(origins))

            self.dpr = input_scale
            self.off = off1
            self.mouse._x, self.mouse._y = p1x, p1y
            self._cal_screen = (g1.get('sx'), g1.get('sy'))
            self._cal_screen_origin = origins[1]
            leaves_ok, leaves = self.eval_world(self._CAL_LEAVE_READ)
            self._cal_window_leaves = (
                max(0, int(leaves or 0)) if leaves_ok else 0
            )
            return True
        finally:
            self.eval_world(self._CAL_REMOVE)

    def recalibrate_after_window_move(self):
        """用户移动窗口稳定后重新读取 DPR，并校准当前 HWND 的客户区坐标。"""

        with self._lock:
            env_ok, env = self.eval_world(
                '({dpr:devicePixelRatio,w:innerWidth,h:innerHeight})')
            if not env_ok or not env or not self.hwnd or not W.is_alive(self.hwnd):
                self.ok = False
                self.reason = 'window moved; viewport unavailable for recalibration'
                return False
            self.dpr = float(env.get('dpr') or 1.0)
            if not self._calibrate(env):
                self.ok = False
                self.reason = self.reason or 'window moved; recalibration failed'
                return False
            try:
                root = int(W.root_of(self.hwnd) or self.hwnd)
                self._bound_root_rect = W.window_rect(root)
            except Exception:
                self.ok = False
                self.reason = 'window moved; geometry unavailable after recalibration'
                return False
            self.ok = True
            self.reason = ''
            return True

    # ------------------------------------------------------------------ #
    def healthy(self):
        if not self.ok:
            return False
        if not W.is_alive(self.hwnd):
            self.ok = False
            self.reason = 'hwnd gone'
            return False
        if self.pid and W.process_of(self.hwnd) != self.pid:
            self.ok = False
            self.reason = 'hwnd replaced'
            return False
        if self._bound_root_rect is not None:
            try:
                root = int(W.root_of(self.hwnd) or self.hwnd)
                if tuple(W.window_rect(root)) != tuple(self._bound_root_rect):
                    self.reason = 'window geometry changed; recalibration required'
                    # 用户拖动窗口只改变顶层屏幕位置，不会替换已验证的内容 HWND。
                    # 先等待 Windows 移窗循环结束，再在原会话内重标定；若直接把
                    # Session 标成失效，下一次动作会在拖动尚未稳定时完整重绑，容易
                    # 让滚动预检读到一半旧坐标、一半新坐标。
                    W.wait_for_window_geometry_stable(root)
                    if not self.recalibrate_after_window_move():
                        return False
            except Exception:
                self.ok = False
                self.reason = 'window geometry unavailable'
                return False
        return True

    def invalidate(self):
        with self._lock:
            self.ok = False
            self._world_ctx = None


# ---------------------------------------------------------------------- #
#: tab_id -> 上一次绑定失败的时刻。绑定要标定窗口、投递探针、等页面回读，
#: 单轮代价在几百毫秒量级。没有退避的话，一个绑不上的标签页会让**每一次**
#: CDP JS 调用都重跑一轮标定，整个驱动会慢到不可用。
_BIND_FAILED_AT = {}
_BIND_FAILED_REASON = {}

#: 本线程正在绑定的 tab。绑定过程本身要发 CDP（建隔离世界、读视口、投探针、
#: 回读 pointermove），这些调用会再次经过 CDP 拦截点，拦截点又会来取会话 ——
#: 没有这道防重入，同一个线程会在 bind() 里无限递归下去。
_BINDING = threading.local()


def _binding_ids():
    ids = getattr(_BINDING, 'ids', None)
    if ids is None:
        ids = _BINDING.ids = set()
    return ids


def _bind_backoff(cfg):
    try:
        return max(0.0, float(getattr(cfg, 'bind_retry_interval', 5.0)))
    except Exception:
        return 5.0


def get_session(tab, cfg, create=True, expected_top_hwnd=None):
    """取（或建立）tab 的注入会话。失败返回 None，调用方退回 CDP。

    ``create=False`` 表示「只查不建」：既不新建会话，也不触发窗口绑定。

    绑定期间本线程再次进来会直接返回 None（见 ``_BINDING``）—— 那些调用是绑定
    自己发出的 CDP，本来就该按原样走主世界。
    """
    if not W.IS_WINDOWS or tab is None or cfg is None:
        return None
    try:
        tid = tab.tab_id
    except Exception:
        return None
    if tid in _binding_ids():
        return None
    with _LOCK:
        s = _SESSIONS.get(tid)
        if s is not None and expected_top_hwnd:
            expected = int(expected_top_hwnd)
            if s.expected_top_hwnd not in (None, expected):
                s.close()
                s = None
                _SESSIONS.pop(tid, None)
            elif s is not None:
                s.expected_top_hwnd = expected
        if s is not None and s.healthy():
            return s
        if not create:
            return None
        failed_at = _BIND_FAILED_AT.get(tid)
        if failed_at is not None and time.time() - failed_at < _bind_backoff(cfg):
            return None
        if s is None:
            # 第三个参数保持位置传递，兼容诊断/测试中只接收 ``*args`` 的轻量
            # Session 替身，同时正式实现仍得到启动会话锁定的顶层 HWND。
            if getattr(tab, 'native_query_page', False) is True:
                from ..query_win32_s.session import QuerySession
                s = QuerySession(tab, cfg, expected_top_hwnd)
            else:
                s = Session(tab, cfg, expected_top_hwnd)
            _SESSIONS[tid] = s
    ids = _binding_ids()
    ids.add(tid)
    try:
        bound = s.bind()
    finally:
        ids.discard(tid)
    if bound:
        with _LOCK:
            _BIND_FAILED_AT.pop(tid, None)
            _BIND_FAILED_REASON.pop(tid, None)
        try:
            from .input_behavior import start_idle
            start_idle(s, cfg)
        except Exception:
            pass
        return s
    with _LOCK:
        _BIND_FAILED_AT[tid] = time.time()
        _BIND_FAILED_REASON[tid] = s.reason or 'native session bind failed'
    if getattr(cfg, 'debug', False):
        try:
            from server.python.log_s import log

            log.warning(
                '[Win32DPInput] native session bind failed',
                module='驱动',
                operation='Win32窗口绑定',
            )
        except Exception:
            pass
    return None


def get_world_session(tab, cfg):
    """获取只承载隔离世界的轻量会话，不触发 Win32 窗口绑定。

    ``dp`` 旧协议的元素状态查询也需要隔离世界，但不需要 HWND、pointermove
    标定或后台键鼠。若复用 :func:`get_session`，隔离 JS 会错误依赖原生输入
    会话，窗口尚未标定时连普通 DP 查询都会被关闭失败。

    ``win32_dp`` 的鼠标键盘仍只允许走 :func:`get_session`，本入口不设置
    ``ok``、``hwnd``、``mouse`` 或 ``keyboard``，不能被当成原生输入成功。
    """

    if tab is None or cfg is None:
        return None
    try:
        tid = tab.tab_id
    except Exception:
        return None
    with _LOCK:
        session = _SESSIONS.get(tid)
        # ChromiumFrame.tab_id 仍是外层标签页ID；只有同一个查询对象才能复用
        # 原生会话的执行上下文，不能把跨域frame对象映射到外层页面。
        if session is not None and session.tab is tab:
            return session
        frame_id = getattr(tab, '_frame_id', None)
        driver = getattr(tab, 'driver', None)
        key = (frame_id, getattr(driver, 'id', None))
        worlds = _WORLD_SESSIONS.setdefault(tid, {})
        session = worlds.get(key)
        if session is None or session.tab is not tab:
            session = Session(tab, cfg)
            worlds[key] = session
        return session


def session_binding_in_progress(tab) -> bool:
    """当前 tab 是否正在建立原生会话。

    绑定自身发出的初始化 CDP 必须原样通过，否则隔离世界拦截会递归阻断页面启动。
    """

    try:
        return tab.tab_id in _binding_ids()
    except Exception:
        return False


def session_unavailable_reason(tab, cfg=None):
    """返回最近一次原生会话不可用原因，不触发重新绑定。"""

    if not W.IS_WINDOWS:
        return 'not-windows'
    if tab is None:
        return 'tab-unavailable'
    if cfg is None:
        return 'native-config-unavailable'
    try:
        tid = tab.tab_id
    except Exception:
        return 'tab-id-unavailable'
    if tid in _binding_ids():
        return 'native-session-binding'
    with _LOCK:
        session = _SESSIONS.get(tid)
        reason = _BIND_FAILED_REASON.get(tid)
        failed_at = _BIND_FAILED_AT.get(tid)
    if session is not None and session.reason:
        reason = session.reason
    if reason:
        if failed_at is not None and time.time() - failed_at < _bind_backoff(cfg):
            return '%s; retry-backoff' % reason
        return reason
    if session is not None and not session.ok:
        return 'native-session-not-ready'
    return 'native-session-unavailable'


def drop_session(tab_id):
    with _LOCK:
        s = _SESSIONS.pop(tab_id, None)
        world_sessions = _WORLD_SESSIONS.pop(tab_id, {})
        _BIND_FAILED_AT.pop(tab_id, None)
        _BIND_FAILED_REASON.pop(tab_id, None)
    if s is not None:
        try:
            from .input_behavior import stop_idle
            stop_idle(s)
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass
    for world_session in world_sessions.values():
        if world_session is not s:
            world_session._world_ctx = None
            world_session._world_frame = None


def drop_all_sessions():
    """会话全部收工：放掉捕获、断开输入队列附加、取消置顶。"""
    with _LOCK:
        ids = list(set(_SESSIONS) | set(_WORLD_SESSIONS))
    for tab_id in ids:
        drop_session(tab_id)


def all_sessions():
    with _LOCK:
        return list(_SESSIONS.values())
