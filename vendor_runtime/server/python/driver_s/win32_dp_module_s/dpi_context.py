"""进程 DPI 感知的唯一入口。"""

from __future__ import annotations

from dataclasses import dataclass

from . import win32_native as W


@dataclass(frozen=True, slots=True)
class DpiContext:
    """当前进程的 Win32 DPI 模式。"""

    is_windows: bool
    mode: str


def ensure_dpi_context() -> DpiContext:
    """在任何 HWND/坐标读取前幂等启用 Per-Monitor DPI 感知。"""

    mode = W.make_dpi_aware()
    return DpiContext(is_windows=bool(W.IS_WINDOWS), mode=str(mode or "none"))


__all__ = ["DpiContext", "ensure_dpi_context"]
