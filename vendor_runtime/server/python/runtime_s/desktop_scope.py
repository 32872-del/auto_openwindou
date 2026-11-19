"""隔离桌面只归创建它的执行端实例所有。"""

from __future__ import annotations

import hashlib
import os

_scope = ""


def configure_desktop_scope(instance_id: str) -> str:
    """必须在取得该实例的互斥锁后设置，启动清扫与创建桌面共用。"""
    global _scope
    _scope = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:20]
    return desktop_prefix()


def desktop_prefix() -> str:
    # 非正式入口未持锁时仅使用本进程空间，不能认领旧实例的桌面。
    scope = _scope or f"process{os.getpid()}"
    return f"AutoDrission_{scope}_"
