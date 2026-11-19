"""环境检测：保存默认网站快照，并通过 CDP 打开人工检测页。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from vendor_runtime.legacy_core import local_http

try:
    from websocket import create_connection  # websocket-client
except ImportError:
    create_connection = None


log = logging.getLogger("WebMonitor")
_LOCAL_NO_PROXY = {"http": None, "https": None}
_write_lock = threading.RLock()
ENVIRONMENT_CHECK_SITES = (
    ("BrowserScan", "https://www.browserscan.net/"),
    ("CreepJS", "https://abrahamjuliot.github.io/creepjs/"),
)
_SENSITIVE_QUERY_KEYS = (
    "token", "key", "password", "passwd", "secret", "auth", "session",
    "code", "credential", "signature", "client_secret", "access_token",
)


class WebMonitorError(Exception):
    pass


def _base_dir() -> str:
    return os.environ['WINDOW_STUDIO_DATA']


def record_path() -> str:
    return os.path.join(_base_dir(), "运行状态", "web_monitor_records.jsonl")


def environment_check_urls(ip_address: str = "") -> list[dict]:
    """返回人工检测页；有效出口 IP 直达两家 IP 风险报告。"""
    normalized_ip = ""
    try:
        normalized_ip = str(ipaddress.ip_address((ip_address or "").strip()))
    except ValueError:
        pass
    result = []
    for name, base_url in ENVIRONMENT_CHECK_SITES:
        url = base_url
        if name == "Scamalytics" and normalized_ip:
            url = f"{base_url}/{normalized_ip}"
        # IP2Proxy 不再拼 ?ip=：我们测到的出口 IP 只是某一次测量的快照，
        # 代理一轮换，浏览器这条连接的真实出口就变了——拼上去等于让它查别人的 IP。
        # 直接开 /demo，让站点报它自己看到的那个 IP，才是这个窗口的真实情况。
        result.append({"name": name, "url": url})
    return result


def sanitize_url(url: str) -> str:
    """保留页面身份，移除凭据、fragment，并遮盖常见敏感查询参数。"""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        query = []
        for key, value in parse_qsl(p.query, keep_blank_values=True):
            if any(mark in key.lower() for mark in _SENSITIVE_QUERY_KEYS):
                value = "[REDACTED]"
            query.append((key, value))
        return urlunsplit((p.scheme, host, p.path, urlencode(query), ""))
    except Exception:
        return url.split("#", 1)[0]


def _url_matches(requested_url: str, actual_url: str) -> bool:
    if not requested_url:
        return bool(actual_url)
    try:
        requested = urlsplit(requested_url)
        actual = urlsplit(actual_url)
        req_host = (requested.hostname or "").lower().removeprefix("www.")
        act_host = (actual.hostname or "").lower().removeprefix("www.")
        req_path = requested.path.rstrip("/") or "/"
        act_path = actual.path.rstrip("/") or "/"
        return req_host == act_host and (req_path == "/" or act_path.startswith(req_path))
    except Exception:
        return requested_url == actual_url


def _list_pages(debug_port: int, timeout: float = 5.0) -> list[dict]:
    try:
        resp = local_http.get(
            f"http://127.0.0.1:{int(debug_port)}/json", timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise WebMonitorError(f"CDP 页面列表获取失败(port={debug_port}): {exc}") from exc
    return [
        item for item in data
        if isinstance(item, dict)
        and item.get("type") == "page"
        and item.get("webSocketDebuggerUrl")
        and not str(item.get("url", "")).startswith("devtools://")
    ]


def _browser_websocket_url(debug_port: int, timeout: float = 15.0) -> str:
    """等待浏览器级 CDP WebSocket 就绪。"""
    deadline = time.time() + max(timeout, 1.0)
    last_error = ""
    while time.time() < deadline:
        try:
            resp = local_http.get(
                f"http://127.0.0.1:{int(debug_port)}/json/version",
                timeout=min(3.0, max(timeout, 1.0)),
            )
            resp.raise_for_status()
            ws_url = str(resp.json().get("webSocketDebuggerUrl", ""))
            if ws_url:
                return ws_url
            last_error = "响应缺少 webSocketDebuggerUrl"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.3)
    raise WebMonitorError(
        f"浏览器 CDP 未就绪(port={debug_port}): {last_error or '未知错误'}"
    )


def open_environment_check_pages(debug_port: int, ip_address: str = "",
                                 timeout: float = 15.0, focus_url: str = "") -> dict:
    """打开环境检测标签页；已存在的同类页面不重复打开。

    ``focus_url`` 是用户的默认网站。**Chrome 每 `Target.createTarget` 一次就把新页推到前台**，
    开完五个，前台会停在最后创建的那一个——用户的默认网站早被挤走了。所以最后要显式指回去：
    有默认网站就回默认网站；没有才退回 BrowserScan（一页综合，比"碰巧最后创建的那个"有道理）。
    这一下纯粹是给人看的，采集全程在后台读页，不依赖谁在前台。
    """
    if not debug_port:
        raise WebMonitorError("debug_port 未就绪")
    ws_url = _browser_websocket_url(debug_port, timeout=timeout)
    existing_pages = _list_pages(debug_port, timeout=min(timeout, 5.0))
    opened, skipped, failed = [], [], []
    primary_target_id = ""
    # 默认网站那个标签页在开检测页之前就存在，先把它的 id 记下来
    focus_target_id = ""
    if focus_url:
        focus_page = next(
            (page for page in existing_pages
             if _url_matches(focus_url, str(page.get("url", "")))),
            None,
        )
        focus_target_id = str((focus_page or {}).get("id", ""))
    for site in environment_check_urls(ip_address):
        existing = next(
            (page for page in existing_pages
             if _url_matches(site["url"], str(page.get("url", "")))),
            None,
        )
        if existing:
            skipped.append(site["name"])
            if site["name"] == "BrowserScan":
                primary_target_id = str(existing.get("id", ""))
            continue
        try:
            result = _cdp_command(
                ws_url, "Target.createTarget", {"url": site["url"]},
                timeout=min(timeout, 10.0),
            )
            if not result.get("targetId"):
                raise WebMonitorError("CDP 未返回 targetId")
            opened.append(site["name"])
            if site["name"] == "BrowserScan":
                primary_target_id = str(result["targetId"])
            existing_pages.append({"url": site["url"]})
        except Exception as exc:
            failed.append({"name": site["name"], "error": str(exc)[:500]})
            log.warning("环境检测页打开失败 %s: %s", site["name"], exc)
    activate_id = focus_target_id or primary_target_id
    if activate_id:
        try:
            _cdp_command(
                ws_url, "Target.activateTarget", {"targetId": activate_id},
                timeout=min(timeout, 10.0),
            )
        except Exception as exc:
            # 标签页已成功创建，激活失败不应把检测整体判成失败。
            log.warning("标签页激活失败: %s", exc)
    return {
        "opened": opened,
        "skipped": skipped,
        "failed": failed,
        "total": len(ENVIRONMENT_CHECK_SITES),
    }


# 浏览器启动时是自己带着默认网站起来的（U 走 start_pages、O 走 start_url），
# 所以这里要先给它一点时间把那一次导航跑完，别抢在前面自己再导航一次——
# 用户看到的"网站自动刷新两次"就是这么来的。
STARTUP_NAV_GRACE_SECONDS = 6.0

# /json 里标签页地址已经写成目标网址、document.location 却还是 about:blank，
# 说明这次导航还在飞行中。这时候只能等它落地，**绝不能再导航一次**——
# 实测正是这一刀造成默认网站又加载一遍（CDP 录到 +0.92s 多出一次 Page.navigate）。
NAV_COMMIT_WAIT_SECONDS = 8.0

# 这些都算"标签页还是空的"，不是真的开了某个网站
_BLANK_URLS = ("", "about:blank", "about:newtab", "chrome://newtab/",
               "chrome://new-tab-page/", "edge://newtab/")


def _is_blank(url: str) -> bool:
    value = str(url or "").strip().lower().rstrip("/")
    return any(value == blank.rstrip("/") for blank in _BLANK_URLS)


def _has_real_page(pages: list[dict]) -> bool:
    """标签页里已经有真网页了——说明浏览器自己那一次导航已经跑完。"""
    return any(str(page.get("url", "")).startswith(("http://", "https://"))
               for page in pages)


def _pick_page(pages: list[dict], requested_url: str = "") -> dict | None:
    for page in pages:
        if _url_matches(requested_url, str(page.get("url", ""))):
            return page
    for page in pages:
        if str(page.get("url", "")).startswith(("http://", "https://")):
            return page
    return pages[0] if pages else None


def _cdp_command(ws_url: str, method: str, params: dict | None = None,
                 timeout: float = 10.0) -> dict:
    if create_connection is None:
        raise WebMonitorError("缺少 websocket-client，请 pip install websocket-client")
    try:
        ws = create_connection(ws_url, timeout=timeout, suppress_origin=True)
    except Exception as exc:
        raise WebMonitorError(f"CDP WebSocket 连接失败: {exc}") from exc
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        ws.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.recv()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if data.get("id") != 1:
                continue
            if data.get("error"):
                raise WebMonitorError(f"CDP {method} 失败: {data['error']}")
            return data.get("result") or {}
        raise WebMonitorError(f"CDP {method} 响应超时")
    except WebMonitorError:
        raise
    except Exception as exc:
        raise WebMonitorError(f"CDP {method} 通信失败: {exc}") from exc
    finally:
        try:
            ws.close()
        except Exception:
            pass


_FINGERPRINT_JS = r"""
(() => {
  const safe = (fn, fallback = null) => { try { return fn(); } catch (_) { return fallback; } };
  const hashText = (text) => {
    let h = 2166136261;
    for (let i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0).toString(16).padStart(8, '0');
  };
  const canvasHash = safe(() => {
    const c = document.createElement('canvas');
    c.width = 280; c.height = 60;
    const x = c.getContext('2d');
    x.textBaseline = 'top';
    x.font = '16px Arial';
    x.fillStyle = '#f60'; x.fillRect(10, 10, 100, 25);
    x.fillStyle = '#069'; x.fillText('Undetectable Monitor 2026', 4, 4);
    x.globalCompositeOperation = 'multiply';
    x.fillStyle = 'rgba(102,204,0,0.7)'; x.beginPath(); x.arc(190, 28, 20, 0, Math.PI * 2); x.fill();
    return hashText(c.toDataURL());
  }, '');
  const webgl = safe(() => {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return {vendor: '', renderer: ''};
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    return {
      vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
      renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)
    };
  }, {vendor: '', renderer: ''});
  const uaData = safe(() => navigator.userAgentData && navigator.userAgentData.toJSON
    ? navigator.userAgentData.toJSON() : null, null);
  return {
    page: {
      url: location.href,
      title: document.title || '',
      ready_state: document.readyState || ''
    },
    fingerprint: {
      user_agent: navigator.userAgent || '',
      ua_data: uaData,
      platform: navigator.platform || '',
      language: navigator.language || '',
      languages: Array.from(navigator.languages || []),
      timezone: safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone, ''),
      timezone_offset: new Date().getTimezoneOffset(),
      screen: {
        width: screen.width, height: screen.height,
        avail_width: screen.availWidth, avail_height: screen.availHeight,
        color_depth: screen.colorDepth, pixel_depth: screen.pixelDepth,
        device_pixel_ratio: window.devicePixelRatio || 1
      },
      hardware_concurrency: navigator.hardwareConcurrency || null,
      device_memory: navigator.deviceMemory || null,
      max_touch_points: navigator.maxTouchPoints || 0,
      webdriver: navigator.webdriver === true,
      cookie_enabled: navigator.cookieEnabled === true,
      do_not_track: navigator.doNotTrack || '',
      plugin_count: safe(() => navigator.plugins.length, 0),
      mime_type_count: safe(() => navigator.mimeTypes.length, 0),
      canvas_hash: canvasHash,
      webgl: webgl
    }
  };
})()
"""


def collect_page_snapshot(debug_port: int, requested_url: str = "",
                          timeout: float = 15.0) -> dict:
    """找到页面；目标页未打开时主动 Page.navigate，然后采集浏览器可见指纹。"""
    if not debug_port:
        raise WebMonitorError("debug_port 未就绪")
    deadline = time.time() + max(timeout, 1.0)
    # 先等浏览器自己那一次导航落地：只要标签页还全是空白，就说明它还在起，
    # 这时候自己导航一次，用户就会看到页面又刷了一遍。
    grace = min(STARTUP_NAV_GRACE_SECONDS, max(timeout, 1.0) / 2.0)
    grace_deadline = time.time() + grace
    page = None
    while time.time() < deadline:
        pages = _list_pages(debug_port)
        page = _pick_page(pages, requested_url)
        if page is not None:
            # 已经落到目标页 / 已经有真网页了 / 等够了 → 不再干等
            if (not requested_url
                    or _url_matches(requested_url, str(page.get("url", "")))
                    or _has_real_page(pages)
                    or time.time() >= grace_deadline):
                break
        time.sleep(0.4)
    if page is None:
        raise WebMonitorError("没有可监控的浏览器页面")

    # 只有"标签页确实还是空的"才由我们来导航。已经开着真网页却不匹配，多半是
    # 站点自己跳转（登录页、跨域重定向），这时候再导航一次就是硬把用户拽回去。
    if (requested_url and not _url_matches(requested_url, str(page.get("url", "")))
            and _is_blank(str(page.get("url", "")))):
        navigate_result = _cdp_command(
            page["webSocketDebuggerUrl"], "Page.navigate",
            {"url": requested_url}, timeout=min(timeout, 10.0),
        )
        if navigate_result.get("errorText"):
            raise WebMonitorError(f"CDP 打开网页失败: {navigate_result['errorText']}")
        while time.time() < deadline:
            pages = _list_pages(debug_port)
            matched = next(
                (item for item in pages
                 if _url_matches(requested_url, str(item.get("url", "")))),
                None,
            )
            if matched:
                page = matched
                break
            time.sleep(0.4)

    # 页面发生跳转后 /json 中的 websocket 地址可能刷新，重新取一次当前目标页。
    pages = _list_pages(debug_port)
    page = _pick_page(pages, requested_url) or page
    def _evaluate(current_page: dict) -> dict | None:
        result = _cdp_command(
            current_page["webSocketDebuggerUrl"],
            "Runtime.evaluate",
            {"expression": _FINGERPRINT_JS, "returnByValue": True, "awaitPromise": False},
            timeout=min(timeout, 10.0),
        )
        return (result.get("result") or {}).get("value")

    def _actual_url(payload) -> str:
        page_part = (payload or {}).get("page") if isinstance(payload, dict) else None
        return str(page_part.get("url", "")) if isinstance(page_part, dict) else ""

    value = _evaluate(page)
    actual_url = _actual_url(value)
    # 读到空白页不代表没打开——导航很可能还没提交。先等它落地再说。
    if requested_url and _is_blank(actual_url):
        commit_deadline = time.time() + min(NAV_COMMIT_WAIT_SECONDS,
                                            max(deadline - time.time(), 0.0))
        while time.time() < commit_deadline:
            time.sleep(0.3)
            pages = _list_pages(debug_port)
            page = _pick_page(pages, requested_url) or page
            value = _evaluate(page)
            actual_url = _actual_url(value)
            if not _is_blank(actual_url):
                break

    # 浏览器启动阶段偶尔会在第一次 Page.navigate 后又把标签重置成 about:blank。
    # 等过之后仍是空白，才认为浏览器确实没把网页打开，由我们补一次导航。
    # **只补空白页这一种**：已经在某个真网页上（站点自己跳转了）就不要再导航，
    # 否则用户眼里就是刚打开的页面又刷新一次。
    if requested_url and not _url_matches(requested_url, actual_url) and _is_blank(actual_url):
        navigate_result = _cdp_command(
            page["webSocketDebuggerUrl"], "Page.navigate",
            {"url": requested_url}, timeout=min(timeout, 10.0),
        )
        if navigate_result.get("errorText"):
            raise WebMonitorError(f"CDP 打开网页失败: {navigate_result['errorText']}")
        retry_deadline = time.time() + min(max(timeout, 1.0), 6.0)
        while time.time() < retry_deadline:
            pages = _list_pages(debug_port)
            matched = next(
                (item for item in pages
                 if _url_matches(requested_url, str(item.get("url", "")))),
                None,
            )
            if matched:
                page = matched
                break
            time.sleep(0.3)
        value = _evaluate(page)
    if not isinstance(value, dict):
        raise WebMonitorError("CDP 指纹采集未返回对象")
    final_url = ((value.get("page") or {}).get("url", "")
                 if isinstance(value.get("page"), dict) else "")
    # 落在别的网址上但确实是个真网页 → 站点自己跳转（登录页/跨域重定向），照常记录，
    # 记录里 requested_url 与 url 两个字段都在，对得上账。只有还停在空白页才算没打开。
    if requested_url and not _url_matches(requested_url, final_url) and _is_blank(final_url):
        raise WebMonitorError(f"目标网页未打开，浏览器当前页: {sanitize_url(final_url) or '未知'}")
    return value


def _fingerprint_hash(fingerprint: dict) -> str:
    raw = json.dumps(fingerprint or {}, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_record(profile_id: str, profile: dict, requested_url: str,
                 ip_check: dict | None, page_snapshot: dict | None = None,
                 error: str = "") -> dict:
    page_snapshot = page_snapshot or {}
    page = page_snapshot.get("page") if isinstance(page_snapshot.get("page"), dict) else {}
    fingerprint = (page_snapshot.get("fingerprint")
                   if isinstance(page_snapshot.get("fingerprint"), dict) else {})
    ip_check = ip_check or {}
    has_page = bool(page.get("url"))
    has_ip = bool(ip_check.get("actual_ip"))
    status = "complete" if has_page and has_ip and not error else "partial"
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": status,
        "profile": {
            "id": profile_id,
            "name": profile.get("name", ""),
            "browser": profile.get("browser", ""),
            "os_label": profile.get("os_label", ""),
            "proxy_type": profile.get("proxy_type", ""),
        },
        "page": {
            "requested_url": sanitize_url(requested_url),
            "url": sanitize_url(str(page.get("url", ""))),
            "title": str(page.get("title", ""))[:500],
            "ready_state": str(page.get("ready_state", "")),
        },
        "fingerprint": fingerprint,
        "fingerprint_hash": _fingerprint_hash(fingerprint) if fingerprint else "",
        "ip": {
            "address": ip_check.get("actual_ip", ""),
            "city": ip_check.get("actual_city", ""),
            "region": ip_check.get("actual_region", ""),
            "zip": ip_check.get("actual_zip", ""),
            "query_failed": bool(ip_check.get("query_failed")),
            "dual_breakdown": ip_check.get("dual_breakdown", ""),
        },
        "error": str(error)[:1000],
    }


def append_record(record: dict, file_path: str | None = None) -> str:
    path = file_path or record_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _write_lock:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    return path


def capture_and_save(profile_id: str, profile: dict, requested_url: str,
                     debug_port: int, ip_check: dict | None,
                     file_path: str | None = None) -> dict:
    snapshot = None
    error = ""
    try:
        snapshot = collect_page_snapshot(debug_port, requested_url=requested_url)
    except Exception as exc:
        error = str(exc)
        log.warning(f"网页监控采集失败 profile={profile_id[:12]}: {error}")
    record = build_record(
        profile_id, profile, requested_url, ip_check,
        page_snapshot=snapshot, error=error,
    )
    path = append_record(record, file_path=file_path)
    record["record_path"] = path
    return record
