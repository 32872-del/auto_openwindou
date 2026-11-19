"""有界的前台输入租约。

前台 SendInput 属于系统级共享资源。本模块只在单窗口协议或明确的恢复路径中
短暂持有它，并在动作结束后把控制权还给动作开始前的用户窗口。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass

from ..driver_module_s.driver_error import Win32InputUnavailableError
from . import win32_native as W


_LEASE_LOCK = threading.Lock()
_LOCAL = threading.local()


@dataclass(frozen=True, slots=True)
class ForegroundLease:
    target_hwnd: int
    previous_hwnd: int
    owner: str


def owns_foreground_lease(hwnd: int) -> bool:
    lease = getattr(_LOCAL, "lease", None)
    return bool(lease and W.root_of(lease.target_hwnd) == W.root_of(hwnd))


@contextmanager
def foreground_lease(
    hwnd: int,
    *,
    owner: str,
    timeout: float = 4.0,
    keep_on_top: bool = True,
):
    """独占、限时地取得目标窗口前台；退出时按条件恢复原窗口。"""

    target = int(W.root_of(hwnd) or hwnd or 0)
    if not target or not W.is_alive(target):
        raise Win32InputUnavailableError("前台恢复目标窗口已经失效")
    nested = getattr(_LOCAL, "lease", None)
    if nested is not None:
        if W.root_of(nested.target_hwnd) != target:
            raise Win32InputUnavailableError("同一线程不能同时租用两个前台窗口")
        yield nested
        return
    if not _LEASE_LOCK.acquire(timeout=max(0.1, min(float(timeout), 8.0))):
        raise Win32InputUnavailableError("前台输入租约等待超时")

    from .foreground_input import force_foreground, is_topmost, set_topmost

    previous = int(W.foreground_window() or 0)
    was_topmost = bool(is_topmost(target))
    lease = ForegroundLease(target, previous, str(owner or "win32-input"))
    _LOCAL.lease = lease
    try:
        if not force_foreground(target, timeout=min(max(timeout, 0.2), 1.5), topmost=keep_on_top):
            raise Win32InputUnavailableError("目标浏览器无法取得短暂前台控制权")
        yield lease
    finally:
        _LOCAL.lease = None
        try:
            if keep_on_top and not was_topmost and W.is_alive(target):
                set_topmost(target, False)
            # 用户若已主动切到第三个窗口，不进行反向抢焦点。
            if (
                previous
                and previous != target
                and W.is_alive(previous)
                and W.root_of(W.foreground_window()) == target
            ):
                force_foreground(previous, timeout=0.5, topmost=False)
        finally:
            _LEASE_LOCK.release()


__all__ = ["ForegroundLease", "foreground_lease", "owns_foreground_lease"]
