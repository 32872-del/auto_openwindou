"""本地 Chrome 的启动就绪检查；只读取当前进程的回环调试接口。"""

from __future__ import annotations

import http.client
import json
import time
from urllib.parse import urlsplit

import psutil

from ..browser_module_s.browser_error import BrowserError
from .chrome_browser_default import CHROME_BROWSER_KEY


def _connection_error(code: str, message: str, **details) -> BrowserError:
    return BrowserError(
        provider=CHROME_BROWSER_KEY,
        operation="connect",
        code=code,
        message=message,
        retryable=True,
        details=details,
    )


def _require_process_alive(process_handle: int) -> None:
    if not process_handle:
        return
    import win32con
    import win32process

    try:
        exit_code = win32process.GetExitCodeProcess(process_handle)
    except Exception as exc:
        raise _connection_error(
            "chrome_process_state_unavailable", "无法核对本次 Chrome 启动进程状态"
        ) from exc
    if exit_code != win32con.STILL_ACTIVE:
        raise _connection_error(
            "chrome_process_exited",
            f"Chrome 启动期间提前退出（代码 {exit_code}）",
            exit_code=int(exit_code),
        )


def _owns_listener(process_id: int, port: int) -> bool:
    # 原生启动返回的根进程持有调试监听端口。不要因为另一个程序抢占了选出的
    # 空闲端口就连接它；也不扫描/连接用户已经打开的其它浏览器。
    if process_id <= 0:
        raise ValueError("Chrome 启动就绪检查需要本次进程 ID")
    try:
        connections = psutil.Process(process_id).connections(kind="tcp4")
    except psutil.NoSuchProcess as exc:
        raise _connection_error("chrome_process_exited", "本次 Chrome 启动进程已退出") from exc
    except (psutil.AccessDenied, OSError) as exc:
        raise _connection_error(
            "chrome_port_owner_unavailable", "无法核对 Chrome 调试端口归属"
        ) from exc
    return any(
        item.status == psutil.CONN_LISTEN
        and item.laddr and item.laddr.port == port
        for item in connections
    )


def _read_json(port: int, path: str, timeout: float):
    # 不读 HTTP_PROXY/HTTPS_PROXY，也不跟随服务端重定向；永远只访问回环。
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            return None
        payload = response.read(65_537)
        if len(payload) > 65_536:
            return None
        return json.loads(payload)
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        connection.close()


def _local_debug_target(value, port: int, target_type: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = urlsplit(value)
        prefix = f"/devtools/{target_type}/"
        return (
            address.scheme == "ws"
            and address.hostname in {"127.0.0.1", "localhost", "::1"}
            and address.port == port
            and address.username is None and address.password is None
            and not address.query and not address.fragment
            and address.path.startswith(prefix)
            and bool(address.path[len(prefix):])
            and "/" not in address.path[len(prefix):]
        )
    except ValueError:
        return False


def wait_debug_port(
    port: int, *, timeout_seconds: float, process_handle: int, process_id: int
) -> None:
    """端口属于本次 Chrome，浏览器目标和初始页面目标都就绪才返回。"""
    port = int(port)
    if not 0 < port <= 65_535:
        raise ValueError("Chrome 调试端口无效")
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    reason = "port_not_owned_or_not_listening"
    while time.monotonic() < deadline:
        _require_process_alive(process_handle)
        if _owns_listener(process_id, port):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            version = _read_json(port, "/json/version", min(0.5, remaining))
            if not (
                isinstance(version, dict)
                and isinstance(version.get("Browser"), str)
                and version["Browser"].startswith(("Chrome/", "HeadlessChrome/", "Chromium/"))
                and _local_debug_target(version.get("webSocketDebuggerUrl"), port, "browser")
            ):
                reason = "browser_endpoint_not_ready"
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                targets = _read_json(port, "/json/list", min(0.5, remaining))
                if isinstance(targets, list) and any(
                    isinstance(item, dict)
                    and item.get("type") in {"page", "webview"}
                    and _local_debug_target(item.get("webSocketDebuggerUrl"), port, "page")
                    for item in targets
                ):
                    _require_process_alive(process_handle)
                    if _owns_listener(process_id, port):
                        return
                    reason = "port_owner_changed"
                else:
                    reason = "page_target_not_ready"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
    raise _connection_error(
        "chrome_debug_not_ready",
        f"Chrome 调试连接未就绪（{reason}），已停止本次启动",
        reason=reason, port=port, timeout_seconds=float(timeout_seconds),
    )


__all__ = ["wait_debug_port"]
