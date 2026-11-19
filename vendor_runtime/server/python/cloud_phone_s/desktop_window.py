"""将云手机坐标换算到 DuoPlus 电脑端画面，输入统一走 Win32。"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from io import BytesIO
from pathlib import Path
import threading
import time


class DesktopInputError(RuntimeError):
    pass


@contextmanager
def physical_pixels():
    user32 = ctypes.windll.user32
    change = user32.SetThreadDpiAwarenessContext
    change.argtypes = [ctypes.c_void_p]
    change.restype = ctypes.c_void_p
    previous = change(ctypes.c_void_p(-4))
    try:
        yield
    finally:
        if previous:
            change(previous)


@contextmanager
def visible_input(window):
    from ..driver_s.win32_dp_module_s import win32_native as W
    import win32gui
    def visible(hwnd):
        left, top, right, bottom = window.rect
        x, y = win32gui.ClientToScreen(hwnd, (round((left + right) / 2), round((top + bottom) / 2)))
        return W.point_is_on_window(hwnd, x, y)
    # DuoPlus 的流画面在完全遮挡时可能停止处理输入。只临时改窗口层级，
    # 不激活、不移动系统光标；结束后使用已有窗口层级租约恢复。
    with W.raise_for_wheel(window.hwnd, verify=visible) as raised:
        if not raised.ok or not visible(window.hwnd):
            raise DesktopInputError('云手机画面仍被其他窗口挡住，未发送输入；请关闭遮挡窗口后重试')
        time.sleep(0.2)
        try:
            yield
        finally:
            time.sleep(0.15)


def locate_screen(frame, phone):
    """匹配当前画面，只有图像匹配通过才接受坐标，不能只按窗口比例猜。"""
    from PIL import ImageChops, ImageStat
    frame = frame.convert('RGB')
    phone = phone.convert('RGB')
    sample = phone.resize((54, 96)).crop((4, 10, 50, 86))
    if max(ImageStat.Stat(sample).stddev) < 10:
        raise DesktopInputError('云手机画面过于空白，无法确认操作区域；请显示主页后重新绑定')
    width, height = frame.size
    ratio = phone.width / phone.height
    best = (float('inf'), None)
    for top in range(20, 121, 4):
        for bottom in range(24, 121, 4):
            available = height - top - bottom
            if available < 80:
                continue
            h = min(available, width / ratio)
            w = h * ratio
            left = (width - w) / 2
            y = top + (available - h) / 2
            rect = (round(left), round(y), round(left + w), round(y + h))
            candidate = frame.crop(rect).resize((54, 96)).crop((4, 10, 50, 86))
            score = sum(ImageStat.Stat(ImageChops.difference(sample, candidate)).mean) / 3
            if score < best[0]:
                best = (score, rect)
    if best[0] > 12 or best[1] is None:
        raise DesktopInputError('电脑端画面与云手机截图不一致，不能确定点击位置；请等待画面加载后重新绑定')
    return best[1], round(best[0], 3)


def locate_navigation(frame, rect):
    """仅接受当前客户端的七个独立工具图标布局，布局未知不猜按钮。"""
    left, _, right, bottom = rect
    if not 24 <= frame.height - bottom <= 100:
        return {}
    strip = frame.convert('RGB').crop((left, bottom + 6, right, frame.height - 6))
    pixels = strip.load()
    groups = []
    for x in range(strip.width):
        if sum(max(pixels[x, y]) < 150 for y in range(strip.height)) < 2:
            continue
        if not groups or x - groups[-1][-1] > 12:
            groups.append([x])
        else:
            groups[-1].append(x)
    centers = [(g[0] + g[-1]) / 2 for g in groups]
    if len(centers) != 7 or any(abs(x - strip.width * (i + .5) / 7) > 15 for i, x in enumerate(centers)):
        return {}
    y = bottom + 6 + strip.height / 2
    return {'back': (left + centers[0], y), 'home': (left + centers[1], y)}


class DuoPlusWindow:
    def __init__(self, hwnd, pid, title, created, client_size, dpi, rect, phone_size):
        from ..driver_s.win32_dp_module_s.background_input import BgMouse, BgKeyboard
        import win32process
        self.hwnd, self.pid, self.title, self.created = hwnd, pid, title, created
        self.tid = win32process.GetWindowThreadProcessId(hwnd)[0]
        self.client_size, self.dpi = tuple(client_size), dpi
        self.rect, self.phone_size = tuple(rect), tuple(phone_size)
        self.navigation = {}
        self.off = (0, 0)
        self.win_lock = threading.RLock()
        self.mouse = BgMouse(self, capture_guard=False)
        self.mouse.instant = True
        self.keyboard = BgKeyboard(self)

    @classmethod
    def bind(cls, name, screen):
        import win32gui
        import win32process
        import win32con
        import psutil
        from types import SimpleNamespace
        from PIL import Image
        from ..browser_s.browser_module_s.browser_window import capture_browser_window_by_handle
        candidates = []
        def inspect(hwnd, _):
            title = win32gui.GetWindowText(hwnd)
            if not win32gui.IsWindowVisible(hwnd) or not title.startswith(name + ' - DuoPlus'):
                return
            try:
                pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                process = psutil.Process(pid)
                if Path(process.exe()).name.lower() == 'duoplus.exe':
                    candidates.append((hwnd, pid, title, process.create_time()))
            except (psutil.Error, OSError):
                return
        with physical_pixels():
            win32gui.EnumWindows(inspect, None)
            if len(candidates) != 1:
                raise DesktopInputError(f'请先在 DuoPlus 电脑端打开“{name}”的设备画面；目前找到 {len(candidates)} 个对应窗口，需要唯一窗口')
            hwnd, pid, title, created = candidates[0]
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            if win32gui.IsIconic(hwnd):
                raise DesktopInputError('云手机窗口仍处于最小化状态，请恢复窗口后重新绑定')
            size = win32gui.GetClientRect(hwnd)[2:]
            # 校准只需画面可见，不需要取得系统前台键盘焦点。
            # 新进程可能被 Windows 拒绝激活，沿用输入阶段的无激活 Z 序租约。
            probe = SimpleNamespace(hwnd=hwnd, rect=(0, 0, *size))
            with visible_input(probe):
                image = Image.open(BytesIO(capture_browser_window_by_handle(
                    hwnd, expected_process_id=pid, visible_surface=True)))
                # 公用截图边缘有10像素，客户区偏移来自真实窗口坐标。
                left, top, _, _ = win32gui.GetWindowRect(hwnd)
                origin_x, origin_y = win32gui.ClientToScreen(hwnd, (0, 0))
                x, y = origin_x - left + 10, origin_y - top + 10
                frame = image.crop((x, y, x + size[0], y + size[1]))
                phone = Image.open(BytesIO(base64.b64decode(screen['image'].split(',', 1)[1])))
                rect, score = locate_screen(frame, phone)
                result = cls(hwnd, pid, title, created, size,
                             ctypes.windll.user32.GetDpiForWindow(hwnd), rect, phone.size)
                result.match_score = score
                result.navigation = locate_navigation(frame, rect)
                return result

    def validate(self, screen_size=None):
        import win32gui
        import win32process
        import psutil
        import pywintypes
        if not win32gui.IsWindow(self.hwnd):
            raise DesktopInputError('云手机画面窗口已关闭，请重新打开并绑定')
        try:
            pid = win32process.GetWindowThreadProcessId(self.hwnd)[1]
            same = (pid == self.pid and win32gui.GetWindowText(self.hwnd) == self.title
                    and psutil.Process(pid).create_time() == self.created)
        except (psutil.Error, OSError, pywintypes.error) as exc:
            raise DesktopInputError(f'云手机窗口进程无法验证：{type(exc).__name__}: {exc}；请重新绑定') from exc
        if not same:
            raise DesktopInputError('云手机窗口身份已改变，请重新绑定')
        if win32gui.IsIconic(self.hwnd) or not win32gui.IsWindowVisible(self.hwnd):
            raise DesktopInputError('云手机窗口已最小化或隐藏，请恢复窗口；可以被其他窗口盖住')
        if (win32gui.GetClientRect(self.hwnd)[2:] != self.client_size
                or ctypes.windll.user32.GetDpiForWindow(self.hwnd) != self.dpi
                or (screen_size is not None and tuple(screen_size) != self.phone_size)):
            raise DesktopInputError('窗口大小、显示缩放或手机方向已改变，请重新绑定，避免点击错位')

    def css_to_client(self, x, y):
        width, height = self.phone_size
        if not 0 <= x < width or not 0 <= y < height:
            raise DesktopInputError(f'坐标超出手机画面范围：宽 {width}，高 {height}')
        left, top, right, bottom = self.rect
        return left + x * (right - left) / width, top + y * (bottom - top) / height

    @staticmethod
    def prepare_keyboard_focus(hwnd):
        # 云手机输入框已由 Android 元素树确认焦点。Electron 收到浏览器通用的
        # WM_ACTIVATE 会抢前台，不能再套用网页窗口的焦点刷新消息。
        import win32gui
        return bool(win32gui.IsWindow(hwnd))

    def describe(self):
        return {'hwnd': self.hwnd, 'title': self.title, 'inputMode': 'win32',
                'phoneWidth': self.phone_size[0], 'phoneHeight': self.phone_size[1],
                'canNavigate': bool(self.navigation),
                'message': '已绑定云手机窗口，点击和滑动使用 Win32'}

    def operate(self, operation, values, screen_size):
        with self.win_lock, physical_pixels():
            self.validate(screen_size)
            with visible_input(self):
                return self._operate_visible(operation, values)

    def _operate_visible(self, operation, values):
        with self.win_lock:
            if operation in ('tap', 'swipe'):
                names = ('x', 'y') if operation == 'tap' else ('x', 'y', 'endX', 'endY')
                points = []
                for key in names:
                    value = values.get(key)
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise DesktopInputError(f'坐标 {key} 必须为整数')
                    points.append(value)
                self.css_to_client(*points[:2])
                if operation == 'swipe':
                    self.css_to_client(*points[2:])
                with self.mouse.logical_action():
                    self.mouse.move_to(*points[:2])
                    if operation == 'tap':
                        self.mouse.click()
                    else:
                        self.mouse.drag(*points[2:])
            elif operation in ('home', 'back'):
                if operation not in self.navigation:
                    raise DesktopInputError('没有识别到当前云手机窗口的导航按钮，请使用 DuoPlus 画面上的按钮')
                with self.mouse.logical_action():
                    self.mouse._travel(*self.navigation[operation])
                    self.mouse.click()
            elif operation == 'enter':
                self.keyboard.press(13, char='\r')
            elif operation == 'text':
                text = values.get('text')
                if not isinstance(text, str) or not text or len(text) > 2000 or any(ord(c) < 32 or ord(c) > 126 for c in text):
                    raise DesktopInputError('当前云手机输入支持1至2000个英文、数字、标点')
                self.keyboard.type_text(text, retries=0)
            else:
                raise DesktopInputError(f'当前 Win32 云手机操作不支持：{operation}')
            return {'message': 'Win32 操作已发送，请刷新画面核对', 'inputMode': 'win32'}
