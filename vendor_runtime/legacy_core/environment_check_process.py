"""一次性四站采集子进程的父进程协议与生命周期守卫。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable
from urllib.request import ProxyHandler, build_opener

from vendor_runtime.legacy_core.environment_diagnostic_schema import SITE_SCHEMAS, sanitize_canonical_fields
from vendor_runtime.legacy_core.environment_schema import ENVIRONMENT_ID_RE


PROTOCOL_VERSION = 1
MAX_OUTPUT_BYTES = 256 * 1024


class DiagnosticProcessError(RuntimeError):
    pass


def browser_alive(debug_port: int, timeout: float = 1.5) -> bool:
    if not 1 <= int(debug_port) <= 65535:
        return False
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{int(debug_port)}/json/version", timeout=timeout) as response:
            data = json.loads(response.read(64 * 1024).decode("utf-8"))
        return bool(isinstance(data, dict) and data.get("webSocketDebuggerUrl"))
    except Exception:
        return False


def _worker_command() -> tuple[list[str], str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--diagnostic-worker"], os.path.dirname(sys.executable)
    root = os.environ['WINDOW_STUDIO_CODE']
    # pythonw 无控制台；一次性查询进程显式使用 python.exe，并隐藏窗口。
    executable=os.path.join(os.path.dirname(sys.executable),'python.exe')
    return [executable,'-X','utf8','-m','vendor_runtime.legacy_core.environment_dp_worker'], root


def _validate_response(response: Any, request: dict) -> dict:
    if not isinstance(response, dict) or response.get("protocol_version") != PROTOCOL_VERSION:
        raise DiagnosticProcessError("worker 返回协议无效")
    if response.get("request_id") != request["request_id"]:
        raise DiagnosticProcessError("worker request_id 不匹配")
    if response.get("environment_id") != request["environment_id"]:
        raise DiagnosticProcessError("worker environment_id 不匹配")
    if response.get("profile_id") != request["profile_id"]:
        raise DiagnosticProcessError("worker profile_id 不匹配")
    checks = response.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(SITE_SCHEMAS):
        raise DiagnosticProcessError("worker checks 站点集合无效")
    allowed_statuses = {"complete", "partial", "manual", "failed"}
    for site, item in checks.items():
        if not isinstance(item, dict) or set(item) != {"status", "fields", "url", "error", "page"}:
            raise DiagnosticProcessError(f"worker checks.{site} 结构无效")
        if item.get("status") not in allowed_statuses:
            raise DiagnosticProcessError(f"worker checks.{site}.status 无效")
        fields = item.get("fields")
        if not isinstance(fields, dict):
            raise DiagnosticProcessError(f"worker checks.{site}.fields 必须是对象")
        # 认不出来的字段丢掉就好，别让一个字段废掉整份采集（见 sanitize_canonical_fields）
        clean, dropped = sanitize_canonical_fields(site, fields)
        if dropped:
            note = f"已忽略本端不认识的字段: {', '.join(sorted(dropped)[:8])}"
            item["error"] = f"{item['error']}；{note}" if item.get("error") else note
        item["fields"] = clean
        if len(str(item.get("url", ""))) > 2000 or len(str(item.get("error", ""))) > 1000:
            raise DiagnosticProcessError(f"worker checks.{site} 文本过长")
        page = str(item.get("page", ""))
        # 只接受 worker 自己按站点命名的文件名，杜绝路径穿越
        if page and page != f"{site}.html":
            raise DiagnosticProcessError(f"worker checks.{site}.page 文件名无效")
    closed = response.get("closed_tabs", [])
    if not isinstance(closed, list) or any(str(site) not in SITE_SCHEMAS for site in closed):
        raise DiagnosticProcessError("worker closed_tabs 无效")
    return response


def collect_diagnostic_sites(*, environment_id: str, profile_id: str, debug_port: int,
                             urls: dict[str, str], reason: str = "initial",
                             timeout: float = 240.0, close_completed: bool = True,
                             page_dir: str = "", assets_dir: str = "",
                             cancel_event: threading.Event | None = None,
                             on_progress: Callable[[dict], None] | None = None) -> dict:
    """启动一次 worker 并等待其退出；坏端口在启动 worker 前就被拒绝。"""
    if not ENVIRONMENT_ID_RE.fullmatch(str(environment_id)):
        raise DiagnosticProcessError("environment_id 格式无效")
    if reason not in {"initial", "manual_retry"}:
        raise DiagnosticProcessError("reason 无效")
    debug_port = int(debug_port)
    if not browser_alive(debug_port):
        raise DiagnosticProcessError(f"浏览器调试端口不可用: {debug_port}")
    request = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "action": "collect_diagnostic_sites",
        "environment_id": environment_id,
        "profile_id": str(profile_id),
        "debug_address": f"127.0.0.1:{debug_port}",
        "reason": reason,
        "sites": list(SITE_SCHEMAS),
        "urls": {site: str(urls.get(site, ""))[:2000] for site in SITE_SCHEMAS},
        "deadline_ms": min(240_000, max(5_000, int((timeout - 3) * 1000))),
        "close_completed": bool(close_completed),
        "page_dir": str(page_dir or ""),
        "assets_dir": str(assets_dir or ""),
        "progress_dir": os.path.join(page_dir, ".progress") if on_progress and page_dir else "",
    }
    command, cwd = _worker_command()
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        creationflags=creationflags,
    )
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    deadline = time.monotonic() + max(5.0, min(float(timeout), 300.0))
    first_communicate = True
    consumed: set[str] = set()

    def consume_progress() -> None:
        if not on_progress or not request["progress_dir"]:
            return
        for site in SITE_SCHEMAS:
            if site in consumed:
                continue
            path = os.path.join(request["progress_dir"], site + ".json")
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                raw = handle.read(MAX_OUTPUT_BYTES + 1)
            if len(raw) > MAX_OUTPUT_BYTES:
                raise DiagnosticProcessError("worker 进度超过 256KB")
            progress = _validate_response(json.loads(raw.decode("utf-8")), request)
            if progress.get("completed_site") != site:
                raise DiagnosticProcessError("worker 进度站点不匹配")
            on_progress(progress)
            consumed.add(site)

    while True:
        try:
            consume_progress()
        except Exception:
            process.kill()
            process.communicate()
            raise
        if cancel_event is not None and cancel_event.is_set():
            process.kill()
            process.communicate()
            raise DiagnosticProcessError("一次性 DP 采集已取消，子进程已结束")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            process.communicate()
            raise DiagnosticProcessError("一次性 DP 采集超时，子进程已结束")
        try:
            stdout, stderr = process.communicate(
                payload if first_communicate else None,
                timeout=min(0.25, remaining),
            )
            consume_progress()
            break
        except subprocess.TimeoutExpired:
            # communicate() 会保留首次输入；后续短轮询只检查退出/取消。
            first_communicate = False
    if len(stdout) > MAX_OUTPUT_BYTES:
        raise DiagnosticProcessError("worker 输出超过 256KB")
    try:
        response = json.loads(stdout.decode("utf-8"))
    except Exception as exc:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise DiagnosticProcessError(f"worker 返回不是有效 JSON: {detail}") from exc
    if process.returncode != 0 or not response.get("ok"):
        raise DiagnosticProcessError(str(response.get("error") or "worker 执行失败")[:1000])
    response = _validate_response(response, request)
    response["browser_alive_after"] = browser_alive(debug_port)
    if not response["browser_alive_after"]:
        raise DiagnosticProcessError("DP 断开后浏览器调试端口已不可用")
    return response
