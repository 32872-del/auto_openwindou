"""手机环境的 Win32 指针：落点直达，按住时直线滑动。"""

import math
import time


def travel_mobile(mouse, x, y, modifiers=0, *, drag=False):
    """返回是否已处理；不改变 Windows 鼠标消息的输入类型。"""
    if not getattr(mouse, "instant", False):
        return False
    if mouse._x is None or not (drag or mouse.pressed):
        mouse._goto(x, y, modifiers)
        return True
    start_x, start_y = mouse._x, mouse._y
    distance = math.hypot(x - start_x, y - start_y)
    if distance == 0:
        return True
    # 手指滑动需要连续按住移动，不能将拖动也缩成一个终点事件。
    duration = min(0.6, max(0.12, distance / 1800.0))
    steps = max(2, math.ceil(duration / 0.016))
    started = time.monotonic()
    for index in range(1, steps + 1):
        remaining = started + duration * index / steps - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        mouse._goto(start_x + (x - start_x) * index / steps,
                    start_y + (y - start_y) * index / steps, modifiers)
    return True
