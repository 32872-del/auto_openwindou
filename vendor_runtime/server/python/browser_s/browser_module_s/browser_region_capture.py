"""远程画面：按浏览器外框截取同一 Desktop 的画面，包含覆盖其上的弹窗。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from concurrent.futures import Future, TimeoutError
import threading


_pending: dict[int, Future] = {}
_pending_lock = threading.Lock()


def _api():
    api = ctypes.WinDLL('user32', use_last_error=True)
    signatures = {
        'GetWindowThreadProcessId': ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
        'GetThreadDesktop': ([wt.DWORD], wt.HANDLE),
        'GetUserObjectInformationW': ([wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)], wt.BOOL),
        'OpenDesktopW': ([wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
        'OpenInputDesktop': ([wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
        'CloseDesktop': ([wt.HANDLE], wt.BOOL),
        'SetThreadDesktop': ([wt.HANDLE], wt.BOOL),
        'GetTopWindow': ([wt.HWND], wt.HWND),
        'GetWindow': ([wt.HWND, wt.UINT], wt.HWND),
        'GetWindowRect': ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
        'IsWindowVisible': ([wt.HWND], wt.BOOL),
        'IsIconic': ([wt.HWND], wt.BOOL),
        'IsWindow': ([wt.HWND], wt.BOOL),
        'GetLayeredWindowAttributes': ([wt.HWND, ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.BYTE), ctypes.POINTER(wt.DWORD)], wt.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(api, name)
        fn.argtypes, fn.restype = args, result
    return api


def _name(api, handle):
    buffer = ctypes.create_unicode_buffer(512)
    needed = wt.DWORD()
    if not handle or not api.GetUserObjectInformationW(handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
        raise OSError('无法确认浏览器所在 Desktop，已取消截图')
    return buffer.value


def _window_identity(api, hwnd, expected_pid):
    pid = wt.DWORD()
    tid = api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not tid or (expected_pid is not None and pid.value != int(expected_pid)):
        raise LookupError('浏览器窗口已关闭或归属发生变化')
    return _name(api, api.GetThreadDesktop(tid)), pid.value


def _input_name(api):
    desktop = api.OpenInputDesktop(0, False, 0x0001)
    if not desktop:
        return None
    try:
        return _name(api, desktop)
    finally:
        api.CloseDesktop(desktop)


def _rect(api, hwnd):
    rect = wt.RECT()
    if not api.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise LookupError('截图时窗口已关闭')
    return (rect.left, rect.top, rect.right, rect.bottom)


def _overlap(first, second):
    return min(first[2], second[2]) > max(first[0], second[0]) and min(first[3], second[3]) > max(first[1], second[1])


def _layer_mask(api, hwnd, image):
    from PIL import Image, ImageChops

    key, alpha, flags = wt.DWORD(), wt.BYTE(), wt.DWORD()
    if not api.GetLayeredWindowAttributes(hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)):
        return None
    opacity = alpha.value if flags.value & 2 else 255
    if not flags.value & 1:
        return Image.new('L', image.size, opacity) if opacity != 255 else None
    color = (key.value & 255, (key.value >> 8) & 255, (key.value >> 16) & 255)
    background = Image.new('RGB', image.size, color)
    delta = ImageChops.difference(image, background)
    channels = delta.split()
    maximum = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    mask = maximum.point(lambda value: opacity if value else 0)
    background.close()
    delta.close()
    for channel in channels:
        channel.close()
    maximum.close()
    return mask


def _capture_bound(api, hwnd, desktop_name, pid, capture_window, edge_pad):
    if _input_name(api) == desktop_name:
        image = capture_window(hwnd, visible_surface=True, expected_process_id=pid)
        if _input_name(api) != desktop_name:
            image.close()
            raise RuntimeError('截图期间桌面已切换，请等待下一帧')
        return image

    # 未激活 Desktop 没有可用的可见屏幕合成表面，不能拿主桌面屏幕 DC。
    # 用该 Desktop 上的窗口表面，按实际坐标和 Z 顺序合成浏览器外框范围。
    bounds = _rect(api, hwnd)
    above = []
    current = int(api.GetTopWindow(0) or 0)
    seen = set()
    while current and current != hwnd and current not in seen:
        seen.add(current)
        if api.IsWindowVisible(current) and not api.IsIconic(current):
            try:
                candidate = _rect(api, current)
                if _overlap(bounds, candidate):
                    above.append((current, candidate))
            except LookupError:
                pass
        current = int(api.GetWindow(current, 2) or 0)  # GW_HWNDNEXT
    if current != hwnd:
        raise LookupError('浏览器不在已绑定的 Desktop 中，已取消截图')

    image = capture_window(hwnd, expected_process_id=pid)
    try:
        for popup, rect in reversed(above):
            if not api.IsWindow(popup) or not api.IsWindowVisible(popup):
                continue
            popup_desktop, popup_pid = _window_identity(api, popup, None)
            if popup_desktop != desktop_name:
                raise LookupError('弹窗所属 Desktop 已变化，已取消截图')
            rect = _rect(api, popup)
            popup_image = capture_window(popup, expected_process_id=popup_pid)
            try:
                if _rect(api, popup) != rect:
                    raise RuntimeError('截图时弹窗正在移动，请等待下一帧')
                # 单窗口截图有外围留边；叠加时只使用真实窗口区域。
                cropped = popup_image.crop((edge_pad, edge_pad, popup_image.width - edge_pad, popup_image.height - edge_pad))
                mask = _layer_mask(api, popup, cropped)
                try:
                    image.paste(cropped, (rect[0] - bounds[0] + edge_pad, rect[1] - bounds[1] + edge_pad), mask)
                finally:
                    cropped.close()
                    if mask is not None:
                        mask.close()
            finally:
                popup_image.close()
        if _window_identity(api, hwnd, pid)[0] != desktop_name:
            raise LookupError('浏览器所属 Desktop 已变化，已丢弃截图')
        if _rect(api, hwnd) != bounds:
            raise RuntimeError('截图时浏览器正在移动或调整大小，请等待下一帧')
        return image
    except BaseException:
        image.close()
        raise


def capture_desktop_region(hwnd, *, expected_process_id, capture_window, edge_pad):
    # API/异步线程不能改变自己的 Desktop。用无窗口的短生命周期线程进入
    # 目标 Desktop；不调用 SwitchDesktop、不移动鼠标、不改变前台窗口。
    with _pending_lock:
        if hwnd in _pending:
            raise RuntimeError('上一帧仍在读取，暂时无法取得新的浏览器画面')
        future = Future()
        _pending[hwnd] = future

    def run():
        api = None
        desktop = None
        previous = None
        image = None
        failure = None
        try:
            api = _api()
            previous = api.GetThreadDesktop(threading.get_native_id())
            desktop_name, pid = _window_identity(api, hwnd, expected_process_id)
            desktop = api.OpenDesktopW(desktop_name, 0, False, 0x00C1)
            if not desktop or not api.SetThreadDesktop(desktop):
                raise OSError('无法进入浏览器所在 Desktop 读取画面')
            if _name(api, api.GetThreadDesktop(threading.get_native_id())) != desktop_name:
                raise RuntimeError('截图线程 Desktop 不一致，已取消截图')
            image = _capture_bound(api, hwnd, desktop_name, pid, capture_window, edge_pad)
        except BaseException as error:
            failure = error
        finally:
            if api is not None and previous:
                api.SetThreadDesktop(previous)
            if api is not None and desktop:
                api.CloseDesktop(desktop)
            with _pending_lock:
                _pending.pop(hwnd, None)
        if future.set_running_or_notify_cancel():
            if failure is not None:
                future.set_exception(failure)
            else:
                future.set_result(image)
        elif image is not None:
            image.close()

    try:
        threading.Thread(target=run, name='browser-region-capture', daemon=True).start()
    except BaseException:
        with _pending_lock:
            _pending.pop(hwnd, None)
        raise
    try:
        return future.result(timeout=8)
    except TimeoutError as exc:
        future.cancel()
        raise RuntimeError('读取浏览器区域画面超时，请检查窗口是否有响应') from exc
