"""如意浏览器进程所有权；按指定桌面启动并核对调试端口归属。"""
from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path


class DesktopProcess:
    def __init__(self, handle: int, pid: int) -> None:
        self.handle, self.pid = handle, pid

    def close(self, *, timeout: float = 5, terminate: bool = False) -> None:
        import win32api
        import win32event
        import win32process

        if not self.handle:
            return
        if win32event.WaitForSingleObject(self.handle, int(timeout * 1000)) != 0:
            if not terminate:
                raise RuntimeError(f"如意浏览器进程 {self.pid} 尚未退出，保留进程句柄")
            win32process.TerminateProcess(self.handle, 1)
            if win32event.WaitForSingleObject(self.handle, 5000) != 0:
                raise RuntimeError(f"如意浏览器进程 {self.pid} 关闭未完成")
        win32api.CloseHandle(self.handle)
        self.handle = 0


def launch_on_desktop(options, desktop_name: str, timeout: float) -> DesktopProcess:
    import win32api
    import win32con
    import win32process
    import psutil

    if not desktop_name or "\\" in desktop_name or "/" in desktop_name:
        raise ValueError("如意启动桌面名称无效")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    options.set_address(f"127.0.0.1:{port}")
    options.prepare_runtime_files()
    options.write_prefs_to_profile()
    command = options.build_command()
    executable = Path(command[0]).resolve(strict=True)
    startup = win32process.STARTUPINFO()
    startup.lpDesktop = f"winsta0\\{desktop_name}"
    process, thread, pid, _ = win32process.CreateProcess(
        str(executable), subprocess.list2cmdline(command), None, None, False,
        win32con.CREATE_NEW_PROCESS_GROUP | win32con.CREATE_UNICODE_ENVIRONMENT,
        None, str(executable.parent), startup,
    )
    owned = DesktopProcess(process.Detach(), int(pid))
    win32api.CloseHandle(thread)
    try:
        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            if not psutil.pid_exists(pid):
                raise RuntimeError(f"如意浏览器在连接前退出：PID {pid}")
            listeners = [c for c in psutil.net_connections(kind="tcp")
                         if c.status == psutil.CONN_LISTEN and c.laddr.port == port]
            if listeners:
                family = {pid, *(p.pid for p in psutil.Process(pid).children(recursive=True))}
                if not all(c.pid in family for c in listeners):
                    raise RuntimeError(f"如意端口 {port} 已被其他进程占用，拒绝连接")
                options.existing_only(True)
                return owned
            time.sleep(.1)
        raise TimeoutError(f"如意隔离进程 {pid} 在 {timeout:g} 秒内没有开放连接端口")
    except BaseException:
        owned.close(timeout=0, terminate=True)
        raise
