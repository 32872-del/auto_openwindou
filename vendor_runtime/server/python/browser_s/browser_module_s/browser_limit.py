"""浏览器公共实现：额度和当前用量的统一返回值。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class LimitSource(StrEnum):
    """额度数据来自哪里，防止把本地估算冒充厂商真实额度。"""

    OFFICIAL_API = "official_api"
    OFFICIAL_DOCS = "official_docs"
    LOCAL_ESTIMATE = "local_estimate"
    UNAVAILABLE = "unavailable"

    @property
    def label(self) -> str:
        labels = {
            self.OFFICIAL_API: "官方 API",
            self.OFFICIAL_DOCS: "官方文档",
            self.LOCAL_ESTIMATE: "本地估算",
            self.UNAVAILABLE: "不可获取",
        }
        return labels[self]


@dataclass(frozen=True, slots=True)
class BrowserLimits:
    """一个浏览器账号或本地客户端能够使用的最大额度。"""

    max_total: int | None = None
    max_create_per_hour: int | None = None
    max_create_per_day: int | None = None
    max_open: int | None = None
    max_recreate: int | None = None
    max_requests_per_minute: int | None = None
    max_requests_per_hour: int | None = None
    create_request_cost: int | None = None
    start_request_cost: int | None = None
    stop_request_cost: int | None = None
    one_time_request_cost: int | None = None
    source: LimitSource = LimitSource.UNAVAILABLE
    note: str = ""

    def __post_init__(self) -> None:
        values = (
            self.max_total,
            self.max_create_per_hour,
            self.max_create_per_day,
            self.max_open,
            self.max_recreate,
            self.max_requests_per_minute,
            self.max_requests_per_hour,
            self.create_request_cost,
            self.start_request_cost,
            self.stop_request_cost,
            self.one_time_request_cost,
        )
        if any(value is not None and value < 0 for value in values):
            raise ValueError("浏览器额度不能小于 0")

    @classmethod
    def unavailable(cls, note: str = "") -> "BrowserLimits":
        return cls(source=LimitSource.UNAVAILABLE, note=note)


@dataclass(frozen=True, slots=True)
class BrowserUsage:
    """浏览器当前已使用的数量和剩余额度。"""

    total: int | None = None
    created_last_hour: int | None = None
    created_last_day: int | None = None
    open_now: int | None = None
    remaining_create: int | None = None
    remaining_requests_minute: int | None = None
    remaining_requests_hour: int | None = None
    reset_at: datetime | None = None
    source: LimitSource = LimitSource.UNAVAILABLE
    note: str = ""

    def __post_init__(self) -> None:
        values = (
            self.total,
            self.created_last_hour,
            self.created_last_day,
            self.open_now,
            self.remaining_create,
            self.remaining_requests_minute,
            self.remaining_requests_hour,
        )
        if any(value is not None and value < 0 for value in values):
            raise ValueError("浏览器用量不能小于 0")

    @classmethod
    def unavailable(cls, note: str = "") -> "BrowserUsage":
        return cls(source=LimitSource.UNAVAILABLE, note=note)


__all__ = ["BrowserLimits", "BrowserUsage", "LimitSource"]
