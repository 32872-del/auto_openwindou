"""浏览器公共实现：注册、选择与健康检查。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Mapping

from .browser_base import BrowserBase
from server.python.browser_s.browser_info import BrowserHealth


@dataclass(frozen=True, slots=True)
class BrowserRegistration:
    """一个浏览器及其启用状态、随机选择权重。"""

    browser: BrowserBase
    enabled: bool = False
    weight: int = 0

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError("浏览器权重不能为负数")


class BrowserCatalog:
    """一次运行持有一个目录，不共享类级可变状态。"""

    def __init__(
        self,
        registrations: Iterable[BrowserRegistration] = (),
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._browsers: dict[str, BrowserRegistration] = {}
        self._rng = rng or random.Random()
        for registration in registrations:
            self.register(registration)

    def register(self, registration: BrowserRegistration) -> None:
        key = registration.browser.key.strip().lower()
        if not key:
            raise ValueError("浏览器 key 不能为空")
        if key in self._browsers:
            raise ValueError(f"浏览器重复注册: {key}")
        self._browsers[key] = registration

    def get(self, key: str) -> BrowserBase:
        normalized = key.strip().lower()
        try:
            return self._browsers[normalized].browser
        except KeyError as exc:
            raise KeyError(f"未注册的浏览器: {key}") from exc

    def enabled(self) -> Mapping[str, BrowserBase]:
        return {
            key: item.browser
            for key, item in self._browsers.items()
            if item.enabled and item.weight > 0
        }

    def select(self, key: str) -> BrowserBase:
        if key.strip().lower() != "random":
            registration = self._browsers.get(key.strip().lower())
            if not registration:
                raise KeyError(f"未注册的浏览器: {key}")
            if not registration.enabled:
                raise ValueError(f"浏览器未启用: {key}")
            return registration.browser
        return self.select_weighted()

    def select_weighted(self) -> BrowserBase:
        pool = [
            registration
            for registration in self._browsers.values()
            if registration.enabled and registration.weight > 0
        ]
        if not pool:
            raise ValueError("没有已启用且权重大于 0 的浏览器")
        if len(pool) == 1:
            return pool[0].browser
        return self._rng.choices(
            [item.browser for item in pool],
            weights=[item.weight for item in pool],
            k=1,
        )[0]

    def health(self, *, enabled_only: bool = True) -> Mapping[str, BrowserHealth]:
        items = (
            (key, item)
            for key, item in self._browsers.items()
            if not enabled_only or item.enabled
        )
        return {
            key: item.browser.check_health()
            for key, item in items
        }


__all__ = ["BrowserCatalog", "BrowserRegistration"]
