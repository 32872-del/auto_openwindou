"""云手机网页版的隔离查看器。进程、窗口、键鼠和截图归属同一Desktop。"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
from queue import Queue
import shutil
import socket
import subprocess
import threading
import time
import uuid
import logging
from urllib.parse import quote


class ViewerError(RuntimeError):
    pass


def chrome_app_url(url: str) -> str:
    """Chrome reads --app through GetSwitchValueASCII, even on Windows.

    Keep URL delimiters, existing escapes and signed tokens byte-for-byte;
    encode Chinese names/spaces without parsing and rebuilding the query.
    """
    return quote(url, safe=":/?#[]@!$&'()*+,;=%")


def is_blank_chrome_title(title: str) -> bool:
    value = title.strip().casefold()
    for suffix in (' - google chrome', ' - chromium'):
        if value.endswith(suffix):
            value = value[:-len(suffix)].strip()
    return value in ('', 'google chrome', 'chromium', '新标签页', '新分頁',
                     '新分頁標籤', 'new tab', 'about:blank', 'chrome://newtab',
                     'chrome://newtab/', 'chrome://new-tab-page/')


def chrome_program() -> Path:
    import os
    candidates = [shutil.which('chrome.exe')]
    for base in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        value = os.environ.get(base)
        if value:
            candidates.append(str(Path(value) / 'Google/Chrome/Application/chrome.exe'))
    for value in candidates:
        if value and Path(value).is_file():
            return Path(value).resolve()
    raise ViewerError('云手机隔离画面需要本机Google Chrome，请安装后重新打开')


class IsolatedViewer:
    """一台设备一个工作线程/Desktop/Chrome进程；不接管已运行的浏览器。"""

    def __init__(self, profile_root: Path, url: str, *, device_identity: str = '', manual: bool = False, visible: bool = False):
        if visible and not manual:
            raise ValueError('当前桌面查看器仅用于人工操作')
        self.desktop_name = 'AutoDrission_cloud_' + uuid.uuid4().hex[:20]
        self.profile = Path(profile_root).resolve() / self.desktop_name
        self.url = url
        self.device_identity = device_identity
        self.manual = manual
        self.visible = visible
        self._requests = Queue()
        self._ready = Future()
        self._closed = threading.Event()
        self._unusable = threading.Event()
        self._guard = threading.Lock()
        self._job = None
        self._session = self._browser = self._tab = None
        self.cleanup_errors = []
        self.hwnd = self.pid = self.port = 0
        self._thread = threading.Thread(target=self._run, name=self.desktop_name, daemon=True)

    def start(self, timeout=40):
        self._thread.start()
        try:
            self._ready.result(timeout)
            return self
        except BaseException:
            self.close()
            raise

    def call(self, operation, *args, timeout=30):
        if operation == 'text' and args and isinstance(args[0], str) and 1 <= len(args[0]) <= 2000:
            # 隔离键态至少保留0.44秒/字符；手机不加拟人停顿，但不能套固定45秒。
            timeout = max(timeout, 30 + len(args[0]) * 0.6)
        with self._guard:
            if self._unusable.is_set() or self._closed.is_set():
                raise ViewerError('云手机隔离窗口已关闭或上次操作超时，请重新打开')
            result = Future()
            self._requests.put((operation, args, result))
        try:
            return result.result(timeout)
        except FutureTimeout as exc:
            # 不重发输入，也不允许下一请求与尚未结束的输入重叠。
            self._unusable.set()
            self.close()
            raise ViewerError(f'云手机隔离窗口操作{operation}超过{timeout:g}秒，已停止此窗口；未重复输入') from exc

    def close(self):
        with self._guard:
            self._unusable.set()
            self._requests.put(None)
        if threading.current_thread() is self._thread:
            return
        self._thread.join(4)
        if self._thread.is_alive():
            import win32job
            with self._guard:
                if self._job is not None:
                    # Job只包含由本查看器以CREATE_SUSPENDED启动并归入的进程树。
                    win32job.TerminateJobObject(self._job, 1)
            self._thread.join(2)
        if self._thread.is_alive():
            message = '云手机查看器进程已终止，但工作线程尚未结束：' + self.desktop_name
            self.cleanup_errors.append(message)
            logging.getLogger(__name__).error(message)
        return self._closed.is_set()

    def _run(self):
        import win32api
        import win32con
        import win32gui
        import win32job
        import win32process
        import win32service
        import win32event
        from ..driver_s.win32_dp_module_s.win32_native import current_desktop_name
        desktop = process = thread = None
        device_mutex = None
        owns_device = False
        previous = win32service.GetThreadDesktop(win32api.GetCurrentThreadId())
        try:
            if self.device_identity:
                import hashlib
                key = hashlib.sha256(self.device_identity.encode()).hexdigest()
                device_mutex = win32event.CreateMutex(None, False, 'Global\\AutoDrission_DuoPlus_' + key)
                result = win32event.WaitForSingleObject(device_mutex, 0)
                if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                    raise ViewerError('这台云手机已被另一个窗口或任务占用，请先关闭原隔离窗口；未发送操作')
                owns_device = True
            if self.visible:
                self.desktop_name = current_desktop_name()
            else:
                desktop = win32service.CreateDesktop(self.desktop_name, 0, win32con.GENERIC_ALL, None)
                desktop.SetThreadDesktop()
            if current_desktop_name() != self.desktop_name:
                raise ViewerError('云手机线程未进入专属Desktop，拒绝启动到普通桌面')
            chrome = chrome_program()
            self.profile.mkdir(parents=True, exist_ok=False)
            port = 0
            if not self.manual:
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    port = reservation.getsockname()[1]
            self.port = port
            command = [str(chrome), f'--user-data-dir={self.profile}', f'--remote-debugging-port={port}',
                '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check',
                '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding',
                '--disable-background-timer-throttling', '--window-size=1100,950', '--window-position=0,0',
                'about:blank']
            if self.manual:
                command=[arg for arg in command if not arg.startswith('--remote-debugging') and arg!='about:blank']
                command.append('--app='+chrome_app_url(self.url))
                self.port=0
            startup = win32process.STARTUPINFO()
            startup.lpDesktop = 'winsta0\\' + self.desktop_name
            job = win32job.CreateJobObject(None, self.profile.name + '_job')
            limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
            with self._guard:
                self._job = job
            process, thread, self.pid, _ = win32process.CreateProcess(str(chrome),
                subprocess.list2cmdline(command), None, None, False,
                win32con.CREATE_NEW_PROCESS_GROUP | win32con.CREATE_SUSPENDED,
                None, str(chrome.parent), startup)
            win32job.AssignProcessToJobObject(job, process)
            win32process.ResumeThread(thread)
            thread.Close(); thread = None
            if not self.manual:
                from DrissionPage import Chromium, ChromiumOptions
                options = ChromiumOptions().set_address(f'127.0.0.1:{port}').existing_only(True)
                self._browser = Chromium(options)
                if self._browser.process_id != self.pid:
                    raise ViewerError('云手机查看器连接端口不属于本次启动进程，拒绝接管')
                self._tab = self._browser.latest_tab
                self._tab.set.timeouts(base=5, page_load=25, script=5)
                self._tab.get(self.url, timeout=25)
            candidates = []
            def inspect(hwnd, _):
                if (win32process.GetWindowThreadProcessId(hwnd)[1] == self.pid
                        and win32gui.IsWindowVisible(hwnd)
                        and win32gui.GetClassName(hwnd) == 'Chrome_WidgetWin_1'):
                    candidates.append(hwnd)
            win32gui.EnumWindows(inspect, None)
            deadline=time.monotonic()+25
            while self.manual and not candidates and time.monotonic()<deadline:
                if win32event.WaitForSingleObject(process, 0) == win32event.WAIT_OBJECT_0:
                    raise ViewerError('云手机控制页浏览器提前退出（退出码 '+str(win32process.GetExitCodeProcess(process))+'）；请确认本机 Chrome 可以正常启动')
                time.sleep(.1);win32gui.EnumWindows(inspect,None)
            if len(candidates) > 1 and not self.manual:
                # 厂商入口可能新开控制页；只在本次专属Chrome内匹配唯一标题，不能按窗口顺序猜。
                self._tab = self._browser.latest_tab
                title = str(self._tab.title or '').strip()
                matched = [h for h in candidates if title and win32gui.GetWindowText(h).startswith(title + ' - ')]
                if len(matched) == 1:
                    candidates = matched
            if len(candidates) != 1:
                titles = [win32gui.GetWindowText(h) for h in candidates]
                surface='当前桌面' if self.visible else '隔离桌面'
                raise ViewerError(f'{surface}内找到{len(candidates)}个查看器窗口，无法唯一绑定：{titles}')
            self.hwnd = candidates[0]
            self._validate()
            if self.manual:
                self._wait_manual_page()
            if self.visible:
                self._operate('show')
            self._ready.set_result(True)
            while True:
                item = self._requests.get()
                if item is None:
                    break
                operation, args, result = item
                if self._unusable.is_set():
                    result.set_exception(ViewerError('云手机隔离查看器正在关闭'))
                    continue
                try:
                    result.set_result(self._operate(operation, *args))
                except Exception as exc:
                    result.set_exception(exc)
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            self._unusable.set()
            def clean(label, action):
                try:
                    action()
                except Exception as exc:
                    message = f'{label}：{type(exc).__name__}: {exc}'
                    self.cleanup_errors.append(message)
                    logging.getLogger(__name__).warning(message, exc_info=True)
            if self._session:
                clean('关闭云手机输入会话失败', self._session.close)
            if self._browser:
                clean('关闭云手机查看器协议失败', lambda: self._browser.quit(timeout=2))
            with self._guard:
                if self._job is not None:
                    clean('关闭云手机进程组失败', self._job.Close)
                    self._job = None
            # AssignProcessToJobObject失败时尚未恢复运行，也要回收自有进程。
            if process:
                import win32event
                def release_process():
                    try:
                        if win32event.WaitForSingleObject(process, 1000) != 0:
                            win32process.TerminateProcess(process, 1)
                    finally:
                        process.Close()
                clean('回收云手机进程句柄失败', release_process)
            if thread: clean('回收云手机线程句柄失败', thread.Close)
            clean('恢复原桌面失败', previous.SetThreadDesktop)
            if desktop: clean('回收云手机隔离桌面失败', desktop.CloseDesktop)
            if device_mutex:
                if owns_device: clean('释放云手机占用失败', lambda: win32event.ReleaseMutex(device_mutex))
                clean('回收云手机占用句柄失败', device_mutex.Close)
            while not self._requests.empty():
                item = self._requests.get_nowait()
                if item is not None and not item[2].done():
                    item[2].set_exception(ViewerError('云手机隔离查看器已结束'))
            self._closed.set()

    def _wait_manual_page(self, timeout=10):
        """Reject the known blank-window fallback, without opening a CDP session.

        A page title does not prove that the remote phone video is connected.
        DuoPlus may still display its official access/password screen.
        """
        import win32gui
        deadline = time.monotonic() + timeout
        while True:
            self._validate()
            if self._unusable.is_set():
                raise ViewerError('云手机控制页打开已取消')
            if not is_blank_chrome_title(win32gui.GetWindowText(self.hwnd)):
                return
            if time.monotonic() >= deadline:
                raise ViewerError('Chrome 只打开了空白页或新标签页，云手机控制页未打开；请重新打开该记录')
            time.sleep(.1)

    def _validate(self):
        import win32gui
        import win32process
        from ..driver_s.win32_dp_module_s.win32_native import current_desktop_name, window_desktop_name
        if (not win32gui.IsWindow(self.hwnd)
                or win32process.GetWindowThreadProcessId(self.hwnd)[1] != self.pid
                or window_desktop_name(self.hwnd) != self.desktop_name
                or current_desktop_name() != self.desktop_name):
            self._unusable.set()
            raise ViewerError('云手机窗口或Desktop身份已经改变，已阻止继续输入')

    def _input(self):
        if self._session is not None and not self._session.healthy():
            raise ViewerError('隔离查看器输入窗口或尺寸已失效：' + str(self._session.reason))
        if self._session is None:
            from ..driver_s.drission_s.stealth_module_s.config import StealthConfig
            from ..driver_s.win32_dp_module_s.native_session import Session
            config = StealthConfig()
            config.idle_motion = False
            config.mouse_capture = False
            config.bind_attempts = 1
            session = Session(self._tab, config, expected_top_hwnd=self.hwnd)
            if not session.bind(attempts=1):
                session.close()
                raise ViewerError('隔离查看器Win32绑定失败：' + str(session.reason))
            session.mouse.instant = True
            session.keyboard.human_timing = False
            self._session = session
        return self._session

    def _operate(self, operation, *args):
        from .desktop_window import physical_pixels
        self._validate()
        with physical_pixels():
            if operation == 'show' and self.visible:
                import pywintypes, win32con, win32gui
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
                try:
                    win32gui.SetForegroundWindow(self.hwnd)
                except (pywintypes.error, OSError):
                    # pywintypes.error 不继承 OSError。前台锁拒绝激活不代表
                    # 控制页失败，不能因此关闭窗口并触发云手机关机。
                    self._validate()
                    logging.getLogger(__name__).info('Windows 未允许云手机控制窗口切到前台，保留窗口并提示用户点击任务栏')
                    try:
                        win32gui.FlashWindow(self.hwnd, True)
                    except (pywintypes.error, OSError):
                        logging.getLogger(__name__).info('云手机控制窗口任务栏提示未成功，继续检查窗口可见性')
                self._validate()
                if not win32gui.IsWindowVisible(self.hwnd) or win32gui.IsIconic(self.hwnd):
                    raise ViewerError('云手机控制页未能恢复为可见窗口，请重新打开该记录')
                return True
            if operation == 'describe':
                return {'hwnd': self.hwnd, 'pid': self.pid, 'desktop': self.desktop_name,
                        'inputMode': 'manual' if self.manual else 'win32', 'isolated': not self.visible}
            if operation == 'query':
                # 仅供执行端内部查询控制页；不会通过客户端API开放任意JS。
                return self._tab.run_js(*args)
            if operation == 'capture':
                from ..browser_s.browser_module_s.browser_window import capture_browser_window_by_handle
                return capture_browser_window_by_handle(self.hwnd, expected_process_id=self.pid)
            if operation == 'captureRegion':
                from io import BytesIO
                from PIL import Image
                import win32gui
                from ..browser_s.browser_module_s.browser_window import capture_browser_window_by_handle
                x,y,width,height,target_width,target_height=args
                session=self._input()
                left,top=session.css_to_client(x,y)
                right,bottom=session.css_to_client(x+width,y+height)
                window_left,window_top,_,_=win32gui.GetWindowRect(self.hwnd)
                # css_to_client 属于实际输入子窗口（通常是 RenderWidget），不是顶层浏览器客户区。
                origin=win32gui.ClientToScreen(session.hwnd,(0,0))
                # 此处调用窗口截图（desktop_region=False），没有桌面区域的10px边距。
                dx,dy=origin[0]-window_left,origin[1]-window_top
                image=Image.open(BytesIO(capture_browser_window_by_handle(self.hwnd,expected_process_id=self.pid)))
                bounds=tuple(round(v) for v in (left+dx,top+dy,right+dx,bottom+dy))
                if min(bounds)<0 or bounds[2]>image.width or bounds[3]>image.height:
                    raise ViewerError('隔离画面截图范围超出窗口，未返回错误裁剪图')
                stream=BytesIO()
                image.crop(bounds).resize((target_width,target_height)).save(stream,format='PNG')
                return stream.getvalue()
            if operation in ('tap', 'longPress', 'swipe'):
                expected = 4 if operation == 'swipe' else 2
                if len(args) != expected or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in args):
                    raise ViewerError('查看器点击或滑动坐标不正确')
                width, height = self._tab.run_js('return [innerWidth,innerHeight]')
                for x, y in zip(args[::2], args[1::2]):
                    if not 0 <= x < width or not 0 <= y < height:
                        raise ViewerError('操作坐标已超出隔离查看器范围，请刷新画面')
                session = self._input()
                with session.mouse.logical_action():
                    session.mouse.move_to(*args[:2])
                    if operation == 'tap': session.mouse.click()
                    elif operation == 'longPress': session.mouse.press_for(.8)
                    else: session.mouse.drag(*args[2:])
                return True
            if operation == 'text':
                text = args[0]
                if not isinstance(text, str) or not 1 <= len(text) <= 2000:
                    raise ViewerError('输入内容长度必须在1至2000字符之间')
                self._input().keyboard.type_text(text, retries=0)
                return True
            if operation == 'enter':
                self._input().keyboard.press(13, char='\r')
                return True
            if operation == 'clearText':
                self._input().keyboard.select_all_delete()
                return True
            raise ViewerError('不支持的隔离查看器操作：' + str(operation))
