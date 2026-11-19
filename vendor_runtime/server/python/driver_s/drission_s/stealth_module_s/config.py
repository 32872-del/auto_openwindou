# -*- coding: utf-8 -*-
"""过检测层的运行配置。

全部开关默认开启；出问题时可以单独关掉某一项来定位，关掉后对应检测点会重新
暴露出来（每个字段的注释写了具体是哪一项）。
"""

from __future__ import annotations

from typing import Any, Mapping


class StealthConfig(object):
    """一份可复制、可逐项覆盖的运行配置。"""

    def __init__(self) -> None:
        # ── CDP 会话层 ──────────────────────────────────────────────
        #: 吞掉 ``Emulation.setFocusEmulationEnabled``。
        #: 关掉后 ``document.hasFocus()`` 恒 true、``visibilityState`` 恒
        #: visible（C1，单点判据里最强的一个）。
        self.disable_focus_emulation = True
        #: 把 ``Runtime.evaluate`` / ``callFunctionOn`` 的 ``userGesture``
        #: 收敛成 False。关掉后每次执行 JS 都会凭空注入一次真实用户激活（C2）。
        self.suppress_user_gesture = True
        #: DrissionPage 内部 JS 改到隔离世界执行。关掉后页面覆写
        #: ``scrollIntoViewIfNeeded`` / ``document.evaluate`` / ``JSON.stringify``
        #: 就能直接抓到驱动的调用（D1/D2/D6）。
        self.isolated_world = True

        # ── UI 层 ──────────────────────────────────────────────────
        #: 滚动到可见改用 CDP ``DOM.scrollIntoViewIfNeeded``（C++ 实现，不进
        #: 页面 JS 引擎）。
        self.cdp_scroll_into_view = True
        #: 截图不再下发 ``clip`` / ``captureBeyondViewport``（C9：视口会途经
        #: 1×1）。本项目的整窗与元素截图走 Win32，正常不会命中这条；它只保护
        #: 直接调用 DrissionPage 截图的代码路径。
        self.safe_screenshot = True
        #: 分屏拼接截图时每屏的稳定等待秒数。
        self.screenshot_settle = 0.12

        # ── 输入层 ─────────────────────────────────────────────────
        #: 真实输入注入总开关。关掉后回到 DrissionPage 的 CDP 合成事件，
        #: C3~C8 / D3 / D4 / D8 全部重新暴露。
        self.background_input = True
        #: 注入方式。两种模式的接口、轨迹、人格完全一致，只是底层不同：
        #:   ``background`` —— PostMessage/SendMessage。真实光标不动，也不抢占
        #:                     Windows 前台焦点；固定一窗口一 tab，不切换标签页。默认。
        #:   ``foreground`` —— SendInput 真实输入流。真实光标跟着走、窗口必须
        #:                     前台、同一时刻只能跑一个。
        self.input_mode = "background"
        #: 历史配置兼容字段。三协议互斥后固定为 False，运行时不会读取它来放行
        #: 输入兜底。CDP 仍可用于查元素、取坐标和普通页面命令。
        self.allow_background_cdp_fallback = False
        #: 前台模式专用：把浏览器窗口钉在最上层。窗口被盖住时 Chromium 会把
        #: 页面标成 hidden，此时它不把输入交给渲染进程，注入直接失效。
        self.keep_on_top = True
        #: 元素落点用二维高斯 + 会话人格偏移，替代固定的 ``padding.top + 3``（D8）。
        self.humanize_click_point = True
        #: 仅保留给单窗口兼容配置。Windows SetCapture 是全局资源，多窗口后台
        #: 协议固定禁用，否则多个 HWND 无法保持独立虚拟鼠标且会干扰用户光标。
        self.mouse_capture = True
        #: 元素聚焦改成真实点击，替代 ``DOM.focus`` 幽灵聚焦（C8）。
        self.click_to_focus = True

        # ── 行为层 ─────────────────────────────────────────────────
        #: 空闲时的手抖与无意识漂移。
        self.idle_motion = True
        self.idle_after = 0.9
        self.idle_tremor_every = (0.6, 2.4)
        self.idle_tremor_px = 3.0
        self.idle_drift_every = (6.0, 22.0)
        self.idle_drift_prob = 0.55
        self.idle_drift_px = 110.0
        #: 滚动改用真实滚轮，页面能收到 ``wheel``（C6 与「只有 scroll 没有
        #: wheel」这条行为特征）。
        self.wheel_scroll = True

        # ── 会话管理 ───────────────────────────────────────────────
        #: 绑定失败后多久才允许再试一次（秒）。没有这个退避的话，绑不上的页面
        #: 每一次 JS 调用都会重跑一轮窗口标定，代价极高。
        self.bind_retry_interval = 5.0
        #: 每轮绑定的重试次数。
        self.bind_attempts = 3

        # ── 其它 ───────────────────────────────────────────────────
        #: 打开后在标准输出打印绑定与退化原因，只用于本机排查。
        self.debug = False

    # ------------------------------------------------------------------ #
    def copy(self) -> "StealthConfig":
        clone = StealthConfig()
        clone.__dict__.update(self.__dict__)
        return clone

    def update(self, values: Mapping[str, Any]) -> "StealthConfig":
        """逐项覆盖；未知字段直接报错，避免拼错开关却毫无反应。"""

        for key, value in dict(values).items():
            if not hasattr(self, key):
                raise AttributeError(f"过检测层没有 {key} 这个选项")
            setattr(self, key, value)
        return self

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def __repr__(self) -> str:
        body = ", ".join(
            f"{key}={value!r}" for key, value in sorted(self.__dict__.items())
        )
        return f"StealthConfig({body})"


__all__ = ["StealthConfig"]
