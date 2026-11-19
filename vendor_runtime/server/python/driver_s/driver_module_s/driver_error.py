"""双驱动边界的稳定异常。"""

from __future__ import annotations

from ..errors import DriverError


class DriverBoundaryError(DriverError):
    """调用越过了 dp / win32_dp 的公开能力边界。"""

    retryable = False


class DPQueryError(DriverError):
    """DP 查询、几何或页面读取失败。"""


class DPSpecialOperationError(DriverError):
    """用户或动作契约明确允许的 DP 特殊协议操作失败。"""


class Win32InputError(DriverError):
    """Win32 鼠标、键盘或滚轮执行失败。"""


class Win32InputUnavailableError(Win32InputError):
    """当前页面无法建立严格 Win32 输入会话；不得自动改走 DP/CDP。"""


__all__ = [
    "DPQueryError",
    "DPSpecialOperationError",
    "DriverBoundaryError",
    "Win32InputError",
    "Win32InputUnavailableError",
]
