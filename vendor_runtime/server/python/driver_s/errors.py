"""浏览器驱动层的稳定异常。

驱动能力不匹配、驱动名称写错等配置问题不会因为重复执行而恢复，因此明确
标记为不可重试。动作执行器会保留原错误并立即停止当前动作。
"""

from __future__ import annotations

from collections.abc import Iterable

from .capability import DriverCapability


class DriverError(RuntimeError):
    """驱动模块统一异常基类。"""

    retryable: bool = True


class DriverSelectionError(DriverError):
    """驱动名称无效或驱动选择互相冲突。"""

    retryable = False


class DriverSelectorError(ValueError):
    """选择器不能在当前驱动中保持原语义。"""

    retryable = False


class DriverCapabilityError(DriverError):
    """当前驱动缺少动作所需能力。"""

    retryable = False

    def __init__(
        self,
        driver_type: str,
        feature_name: str,
        missing: Iterable[DriverCapability],
    ) -> None:
        self.driver_type = str(driver_type or "unknown")
        self.feature_name = str(feature_name or "当前功能")
        self.missing_capabilities = tuple(sorted(set(missing), key=lambda item: item.value))
        missing_text = "、".join(item.value for item in self.missing_capabilities)
        super().__init__(
            f"{self.feature_name}不支持当前浏览器驱动 {self.driver_type}；"
            f"缺少能力：{missing_text or 'unknown'}"
        )


__all__ = [
    "DriverCapabilityError",
    "DriverError",
    "DriverSelectorError",
    "DriverSelectionError",
]
