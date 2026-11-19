"""隔离 Desktop 启动桥的稳定数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
import re


_SAFE_DESKTOP_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_MAX_EXECUTABLES = 16
_MAX_MARKERS = 16


class DesktopBridgeError(RuntimeError):
    """启动桥安装、校验或运行失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True, slots=True)
class DesktopBridgePolicy:
    """某个厂商浏览器根进程的 Desktop 路由规则。"""

    provider: str
    desktop_name: str
    executable_names: tuple[str, ...]
    command_markers: tuple[str, ...]
    fail_closed: bool = True

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        desktop_name = str(self.desktop_name or "").strip()
        executable_names = tuple(dict.fromkeys(
            str(value or "").strip().lower()
            for value in self.executable_names
            if str(value or "").strip()
        ))
        command_markers = tuple(dict.fromkeys(
            str(value or "").strip()
            for value in self.command_markers
            if str(value or "").strip()
        ))
        if not provider:
            raise ValueError("provider 不能为空")
        if _SAFE_DESKTOP_NAME.fullmatch(desktop_name) is None:
            raise ValueError("Desktop 名称只能包含字母、数字、点、下划线和短横线")
        if not executable_names or len(executable_names) > _MAX_EXECUTABLES:
            raise ValueError("浏览器根进程名称必须为 1 到 16 个")
        if len(command_markers) > _MAX_MARKERS:
            raise ValueError("命令行标记不能超过 16 个")
        if any("\\" in name or "/" in name for name in executable_names):
            raise ValueError("进程规则只能使用文件名，不能携带路径")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "desktop_name", desktop_name)
        object.__setattr__(self, "executable_names", executable_names)
        object.__setattr__(self, "command_markers", command_markers)


@dataclass(frozen=True, slots=True)
class DesktopBridgeStatus:
    """原生 Hook 共享状态的脱敏快照。"""

    manager_pid: int
    desktop_name: str
    enabled: bool
    installed: bool
    modules_patched: int
    hooks_patched: int
    matched_processes: int
    successful_processes: int
    failed_processes: int
    last_child_pid: int
    last_error: int
    last_executable: str


__all__ = [
    "DesktopBridgeError",
    "DesktopBridgePolicy",
    "DesktopBridgeStatus",
]
