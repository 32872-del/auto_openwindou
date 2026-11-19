"""客户端指纹环境 SQLite 权威库。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone

from vendor_runtime.legacy_core.environment_identity import LAUNCH_ID_RE, folder_name, new_launch_id, safe_join
from vendor_runtime.legacy_core.environment_schema import build_event, new_environment_id, validate_event


_SEARCH_ID_RE = re.compile(
    r"(?:env|evt)_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"|\d{4}-\d{2}-\d{2}(?:_\d{2}-\d{2}_|-)[A-Za-z0-9]{7}",
    re.IGNORECASE,
)


def _base_dir() -> str:
    return os.environ['WINDOW_STUDIO_DATA']


def default_db_path() -> str:
    return os.path.join(_base_dir(), "运行状态", "检测记录.sqlite")


def pages_root() -> str:
    """检测页存档根目录，与 SQLite 库同级。"""
    return os.environ.get('WINDOW_STUDIO_ARCHIVES',os.path.join(_base_dir(),'网页存档'))


def page_dir(folder: str, launch_id: str) -> str:
    """一次采集的存档目录 ``<客户端别名>-<客户端编号>/<启动编号>/``，双端一致。"""
    return safe_join(pages_root(), str(folder), str(launch_id))


def event_page_dir(event: dict) -> str:
    """从事件里推出它的存档目录；只认合法启动编号，防路径穿越。"""
    source = event.get("source") or {}
    launch_id = str(event.get("launch_id", ""))
    if not LAUNCH_ID_RE.fullmatch(launch_id):
        raise ValueError("launch_id 无效")
    return page_dir(folder_name(source.get("client_name", ""),
                                source.get("client_number", "")), launch_id)


def assets_dir() -> str:
    """所有存档共用的 CSS 目录；页面里用 ../../_assets/ 指过来。"""
    return os.path.join(pages_root(), "_assets")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _search_tokens(query: str) -> list[str]:
    """标准环境/事件 ID 优先，其余混合文本再拆为联合模糊搜索词。"""
    text = str(query or "")
    ids = [match.group(0).casefold() for match in _SEARCH_ID_RE.finditer(text)]
    remainder = _SEARCH_ID_RE.sub(" ", text)
    generic = [
        token.casefold()
        for token in re.split(r"[^\w.\-]+", remainder, flags=re.UNICODE)
        if token
    ]
    return list(dict.fromkeys(ids + generic))


def _scalar_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _scalar_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _scalar_values(child)
    elif value is not None:
        yield str(value)


def _event_search_text(event: dict) -> str:
    """只索引可复盘字段的值，不让 JSON 键名造成无意义命中。"""
    values = [event.get("environment_id", ""), event.get("event_id", ""),
              event.get("launch_id", ""), event.get("fingerprint_hash", "")]
    for key in ("source", "profile", "fingerprint", "ip", "latency"):
        values.extend(_scalar_values(event.get(key, {})))
    values.extend(_scalar_values(event.get('checks', {}).get('tls', {}).get('fields', {})))
    browser = str((event.get("profile") or {}).get("browser", "")).casefold()
    if browser == "o":
        values.extend(("octo", "octobrowser"))
    elif browser == "u":
        values.append("undetectable")
    return " ".join(str(value) for value in values).casefold()


def _latency_text(latency: dict | None) -> str:
    """摘要里的延迟一列：``US 568 ms / CA 1115 ms``；没测到就留空。"""
    if not isinstance(latency, dict):
        return ""
    parts = []
    for key, label in (("us_avg_ms", "US"), ("ca_avg_ms", "CA")):
        value = latency.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{label} {int(round(value))} ms")
    return " / ".join(parts)


def _event_summary(event: dict) -> dict:
    source = event.get("source") or {}
    profile = event.get("profile") or {}
    ip = event.get("ip") or {}
    return {
        "environment_id": event.get("environment_id", ""),
        "event_id": event.get("event_id", ""),
        "launch_id": event.get("launch_id", ""),
        "client_number": source.get("client_number", ""),
        "pages": {name: str((item or {}).get("page", ""))
                  for name, item in (event.get("checks") or {}).items()
                  if isinstance(item, dict) and item.get("page")},
        "captured_at": event.get("captured_at", ""),
        "status": event.get("status", ""),
        "capture_reason": event.get("capture_reason", ""),
        "client_id": source.get("client_id", ""),
        "client_name": source.get("client_name", ""),
        "profile_id": profile.get("id", ""),
        "profile_name": profile.get("name", ""),
        "browser": profile.get("browser", ""),
        "os_label": profile.get("os_label", ""),
        "ip": ip.get("address", ""),
        "location": " ".join(str(ip.get(key, "")) for key in ("city", "region", "zip") if ip.get(key)),
        "fingerprint_hash": event.get("fingerprint_hash", ""),
        "latency": _latency_text(event.get("latency")),
    }


class EnvironmentStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = os.path.abspath(db_path or default_db_path())
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _connection(self):
        """提交/回滚事务并确保 SQLite 连接始终被关闭。"""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS environments (
                    environment_id TEXT PRIMARY KEY,
                    launch_id TEXT NOT NULL DEFAULT '',
                    client_id TEXT NOT NULL,
                    client_name TEXT NOT NULL DEFAULT '',
                    profile_id TEXT NOT NULL DEFAULT '',
                    profile_name TEXT NOT NULL DEFAULT '',
                    browser TEXT NOT NULL DEFAULT '',
                    os_label TEXT NOT NULL DEFAULT '',
                    ip_address TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    latest_event_id TEXT NOT NULL,
                    latest_captured_at TEXT NOT NULL,
                    latest_status TEXT NOT NULL,
                    latest_summary_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_env_profile ON environments(client_id, profile_id);
                CREATE INDEX IF NOT EXISTS idx_env_latest ON environments(latest_captured_at DESC);
                CREATE TABLE IF NOT EXISTS page_outbox (
                    event_id TEXT NOT NULL,
                    site TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(event_id, site)
                );
                CREATE TABLE IF NOT EXISTS environment_events (
                    event_id TEXT PRIMARY KEY,
                    environment_id TEXT NOT NULL REFERENCES environments(environment_id),
                    captured_at TEXT NOT NULL,
                    capture_reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_event_env ON environment_events(environment_id, captured_at DESC);
                CREATE TABLE IF NOT EXISTS sync_outbox (
                    event_id TEXT PRIMARY KEY REFERENCES environment_events(event_id),
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due ON sync_outbox(status, next_retry_at);
                CREATE TABLE IF NOT EXISTS archive_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_imports (
                    line_hash TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL
                );
            """)

    def save_event(self, event: dict, queue_remote: bool = True) -> bool:
        validate_event(event)
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        profile, source, ip = event["profile"], event["source"], event["ip"]
        summary = json.dumps({
            "profile": profile, "source": source, "ip": ip,
            "fingerprint_hash": event.get("fingerprint_hash", ""),
        }, ensure_ascii=False, separators=(",", ":"))
        now = event["captured_at"]
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM environment_events WHERE event_id=?", (event["event_id"],)
            ).fetchone()
            if exists:
                conn.rollback()
                return False
            conn.execute("""
                INSERT INTO environments(
                    environment_id, launch_id, client_id, client_name, profile_id, profile_name,
                    browser, os_label, ip_address, created_at, latest_event_id,
                    latest_captured_at, latest_status, latest_summary_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(environment_id) DO UPDATE SET
                    launch_id=excluded.launch_id,
                    client_name=excluded.client_name, profile_id=excluded.profile_id,
                    profile_name=excluded.profile_name, browser=excluded.browser,
                    os_label=excluded.os_label, ip_address=excluded.ip_address,
                    latest_event_id=excluded.latest_event_id,
                    latest_captured_at=excluded.latest_captured_at,
                    latest_status=excluded.latest_status,
                    latest_summary_json=excluded.latest_summary_json
                WHERE excluded.latest_captured_at > environments.latest_captured_at
                   OR (excluded.latest_captured_at = environments.latest_captured_at
                       AND excluded.latest_event_id > environments.latest_event_id)
            """, (
                event["environment_id"], event.get("launch_id", ""),
                source["client_id"], source.get("client_name", ""),
                profile.get("id", ""), profile.get("name", ""), profile.get("browser", ""),
                profile.get("os_label", ""), ip.get("address", ""), now,
                event["event_id"], now, event["status"], summary,
            ))
            conn.execute(
                "INSERT INTO environment_events VALUES(?,?,?,?,?,?)",
                (event["event_id"], event["environment_id"], now,
                 event["capture_reason"], event["status"], payload),
            )
            if queue_remote:
                conn.execute(
                    "INSERT INTO sync_outbox(event_id,status,attempts,next_retry_at,last_error,updated_at) VALUES(?,?,?,?,?,?)",
                    (event["event_id"], "pending", 0, now, "", now),
                )
                # 页面存档单独排队：走另一条通道，失败不拖累事件本身
                folder = event_page_dir(event)
                names = [(site, str(item.get("page", "") or ""))
                         for site, item in (event.get("checks") or {}).items()]
                names.append(("data", "data.json"))   # 附带的抽取结果跟着一起上传
                for site, name in names:
                    if not name or not os.path.exists(os.path.join(folder, name)):
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO page_outbox"
                        "(event_id,site,path,status,attempts,next_retry_at,last_error,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (event["event_id"], site, os.path.join(folder, name),
                         "pending", 0, now, "", now),
                    )
            conn.execute(
                "INSERT INTO archive_meta(key,value) VALUES('xlsx_dirty','1') ON CONFLICT(key) DO UPDATE SET value='1'"
            )
            conn.commit()
        return True

    def get_event(self, event_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute("SELECT payload_json FROM environment_events WHERE event_id=?", (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_environment(self, environment_id: str) -> dict | None:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM environment_events WHERE environment_id=? ORDER BY captured_at,event_id",
                (environment_id,),
            ).fetchall()
        if not rows:
            return None
        events = [json.loads(row[0]) for row in rows]
        return {"environment_id": environment_id, "latest": events[-1], "events": events}

    def all_events(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute("SELECT payload_json FROM environment_events ORDER BY captured_at,event_id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def search_environments(self, query: str = "", limit: int = 500) -> list[dict]:
        """按环境聚合搜索所有历史事件，并返回每个环境的最新摘要。"""
        tokens = _search_tokens(query)
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT env.environment_id, env.latest_event_id, env.latest_captured_at,
                       event.event_id, event.payload_json
                FROM environments env
                JOIN environment_events event ON event.environment_id=env.environment_id
                ORDER BY env.latest_captured_at DESC, env.environment_id, event.captured_at DESC, event.event_id DESC
            """).fetchall()
        grouped: dict[str, dict] = {}
        for row in rows:
            group = grouped.setdefault(row["environment_id"], {"latest": None, "search": []})
            event = json.loads(row["payload_json"])
            group["search"].append(_event_search_text(event))
            if row["event_id"] == row["latest_event_id"]:
                group["latest"] = event
        results = []
        for group in grouped.values():
            haystack = " ".join(group["search"])
            if tokens and not any(token in haystack for token in tokens):
                continue
            latest = group["latest"]
            if latest:
                results.append(_event_summary(latest))
            if len(results) >= max(1, min(int(limit), 5000)):
                break
        return results

    def outbox_snapshot(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM sync_outbox ORDER BY updated_at,event_id").fetchall()
        return [dict(row) for row in rows]

    def due_events(self, limit: int = 20, now_iso: str | None = None,
                   *, one_per_environment: bool = False) -> list[dict]:
        now_iso = now_iso or _utc_now_iso()
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT payload_json FROM (
                    SELECT e.payload_json, o.next_retry_at, o.event_id,
                        ROW_NUMBER() OVER (PARTITION BY e.environment_id
                            ORDER BY o.next_retry_at,o.event_id) AS position
                    FROM sync_outbox o JOIN environment_events e ON e.event_id=o.event_id
                    WHERE o.status IN ('pending','retry') AND o.next_retry_at<=?
                ) WHERE (?=0 OR position=1)
                ORDER BY next_retry_at,event_id LIMIT ?
            """, (now_iso, int(one_per_environment), max(1, min(int(limit), 20)))).fetchall()
        return [json.loads(row[0]) for row in rows]

    def due_pages(self, limit: int = 8, now_iso: str | None = None,
                  *, one_per_path: bool = False) -> list[dict]:
        now_iso = now_iso or _utc_now_iso()
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT event_id,site,path,environment_id FROM (
                    SELECT s.event_id,s.site,s.path,e.environment_id,s.next_retry_at,
                        ROW_NUMBER() OVER (PARTITION BY s.path
                            ORDER BY s.next_retry_at,s.event_id,s.site) AS position
                    FROM page_outbox s JOIN environment_events e ON e.event_id=s.event_id
                    WHERE s.status IN ('pending','retry') AND s.next_retry_at<=?
                ) WHERE (?=0 OR position=1)
                ORDER BY next_retry_at,event_id,site LIMIT ?
            """, (now_iso, int(one_per_path), max(1, min(int(limit), 20)))).fetchall()
        return [dict(row) for row in rows]

    def mark_page(self, event_id: str, site: str, status: str,
                        error: str = "", next_retry_at: str | None = None) -> None:
        now = _utc_now_iso()
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE page_outbox SET status=?, attempts=attempts+?, next_retry_at=?,"
                " last_error=?, updated_at=? WHERE event_id=? AND site=?",
                (status, 0 if status == "sent" else 1, next_retry_at or now,
                 str(error)[:1000], now, event_id, site),
            )

    def page_outbox_snapshot(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT event_id, site, path, status, attempts, last_error FROM page_outbox"
                " ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        now = _utc_now_iso()
        with self._lock, self._connection() as conn:
            conn.executemany(
                "UPDATE sync_outbox SET status='sent',last_error='',updated_at=? WHERE event_id=?",
                [(now, event_id) for event_id in event_ids],
            )

    def mark_retry(self, event_id: str, error: str, *, permanent: bool = False,
                   next_retry_at: str | None = None) -> None:
        now = _utc_now_iso()
        next_retry_at = next_retry_at or now
        status = "failed" if permanent else "retry"
        with self._lock, self._connection() as conn:
            conn.execute("""
                UPDATE sync_outbox SET status=?, attempts=attempts+1,
                    next_retry_at=?, last_error=?, updated_at=? WHERE event_id=?
            """, (status, next_retry_at, str(error)[:1000], now, event_id))

    def revive_failed(self) -> int:
        """把此前判死的补传任务复活成待重试。

        服务端版本落后（例如还不认识新字段）会回 4xx，旧逻辑直接判永久失败，
        服务端升级后这些事件再也不会补传。启动/改配置时给它们一次机会。
        """
        now = _utc_now_iso()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE sync_outbox SET status='retry', attempts=0, next_retry_at=?, updated_at=?"
                " WHERE status='failed'", (now, now),
            )
            shots = conn.execute(
                "UPDATE page_outbox SET status='retry', attempts=0, next_retry_at=?, updated_at=?"
                " WHERE status='failed'", (now, now),
            )
            return int(cursor.rowcount or 0) + int(shots.rowcount or 0)

    def enqueue_missing_local_archives(self) -> dict[str, int]:
        """补齐旧版仅本地保存的事件/网页；已发送或已排队的状态保持原样。"""
        now = _utc_now_iso()
        added_events = added_pages = 0
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT e.event_id,e.payload_json FROM environment_events e "
                "LEFT JOIN sync_outbox o ON o.event_id=e.event_id WHERE o.event_id IS NULL"
            ).fetchall()
            for row in rows:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO sync_outbox"
                    "(event_id,status,attempts,next_retry_at,last_error,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (row['event_id'], 'pending', 0, now, '', now),
                )
                added_events += cursor.rowcount
            for row in conn.execute("SELECT event_id,payload_json FROM environment_events"):
                event = json.loads(row['payload_json'])
                try:
                    folder = event_page_dir(event)
                except ValueError:
                    continue
                names = [(site, str(item.get('page', '') or ''))
                         for site, item in (event.get('checks') or {}).items()]
                names.append(('data', 'data.json'))
                for site, name in names:
                    if not name or not os.path.isfile(os.path.join(folder, name)):
                        continue
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO page_outbox"
                        "(event_id,site,path,status,attempts,next_retry_at,last_error,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (row['event_id'], site, os.path.join(folder, name),
                         'pending', 0, now, '', now),
                    )
                    added_pages += cursor.rowcount
                    conn.execute(
                        "UPDATE page_outbox SET path=? WHERE event_id=? AND site=? AND path<>?",
                        (os.path.join(folder, name), row['event_id'], site, os.path.join(folder, name)),
                    )
        return {'events': added_events, 'pages': added_pages}

    def retry_unsent_now(self) -> dict[str, int]:
        """手动重试时提前唤醒尚未确认送达的事件和页面。"""
        now = _utc_now_iso()
        with self._lock, self._connection() as conn:
            events = conn.execute(
                "UPDATE sync_outbox SET status='retry',next_retry_at=?,updated_at=? "
                "WHERE status IN ('pending','retry','failed')", (now, now),
            ).rowcount
            pages = conn.execute(
                "UPDATE page_outbox SET status='retry',next_retry_at=?,updated_at=? "
                "WHERE status IN ('pending','retry','failed')", (now, now),
            ).rowcount
        return {'events': events, 'pages': pages}

    def set_xlsx_clean(self) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO archive_meta(key,value) VALUES('xlsx_dirty','0') ON CONFLICT(key) DO UPDATE SET value='0'"
            )

    def import_legacy_jsonl(self, file_path: str, *, client_id: str,
                            client_name: str = "", queue_remote: bool = False) -> dict:
        """幂等导入旧 schema v1 JSONL；每条旧快照成为独立环境。"""
        imported = skipped = invalid = 0
        if not os.path.exists(file_path):
            return {"imported": 0, "skipped": 0, "invalid": 0}
        with open(file_path, "r", encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                digest = hashlib.sha256(raw.rstrip("\r\n").encode("utf-8")).hexdigest()
                with self._connection() as conn:
                    if conn.execute("SELECT 1 FROM legacy_imports WHERE line_hash=?", (digest,)).fetchone():
                        skipped += 1
                        continue
                try:
                    old = json.loads(raw)
                    if old.get("schema_version") != 1:
                        raise ValueError("not schema v1")
                    profile = old.get("profile") if isinstance(old.get("profile"), dict) else {}
                    page = old.get("page") if isinstance(old.get("page"), dict) else {}
                    fingerprint = old.get("fingerprint") if isinstance(old.get("fingerprint"), dict) else {}
                    old_ip = old.get("ip") if isinstance(old.get("ip"), dict) else {}
                    event = build_event(
                        environment_id=new_environment_id(), launch_id=new_launch_id(),
                        capture_reason="manual",
                        source={"client_id": client_id, "client_name": client_name},
                        profile=profile,
                        page_snapshot={"page": page, "fingerprint": fingerprint},
                        ip_check={
                            "actual_ip": old_ip.get("address", ""), "actual_city": old_ip.get("city", ""),
                            "actual_region": old_ip.get("region", ""), "actual_zip": old_ip.get("zip", ""),
                            "query_failed": old_ip.get("query_failed", False),
                            "dual_breakdown": old_ip.get("dual_breakdown", ""),
                        }, checks={}, error=old.get("error", ""),
                    )
                    self.save_event(event, queue_remote=queue_remote)
                    with self._connection() as conn:
                        conn.execute("INSERT OR IGNORE INTO legacy_imports(line_hash,event_id) VALUES(?,?)", (digest, event["event_id"]))
                    imported += 1
                except Exception:
                    invalid += 1
        return {"imported": imported, "skipped": skipped, "invalid": invalid}
