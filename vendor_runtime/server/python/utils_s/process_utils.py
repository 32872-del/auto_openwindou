"""端口、进程树与就绪等待能力。"""

import socket
from time import sleep
from typing import Callable


def wait_for_tcp_port(
    port: int,
    *,
    max_wait: int = 40,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    for _ in range(max_wait):
        if should_stop is not None and should_stop():
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(1)
                connection.connect(("127.0.0.1", port))
                return True
        except (ConnectionRefusedError, OSError, socket.timeout):
            pass
        sleep(1)
    return False


def find_pid_by_port(port: int) -> tuple[int | None, str | None]:
    import psutil

    for process in psutil.process_iter(["pid", "name"]):
        try:
            for connection in process.connections():
                if connection.status == "LISTEN" and connection.laddr.port == port:
                    return process.info["pid"], process.info["name"]
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    return None, None


def kill_process_tree_by_port(port: int) -> list[int]:
    """按监听端口回收进程树，返回已发出终止指令的 PID。"""

    killed_pids: list[int] = []
    if not port:
        return killed_pids
    try:
        import psutil
    except Exception:
        return killed_pids

    try:
        targets = []
        for process in psutil.process_iter(["pid", "name"]):
            try:
                for connection in process.connections(kind="inet"):
                    if connection.status == "LISTEN" and connection.laddr.port == port:
                        targets.append(process)
                        break
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                pass

        for process in targets:
            try:
                pid = process.pid
                children = process.children(recursive=True)
                process.kill()
                killed_pids.append(pid)
                for child in children:
                    try:
                        child.kill()
                        killed_pids.append(child.pid)
                    except Exception:
                        pass
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
    except Exception:
        pass
    return killed_pids


# 打包成品里每调用一次外部程序，Windows 就会给它开一个控制台窗口——使用者
# 看到的就是"一会儿弹几个小黑框"。浏览器程序探测里那两处 PowerShell 每 60
# 秒健康检查都会跑，所以黑框是周期性成片出现的。
#
# 这个标志只对**控制台程序**生效；资源管理器、浏览器管理器这类图形程序加上
# 它是空操作，不会把本该看得见的窗口藏掉。所以可以无差别地加在所有调用上。
CREATE_NO_WINDOW = 0x08000000


def no_window_kwargs() -> dict:
    """给 subprocess 调用补上"别弹控制台窗口"。

    非 Windows 上返回空字典，保持跨平台可导入。
    """

    import os

    if os.name != "nt":
        return {}
    return {"creationflags": CREATE_NO_WINDOW}
