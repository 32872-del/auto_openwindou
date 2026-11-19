"""指纹环境 outbox 后台同步器。"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests

from vendor_runtime.legacy_core.environment_page_archive import ASSET_NAME_RE
from vendor_runtime.legacy_core.environment_identity import is_client_number
from vendor_runtime.legacy_core.environment_store import EnvironmentStore, assets_dir, pages_root


log = logging.getLogger("EnvironmentSync")


def _locate(path: str) -> tuple[str, str, str] | None:
    """从存档文件路径反推 ``(<别名-编号>, <启动编号>, <文件名>)``；越界返回 None。"""
    try:
        relative = os.path.relpath(os.path.abspath(path), pages_root())
    except ValueError:
        return None
    parts = relative.replace("\\", "/").split("/")
    if len(parts) != 3 or parts[0] in ("..", "_assets"):
        return None
    return parts[0], parts[1], parts[2]


def client_number_taken(config: dict, number: str) -> bool:
    """问服务端这个客户端编号有没有被别人用掉；连不上就当没占用（离线先用着）。"""
    if not is_client_number(str(number or "").upper()):
        return False
    server_url = str(config.get("server_url", "") or "")
    if not server_url:
        raise RuntimeError("未配置服务器地址")
    endpoint = urljoin(server_url.rstrip("/") + "/", "api/fingerprint/client-number")
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(endpoint, params={"number": str(number).upper(),
                                                 "client_id": str(config.get("client_id", ""))},
                               timeout=float(config.get("timeout", 5) or 5))
    response.raise_for_status()
    return bool(response.json().get("taken"))


class EnvironmentSync:
    def __init__(self, store: EnvironmentStore, config: dict | None = None):
        self.store = store
        self._config = dict(config or {})
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._assets_requested = True

    def configure(self, config: dict | None) -> None:
        self._config = dict(config or {})
        # 换了服务器地址或服务端刚升级时，把之前判死的补传任务复活。
        self._revive_failed()
        self.trigger()

    def _revive_failed(self) -> None:
        try:
            revived = self.store.revive_failed()
        except Exception as exc:
            log.warning("复活失败的补传任务时出错: %s", exc)
            return
        if revived:
            log.info("已复活 %d 条此前判死的指纹环境补传任务", revived)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._revive_failed()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="environment-outbox", daemon=True)
        self._thread.start()
        self.trigger()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def trigger(self) -> None:
        self._assets_requested = True
        self._wake.set()

    def _enabled(self) -> bool:
        return bool(self._config.get("enabled", True) and self._config.get("server_url"))

    def _run(self) -> None:
        # 事件、页面各四个并发名额，CSS 独立一个；完成一个便补一个，不等整批结束。
        pool = ThreadPoolExecutor(max_workers=9, thread_name_prefix="environment-upload")
        running = {}
        last_assets = 0.0

        def submit(key, callback, *args):
            future = pool.submit(callback, *args)
            running[key] = future
            future.add_done_callback(lambda _: self._wake.set())

        try:
            while not self._stop.is_set():
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                for key, future in list(running.items()):
                    if future.done():
                        del running[key]
                        try:
                            future.result()
                        except Exception as exc:
                            log.warning("独立补传任务失败 %s: %s", key[0], exc)
                if not self._enabled() or self._stop.is_set():
                    continue
                try:
                    for event in self.store.due_events(20, one_per_environment=True):
                        key = ("event", event["environment_id"])
                        if key in running:
                            continue
                        if sum(k[0] == "event" for k in running) >= 4:
                            break
                        submit(key, self._send_event, event)
                    for row in self.store.due_pages(20, one_per_path=True):
                        key = ("page", os.path.normcase(os.path.abspath(row["path"])))
                        if key in running:
                            continue
                        if sum(k[0] == "page" for k in running) >= 4:
                            break
                        submit(key, self._send_pages, [row])
                    if (("assets", "") not in running and
                            (self._assets_requested or time.monotonic() - last_assets >= 15.0)):
                        self._assets_requested = False
                        last_assets = time.monotonic()
                        submit(("assets", ""), self.flush_assets)
                except Exception as exc:
                    log.warning("指纹环境补传排队失败: %s", exc)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def flush_once(self) -> dict:
        if not self._enabled():
            return {"sent": 0, "disabled": True}
        events = self.store.due_events(20)
        if not events:
            return {"sent": 0}
        groups = {}
        for event in events:
            groups.setdefault(event["environment_id"], []).append(event)

        def send_group(group):
            return sum(self._send_event(event)["sent"] for event in group)

        with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
            sent = sum(pool.map(send_group, groups.values()))
        return {"sent": sent, "total": len(events)}

    def _send_event(self, event: dict) -> dict:
        # 每个 HTTP 请求只有一条记录；某条拒收或超时不会让其他环境整批重试。
        events = [event]
        timeout = max(1.0, float(self._config.get("timeout", 5) or 5))
        endpoint = urljoin(str(self._config["server_url"]).rstrip("/") + "/", "api/fingerprint/events")
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(
                    endpoint, json={"events": events}, timeout=timeout,
                )
        except Exception as exc:
            for event in events:
                self._retry(event["event_id"], str(exc))
            return {"sent": 0, "error": str(exc)}
        if response.status_code >= 500 or response.status_code in (408, 429):
            error = f"HTTP {response.status_code}"
            for event in events:
                self._retry(event["event_id"], error)
            return {"sent": 0, "error": error}
        if response.status_code >= 400:
            error = f"HTTP {response.status_code}: {response.text[:300]}"
            # 413 是这条事件本身太大，重发无意义；其余 4xx（含服务端版本落后不认识
            # 新字段的 400）都要继续退避重试，等服务端升级后自愈，绝不丢数据。
            permanent = response.status_code == 413
            for event in events:
                if permanent:
                    self.store.mark_retry(event["event_id"], error, permanent=True)
                else:
                    self._retry(event["event_id"], error)
            return {"sent": 0, "error": error}
        try:
            results = response.json().get("results", [])
        except Exception:
            results = []
        by_id = {str(item.get("event_id", "")): item for item in results if isinstance(item, dict)}
        sent = []
        for event in events:
            item = by_id.get(event["event_id"], {})
            if item.get("accepted") or item.get("duplicate"):
                sent.append(event["event_id"])
            elif item.get("rejected"):
                # 结构性拒绝同样可能是服务端版本落后，退避重试而不是直接判死。
                self._retry(event["event_id"], str(item.get("error", "rejected")))
            else:
                self._retry(event["event_id"], "服务器响应缺少该 event_id")
        self.store.mark_sent(sent)
        return {"sent": len(sent), "total": len(events)}

    def flush_pages(self, limit: int = 8) -> dict:
        """把检测页 HTML 逐份传上去；比事件大，所以走独立通道和独立重试。"""
        if not self._enabled():
            return {"sent": 0, "disabled": True}
        pending = self.store.due_pages(limit)
        if not pending:
            return {"sent": 0}
        groups = {}
        for row in pending:
            groups.setdefault(os.path.normcase(os.path.abspath(row["path"])), []).append(row)
        with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
            sent = sum(result["sent"] for result in pool.map(self._send_pages, groups.values()))
        return {"sent": sent, "total": len(pending)}

    def _send_pages(self, pending: list[dict]) -> dict:
        timeout = max(5.0, float(self._config.get("timeout", 5) or 5) * 3)
        base = str(self._config["server_url"]).rstrip("/") + "/"
        endpoint = urljoin(base, "api/fingerprint/page")
        sent = 0
        with requests.Session() as session:
            session.trust_env = False
            for row in pending:
                path = str(row.get("path", ""))
                event_id, site = row["event_id"], row["site"]
                if not os.path.exists(path):
                    # 本地文件没了就别一直重试，标记掉即可
                    self.store.mark_page(event_id, site, "failed", "本地页面存档不存在")
                    continue
                located = _locate(path)
                if not located:
                    self.store.mark_page(event_id, site, "failed", "存档路径不在存档根目录下")
                    continue
                folder, launch_id, name = located
                content_type = {
                    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    '.json': 'application/json; charset=utf-8',
                }.get(os.path.splitext(name)[1].lower(), 'text/html; charset=utf-8')
                try:
                    with open(path, "rb") as handle:
                        body = handle.read()
                    # 固定本次上传的字节后立即释放文件；慢请求不能锁住下次 data.json 更新。
                    response = session.post(
                        endpoint, params={"folder": folder, "launch_id": launch_id,
                                          "name": name, "event_id": event_id, "site": site},
                        data=body, timeout=timeout,
                        headers={"Content-Type": content_type},
                    )
                except Exception as exc:
                    self._retry_page(event_id, site, str(exc))
                    continue
                if response.status_code == 413:
                    self.store.mark_page(event_id, site, "failed", "页面过大，服务端拒收")
                elif response.status_code >= 400:
                    self._retry_page(event_id, site, f"HTTP {response.status_code}: {response.text[:200]}")
                else:
                    self.store.mark_page(event_id, site, "sent")
                    sent += 1
        return {"sent": sent, "total": len(pending)}

    def flush_assets(self, limit: int = 40) -> dict:
        """共享 CSS 只传服务端还没有的那几份：先问一次缺哪些，再逐个上传。"""
        if not self._enabled():
            return {"sent": 0, "disabled": True}
        folder = assets_dir()
        if not os.path.isdir(folder):
            return {"sent": 0}
        names = sorted(name for name in os.listdir(folder) if ASSET_NAME_RE.fullmatch(name))
        if not names:
            return {"sent": 0}
        timeout = max(5.0, float(self._config.get("timeout", 5) or 5) * 3)
        base = str(self._config["server_url"]).rstrip("/") + "/"
        sent = 0
        with requests.Session() as session:
            session.trust_env = False
            try:
                response = session.post(urljoin(base, "api/fingerprint/assets/missing"),
                                        json={"names": names}, timeout=timeout)
                missing = list(response.json().get("missing", [])) if response.ok else []
            except Exception as exc:
                log.warning("询问服务端缺哪些 CSS 失败: %s", exc)
                return {"sent": 0, "error": str(exc)[:200]}
            for name in missing[:max(1, int(limit))]:
                if not ASSET_NAME_RE.fullmatch(str(name)):
                    continue
                path = os.path.join(folder, str(name))
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, "rb") as handle:
                        body = handle.read()
                    result = session.post(urljoin(base, "api/fingerprint/asset"),
                                          params={"name": name}, data=body, timeout=timeout,
                                          headers={"Content-Type": "text/css; charset=utf-8"})
                    if result.ok:
                        sent += 1
                except Exception as exc:
                    log.warning("上传共享 CSS %s 失败: %s", name, exc)
        return {"sent": sent, "missing": len(missing)}

    def _retry_page(self, event_id: str, site: str, error: str) -> None:
        row = next((x for x in self.store.page_outbox_snapshot()
                    if x["event_id"] == event_id and x["site"] == site), {})
        attempts = int(row.get("attempts", 0)) + 1
        self.store.mark_page(event_id, site, "retry", error,
                             next_retry_at=self._next_retry_at(attempts))

    def _next_retry_at(self, attempts: int) -> str:
        raw_retry_cfg = self._config.get("retry")
        retry_cfg: dict = raw_retry_cfg if isinstance(raw_retry_cfg, dict) else {}
        base = max(1, int(retry_cfg.get("base_seconds", 5) or 5))
        cap = max(base, int(retry_cfg.get("max_seconds", 300) or 300))
        delay = min(cap, base * (2 ** min(attempts - 1, 8))) + random.uniform(0, min(base, 5))
        due = datetime.now(timezone.utc) + timedelta(seconds=delay)
        return due.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _retry(self, event_id: str, error: str) -> None:
        row = next((x for x in self.store.outbox_snapshot() if x["event_id"] == event_id), {})
        attempts = int(row.get("attempts", 0)) + 1
        self.store.mark_retry(event_id, error, next_retry_at=self._next_retry_at(attempts))


def test_connection(config: dict) -> tuple[bool, str]:
    """提交空批次验证指纹环境接口连通性，不写事件。"""
    if not config.get("server_url"):
        return False, "请先填写服务器地址"
    endpoint = urljoin(str(config["server_url"]).rstrip("/") + "/", "api/fingerprint/events")
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(endpoint, json={"events": []}, timeout=float(config.get("timeout", 5) or 5))
        if response.status_code == 200:
            return True, "连接成功"
        return False, f"服务器返回 HTTP {response.status_code}"
    except Exception as exc:
        return False, f"连接失败: {exc}"
