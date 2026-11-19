"""一次性 DrissionPage 子进程：只连接、采集、断开，不管理浏览器生命周期。"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from vendor_runtime.legacy_core.environment_page_archive import capture_page
from vendor_runtime.legacy_core.environment_diagnostic_schema import (
    SITE_BUSY_MARKERS,
    SITE_BUSY_TOLERANCE,
    SITE_HOSTS,
    SITE_MAX_REFRESH,
    SITE_MINIMUMS,
    SITE_REFRESH_AFTER,
    SITE_REQUIRED_GRACE,
    SITE_READY_MARKERS,
    SITE_SCHEMAS,
    all_aliases,
    normalize_candidates,
    result_status,
)


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_ERROR_CHARS = 1000
# 这是超时上限，不是固定等待：每轮都读一次目标元素，内容一符合格式就立刻收工。
# BrowserScan 的 DNS 是整页**最后**才出来的一项，等到它 = 整页指纹（visitor ID / Canvas /
# WebGL / Audio / Client Rects / WebGPU / 屏幕等）都已经算完，这时候快照才有值。
# 本机直连约 13 秒；住宅代理慢时实测 60 秒都不够，所以放到 150 秒。
NORMAL_RESULT_WAIT_SECONDS = 150.0
# 「整页跑完」的判据：白名单字段 + 整页文本连续这么久不再变化。
# 光看白名单字段不够——实测 browserleaks 的 JA3/JA4/Akamai/Whois 要 0.8~3.5 秒才补上，
# leakdig 的指纹真实度会从 69% 一路重算到 85%、DNS 泄漏 25.4 秒才出，
# 而这些都不在白名单里，只盯字段会在 1 秒就收工，存下一份半截页面。
PAGE_QUIET_SECONDS = 5.0
# 页面连一个字都没渲染出来、也没有盾，等这么久就直接判失败——多半是代理不通。
# 实测有一轮代理是死的，五站各自白烧 150 秒才收工，一共浪费两分半。
EMPTY_PAGE_BAIL_SECONDS = 45.0
# 单次 run_js 的超时。原来写死 5 秒，实测 3 个窗口并发时 browserscan 那种 400KB 的 DOM
# 经常一轮就超，而超时会让整站直接判 failed —— 一次卡顿废掉整站，白瞎 150 秒预算。
# 现在放宽到 20 秒，并且允许连续失败几次后再放弃。
JS_CALL_TIMEOUT = 20.0
JS_MAX_CONSECUTIVE_ERRORS = 4
# 等页面上"检测中"消失的预算；超了就不再等，把还剩几个写进 error。
# 必须大于 SITE_REFRESH_AFTER，否则刷新前预算就到期了，刷新等于白刷。
BUSY_WAIT_SECONDS = 60.0
CHALLENGE_AUTO_PASS_SECONDS = 60.0   # 过盾总预算（四站并行，总耗时取最慢一站）
# Cloudflare 有两种盾：
#   1) Turnstile 交互式——页面上有 iframe/shadow-root 的复选框，要点。
#   2) Managed Challenge 非交互式——**整页零个 iframe、零个 shadow root**，
#      靠 JS 在后台跑，压根没有可点的东西（实测 scamalytics 的盾就是这种：
#      正文写着 "Performing security verification"，唯一的可见小方块是页脚的 ray-id）。
# 对第 2 种，点击纯属浪费预算：它要的是时间和重试。所以只在**确实有 widget** 时才点，
# 没有 widget 就安静等，等不过就原地刷新一次（换一次挑战往往就过了）。
CHALLENGE_RELOAD_AFTER = 20.0        # 非交互盾：等这么久还没放行就刷新
CHALLENGE_MAX_RELOAD = 2
CHALLENGE_CLICK_ATTEMPTS = 30        # 安全上限，正常在预算内一直点到放行为止，不回落人工
CHALLENGE_SETTLE_SECONDS = 1.5
_FIRST_CLICK_OFFSET = 15
_MAX_CLICK_OFFSET = 40
_RESET_CLICK_OFFSET = 10
_CHALLENGE_ERROR = "自动过盾未通过：已自动点击 {clicks} 次、等待 {seconds:.0f} 秒仍停留在验证页"
# 采完就关标签页时，浏览器至少要留下这么多个标签页——关到 0 个 Chrome 会直接退出，
# 窗口消失会被监听线程当成"用户关窗"，进而触发自动删除窗口。
MIN_TABS_LEFT_OPEN = 1
# 五秒盾承载元素：首选与通用配置一致的网格容器，其余为 Turnstile 常见挂载点。
_CHALLENGE_HOSTS = (
    "css:div[style*='display: grid'] div div",
    "css:.cf-turnstile",
    "css:#turnstile-wrapper",
    "css:#challenge-stage",
    "css:div[id^='cf-chl-widget']",
    # 下面这几个是"没有任何 id/class 特征"的变体：实测 scamalytics 的盾页整页只有
    # main-wrapper / main-content，上面 5 个选择器一个都匹配不到，49 秒里根本没东西可点。
    "css:.main-content div",
    "css:.main-wrapper div",
    "css:body div div",
)

_DOM_SCRIPT = r"""
(() => {
  const aliases = __ALIASES__;
  const compact = value => String(value || '').normalize('NFKC').replace(/\s+/g, ' ').trim();
  const norm = value => compact(value).replace(/[:：-]+$/, '').trim().toLowerCase();
  const known = new Set(aliases.map(norm));
  const pairs = [];
  const pairKeys = new Set();
  const isMarkup = text => /<[a-z!/][^>]*>/i.test(text);
  const isNoscript = node => String(node?.tagName || '').toUpperCase() === 'NOSCRIPT';
  const directText = node => isNoscript(node) ? '' : compact(Array.from(node?.childNodes || [])
    .filter(child => child.nodeType === 3).map(child => child.nodeValue || '').join(' '));
  const visibleText = node => isNoscript(node) ? ''
    : compact(node?.innerText || node?.textContent || '');
  const push = (label, value) => {
    label = compact(label);
    value = compact(value);
    if (!known.has(norm(label)) || !value || value.length > 300 || pairs.length >= 240) return;
    const foldedValue = value.toLowerCase();
    if (foldedValue.includes('detectionscience')) return;
    for (const alias of known) {
      if (foldedValue.includes(alias + ':') || foldedValue.includes(alias + '：')) return;
    }
    const key = norm(label) + '\u0000' + value;
    if (pairKeys.has(key)) return;
    pairKeys.add(key);
    pairs.push([label, value]);
  };
  const roots = [document];
  const seenRoots = new Set();
  const snapshots = [];
  for (let index = 0; index < roots.length && index < 80; index++) {
    const root = roots[index];
    if (!root || seenRoots.has(root)) continue;
    seenRoots.add(root);
    let all = [];
    try { all = Array.from(root.querySelectorAll('*')).slice(0, 8000); } catch (_) {}
    snapshots.push({root, all});
    for (const element of all) {
      try {
        if (element.shadowRoot && !seenRoots.has(element.shadowRoot)) roots.push(element.shadowRoot);
      } catch (_) {}
      if (String(element.tagName || '').toLowerCase() === 'iframe') {
        try {
          if (element.contentDocument && !seenRoots.has(element.contentDocument)) roots.push(element.contentDocument);
        } catch (_) {}
      }
    }
  }

  const candidateText = node => {
    const candidates = [];
    const addCandidate = value => {
      const text = compact(value);
      if (text && text.length <= 300 && !/^[()\[\]{}\s—-]+$/.test(text) &&
          !isMarkup(text) && !known.has(norm(text)) && !candidates.includes(text)) {
        candidates.push(text);
      }
    };
    addCandidate(directText(node));
    try {
      for (const descendant of Array.from(node?.querySelectorAll?.('*') || [])) {
        if (isNoscript(descendant) || descendant.closest?.('noscript')) continue;
        addCandidate(directText(descendant));
        if (!(descendant.children || []).length) addCandidate(visibleText(descendant));
      }
    } catch (_) {}
    addCandidate(visibleText(node));
    const score = text => {
      const folded = text.toLowerCase();
      let value = text.length;
      if (/privacy issues|detection\s*science|sign in|click here|explore ip2proxy|unexpand/.test(folded)) value += 1000;
      for (const alias of known) {
        if (folded.includes(alias + ':') || folded.includes(alias + '：')) value += 500;
      }
      return value;
    };
    candidates.sort((left, right) => score(left) - score(right));
    return candidates[0] || '';
  };
  const inferValue = labelElement => {
    if (/[-—]\s*$/.test(visibleText(labelElement))) return '';
    for (let sibling = labelElement.nextElementSibling, count = 0;
         sibling && count < 3; sibling = sibling.nextElementSibling, count++) {
      const value = candidateText(sibling);
      if (value) return value;
    }
    let branch = labelElement;
    for (let depth = 0; branch?.parentElement && depth < 4; depth++) {
      const parent = branch.parentElement;
      const children = Array.from(parent.children || []);
      const branchIndex = children.findIndex(child => child === branch || child.contains(branch));
      if (branchIndex >= 0) {
        for (let index = branchIndex + 1; index < children.length; index++) {
          const value = candidateText(children[index]);
          if (value) return value;
        }
        if (children.length >= 2) return '';
      }
      branch = parent;
    }
    return '';
  };

  let challenge = false;
  const challengeParts = [String(document.title || '')];
  for (const {root, all} of snapshots) {
    let query = selector => [];
    try { query = selector => Array.from(root.querySelectorAll(selector)); } catch (_) {}
    for (const row of query('tr').slice(0, 1200)) {
      const cells = Array.from(row.querySelectorAll(':scope > th, :scope > td'));
      if (cells.length >= 2) push(visibleText(cells[0]), candidateText(cells[cells.length - 1]));
    }
    for (const dt of query('dt').slice(0, 600)) {
      const dd = dt.nextElementSibling;
      if (dd && String(dd.tagName).toLowerCase() === 'dd') push(visibleText(dt), candidateText(dd));
    }
    for (const element of all) {
      const text = visibleText(element);
      if (known.has(norm(text))) push(text, inferValue(element));
      if (text && text.length <= 400) {
        const split = text.match(/^(.{1,80}?)[\s]*[:：][\s]*(.{1,300})$/);
        if (split) push(split[1], split[2]);
      }
    }
    for (const row of query('[class*=row], [class*=item], [class*=result], [class*=field], [class*=card]').slice(0, 1600)) {
      const children = Array.from(row.children || []).filter(child => visibleText(child));
      if (children.length >= 2) push(visibleText(children[0]), candidateText(children[children.length - 1]));
    }
    const bodyText = compact(root.body?.innerText || root.host?.innerText || '').slice(0, 1600);
    if (bodyText) challengeParts.push(bodyText);
    for (const frame of query('iframe[src*="challenges.cloudflare.com"], iframe[src*="/cdn-cgi/challenge-platform/"]')) {
      try {
        const rect = frame.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) challenge = true;
      } catch (_) {}
    }
    for (const marker of query('.cf-turnstile, #challenge-stage, [class*=challenge-stage]')) {
      try {
        const rect = marker.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) challenge = true;
      } catch (_) {}
    }
  }
  // 盾里有没有"真正能点的组件"：Turnstile 一定挂在 iframe 或 shadow root 里。
  // 没有它就是非交互式挑战，点了也白点。
  let widget = false;
  try {
    for (const frame of document.querySelectorAll(
        'iframe[src*="challenges.cloudflare.com"], iframe[title*="challenge"], iframe[title*="Cloudflare"]')) {
      const rect = frame.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) { widget = true; break; }
    }
    if (!widget) {
      for (const node of document.querySelectorAll('.cf-turnstile, #turnstile-wrapper, div[id^="cf-chl-widget"]')) {
        const rect = node.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) { widget = true; break; }
      }
    }
    if (!widget) {
      for (const node of Array.from(document.querySelectorAll('*')).slice(0, 3000)) {
        if (node.shadowRoot) { widget = true; break; }
      }
    }
  } catch (_) {}
  const challengeText = challengeParts.join(' ').toLowerCase();
  challenge = challenge || /just a moment|checking your browser|verify you are human|security verification|performing security verification|请稍候|正在验证|安全验证|验证您是人类/.test(challengeText);

  // 结果页就绪标记（如 IP2Proxy 的 div h2 = "IP Address"）：出现即说明盾已放行。
  const marker = __MARKER__;
  const needle = String(marker && marker.contains || '').toLowerCase();
  let ready = false;
  if (needle && marker.selector) {
    for (const {root} of snapshots) {
      let nodes = [];
      try { nodes = Array.from(root.querySelectorAll(marker.selector)).slice(0, 60); } catch (_) {}
      for (const node of nodes) {
        if (visibleText(node).toLowerCase().includes(needle)) { ready = true; break; }
      }
      if (ready) break;
    }
  }
  // 整页内容指纹：用来判断"页面还在往里填东西"还是"真的跑完了"。
  // 时:分:秒 先换成 TIME——leakdig / browserleaks 上都有每秒跳的时钟，
  // 不归一化的话页面永远"在变"，等到天荒地老也不会静默。
  const pageText = compact(document.body?.innerText || '')
      .replace(/\d{1,2}:\d{2}(:\d{2})?/g, 'TIME');
  // 页面自己写的"检测中"还剩几个——用来判断这一站是不是真跑完了
  let busy = 0;
  for (const marker of __BUSY__) {
    if (!marker) continue;
    busy += pageText.split(marker).length - 1;
  }
  let digest = 2166136261;
  for (let i = 0; i < pageText.length; i++) {
    digest ^= pageText.charCodeAt(i);
    digest = Math.imul(digest, 16777619);
  }
  return {ready_state: String(document.readyState || ''), challenge, ready, pairs,
          busy, widget, text_len: pageText.length, text_hash: digest >>> 0};
})()
"""


_CREEPJS_DOM_SCRIPT = r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const fp = text.match(/FP ID:\s*([a-f0-9]{64})\b/i);
  const fuzzy = text.match(/Fuzzy:\s*([a-f0-9]{64})\b/i);
  const ready = document.readyState === 'complete' && fp && fuzzy
    && !/^0+$/.test(fuzzy[1]) && !text.includes('Computing...')
    && text.includes('Navigator') && text.includes('WebGL') && text.length > 1500;
  let digest = 2166136261;
  for (let i = 0; i < text.length; i++) {
    digest ^= text.charCodeAt(i);
    digest = Math.imul(digest, 16777619);
  }
  return {ready_state: document.readyState, challenge: false, ready: !!ready,
          pairs: ready ? [['FP ID', fp[1]], ['Fuzzy', fuzzy[1]]] : [],
          busy: ready ? 0 : 1, text_len: text.length, text_hash: digest >>> 0};
})()
"""


def _empty_check(url: str = "", *, status: str = "manual", error: str = "") -> dict:
    return {"status": status, "fields": {}, "url": str(url)[:2000],
            "error": str(error)[:MAX_ERROR_CHARS], "page": ""}


def _request_port(request: dict) -> int:
    address = str(request.get("debug_address", ""))
    host, separator, raw_port = address.rpartition(":")
    if not separator or host != "127.0.0.1" or not raw_port.isdigit():
        raise ValueError("debug_address 必须是 127.0.0.1:<port>")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError("debug_port 超出范围")
    return port


def _browser_is_headless(port: int) -> bool:
    """让 DP 选项匹配现有浏览器，避免其因 headless 模式不一致触发内部重连。"""
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
        data = json.loads(response.read(64 * 1024).decode("utf-8"))
    return "headless" in str(data.get("User-Agent", "")).casefold()


def _site_for_url(url: str, allowed_sites: set[str]) -> str:
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except Exception:
        return ""
    for site in allowed_sites:
        if host in SITE_HOSTS[site]:
            if site == "creepjs" and not urlsplit(url).path.startswith("/creepjs/"):
                continue
            return site
    return ""


def _challenge_element(tab: Any) -> Any:
    """找到当前可见的五秒盾承载元素；找不到返回 None，不抛出。"""
    for selector in _CHALLENGE_HOSTS:
        try:
            element = tab.ele(selector, timeout=0.3)
        except Exception:
            continue
        if not element:
            continue
        try:
            if element.states.is_displayed:
                return element
        except Exception:
            continue
    return None


def _challenge_frame(tab: Any, element: Any) -> Any:
    """穿透 shadow root 拿到 Turnstile 的 iframe 帧；拿不到返回 None。"""
    getters = (
        lambda: tab.get_frame(element.shadow_root.ele("tag:iframe", timeout=0.3), timeout=0.5),
        lambda: tab.get_frame("css:iframe[src*='challenges.cloudflare.com']", timeout=0.5),
    )
    for getter in getters:
        try:
            frame = getter()
        except Exception:
            continue
        if frame:
            return frame
    return None


_CHECKBOX_PROBE_JS = r"""
(() => {
  // 盾页上真正可点的那个小方块：可见、尺寸像复选框/小组件、在页面上半部分。
  // 有的变体没有任何 id/class，只能靠几何特征找。
  const out = [];
  for (const el of Array.from(document.querySelectorAll('div,span,label,input,iframe')).slice(0, 4000)) {
    const r = el.getBoundingClientRect();
    if (r.width < 12 || r.height < 12 || r.width > 420 || r.height > 220) continue;
    if (r.top < 0 || r.left < 0) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
    out.push({x: Math.round(r.left), y: Math.round(r.top),
              w: Math.round(r.width), h: Math.round(r.height),
              tag: el.tagName});
  }
  out.sort((a, b) => (a.y - b.y) || (a.x - b.x));
  return JSON.stringify(out.slice(0, 6));
})()
"""


def _press_by_geometry(tab: Any, offset: int) -> bool:
    """没有可用选择器时，按几何位置去点盾里那个小方块（只按下/松开，不 click）。"""
    try:
        boxes = json.loads(tab.run_js(_CHECKBOX_PROBE_JS, as_expr=True, timeout=5))
    except Exception:
        return False
    for box in boxes or []:
        try:
            target_x = int(box["x"]) + min(int(offset), max(2, int(box["w"]) - 2))
            target_y = int(box["y"]) + min(int(offset), max(2, int(box["h"]) - 2))
            tab.actions.move_to((target_x, target_y), duration=1).hold().wait(0.3).release()
            return True
        except Exception:
            continue
    return False


def _press_challenge(tab: Any, offset: int) -> tuple[bool, bool]:
    """按盾：返回 ``(有没有找到承载元素, 有没有真的按下去)``。

    两个值分开报，是为了在失败时能说清到底是"找不到东西点"还是"点了也没用"——
    实测遇到过整页只有 main-wrapper/main-content 的变体，原来的选择器一个都匹配不到。
    """
    element = _challenge_element(tab)
    if element is None:
        # 这个盾变体没有任何可识别的承载元素，退到按几何位置点
        return False, _press_by_geometry(tab, offset)
    pressed = False
    try:
        tab.actions.move_to(element, offset, offset, duration=1).hold().wait(0.3).release()
        pressed = True
    except Exception:
        pass
    try:
        frame = _challenge_frame(tab, element)
        if frame is not None:
            frame.actions.move(offset, offset, duration=1).hold().wait(0.3).release()
            pressed = True
    except Exception:
        pass
    return True, pressed


def _read_tab(tab: Any, site: str, timeout_seconds: float) -> dict:
    script = _DOM_SCRIPT.replace("__ALIASES__", json.dumps(all_aliases(site), ensure_ascii=False))
    script = script.replace(
        "__MARKER__", json.dumps(SITE_READY_MARKERS.get(site) or {}, ensure_ascii=False)
    )
    script = script.replace(
        "__BUSY__", json.dumps(list(SITE_BUSY_MARKERS.get(site, ())), ensure_ascii=False)
    )
    if site == "creepjs":
        script = _CREEPJS_DOM_SCRIPT
    busy_tolerance = SITE_BUSY_TOLERANCE.get(site, 0)
    started_at = time.monotonic()
    available = max(0.5, timeout_seconds)
    hard_deadline = started_at + available
    normal_deadline = min(hard_deadline, started_at + NORMAL_RESULT_WAIT_SECONDS)
    challenge_deadline = min(hard_deadline, started_at + CHALLENGE_AUTO_PASS_SECONDS)
    attempts_left = CHALLENGE_CLICK_ATTEMPTS
    offset = _FIRST_CLICK_OFFSET
    clicks = 0
    last_value: dict = {}
    fields: dict[str, Any] = {}
    conversion_failed = False
    stable_state: tuple | None = None
    stable_since = 0.0
    stable_complete = False
    busy_left = 0
    refreshes = 0
    js_errors = 0
    js_last_error = ""
    challenge_reloads = 0
    challenge_reload_at = 0.0
    saw_widget = False
    press_attempts = 0
    press_found = 0
    bailed_empty = False
    waived: set[str] = set()
    grace = SITE_REQUIRED_GRACE.get(site, {})
    refresh_after = SITE_REFRESH_AFTER.get(site, 0.0)
    refresh_at = started_at + refresh_after if refresh_after else 0.0
    blocked = False
    while True:
        now = time.monotonic()
        active_deadline = challenge_deadline if blocked else normal_deadline
        remaining = active_deadline - now
        if remaining <= 0:
            break
        try:
            value = tab.run_js(script, as_expr=True,
                               timeout=min(JS_CALL_TIMEOUT, max(0.05, remaining)))
            js_errors = 0
        except Exception as exc:
            # 并发下 CDP 偶尔回不过来，重试几次再放弃——别让一次卡顿废掉整站
            js_errors += 1
            js_last_error = str(exc).splitlines()[0][:120]
            if js_errors >= JS_MAX_CONSECUTIVE_ERRORS:
                break
            time.sleep(min(1.0, max(0.0, active_deadline - time.monotonic())))
            continue
        last_value = value if isinstance(value, dict) else {}
        pairs = last_value.get("pairs")
        fields, conversion_failed = normalize_candidates(site, pairs if isinstance(pairs, list) else [])
        was_blocked = blocked
        # 结果页就绪标记出现即视为已放行，避免残留盾文案把已出结果的页面判成挑战页。
        blocked = bool(last_value.get("challenge")) and not bool(last_value.get("ready"))
        if was_blocked and not blocked:
            # 盾刚放行，结果还要重新渲染：重新给足正常读取预算（不超过本站硬上限）。
            normal_deadline = min(hard_deadline, time.monotonic() + NORMAL_RESULT_WAIT_SECONDS)
        base_required, any_of = SITE_MINIMUMS[site]
        # 某些必需字段等超时就不再要求（如 BrowserScan 的 DNS），否则一项能拖死整站
        elapsed = time.monotonic() - started_at
        waived = {field for field, limit in grace.items()
                  if field not in fields and elapsed >= limit}
        required = base_required - waived
        minimum_met = required.issubset(fields) and bool(any_of.intersection(fields))
        # 页面上还有几项写着"检测中"：在预算内就继续等，超了就不再为它干等。
        busy_left = int(last_value.get("busy") or 0)
        still_busy = (busy_left > busy_tolerance
                      and time.monotonic() - started_at < BUSY_WAIT_SECONDS)
        # 页面自己的检测（如 BrowserScan 的 DNS 那行）没跑完时，值还是占位文本、
        # 解析不出来。这时不能算完成——等它把真值算出来再存，超时才退而求其次。
        # 整页文本 + 白名单字段都不再变，才算这一站真的跑完了。
        state = (last_value.get("text_hash"), last_value.get("text_len"),
                 tuple(sorted((key, str(item)) for key, item in fields.items())))
        settled_at = time.monotonic()
        if not blocked and minimum_met and not conversion_failed and not still_busy:
            if state != stable_state:
                stable_state = state
                stable_since = settled_at
            elif settled_at - stable_since >= PAGE_QUIET_SECONDS:
                stable_complete = True
                break
        else:
            stable_state = None
            stable_since = 0.0
        # 等够了还是没算出来 → 原地刷新一次（只 reload 当前页，不跳别的地址）
        if (refresh_at and not blocked and not stable_complete and refreshes < SITE_MAX_REFRESH
                and time.monotonic() >= refresh_at):
            refreshes += 1
            refresh_at = 0.0
            try:
                tab.run_cdp("Page.reload", ignoreCache=True)
                print(f"{site} 等 {refresh_after:.0f} 秒仍未出结果，已刷新一次", file=sys.stderr)
            except Exception as exc:
                print(f"{site} 刷新失败: {exc}", file=sys.stderr)
            # 刷完等于从头开始渲染，重新给足读取预算
            normal_deadline = min(hard_deadline, time.monotonic() + NORMAL_RESULT_WAIT_SECONDS)
            stable_state = None
            stable_since = 0.0
            time.sleep(min(2.0, max(0.0, normal_deadline - time.monotonic())))
            continue
        # 没有盾、也没有任何内容 → 页面根本没加载起来，早退好过白等
        if (not blocked and not fields and not last_value.get("ready")
                and int(last_value.get("text_len") or 0) < 40
                and time.monotonic() - started_at >= EMPTY_PAGE_BAIL_SECONDS):
            bailed_empty = True
            break
        if blocked and time.monotonic() < challenge_deadline:
            has_widget = bool(last_value.get("widget"))
            saw_widget = saw_widget or has_widget
            if has_widget and attempts_left > 0:
                # 交互式 Turnstile：按下—停顿—松开
                press_attempts += 1
                found, pressed = _press_challenge(tab, offset)
                press_found += 1 if found else 0
                if pressed:
                    clicks += 1
                    offset = offset + 5 if offset < _MAX_CLICK_OFFSET else _RESET_CLICK_OFFSET
                attempts_left -= 1
            elif not has_widget:
                # 非交互式挑战：没东西可点，只能等；等够了就换一次挑战
                if not challenge_reload_at:
                    challenge_reload_at = time.monotonic() + CHALLENGE_RELOAD_AFTER
                elif (time.monotonic() >= challenge_reload_at
                        and challenge_reloads < CHALLENGE_MAX_RELOAD):
                    challenge_reloads += 1
                    challenge_reload_at = time.monotonic() + CHALLENGE_RELOAD_AFTER
                    try:
                        tab.run_cdp("Page.reload", ignoreCache=True)
                        print(f"{site} 非交互式盾等 {CHALLENGE_RELOAD_AFTER:.0f} 秒未放行，"
                              f"刷新第 {challenge_reloads} 次", file=sys.stderr)
                    except Exception as exc:
                        print(f"{site} 盾页刷新失败: {exc}", file=sys.stderr)
            settle = min(CHALLENGE_SETTLE_SECONDS, challenge_deadline - time.monotonic())
            if settle > 0:
                time.sleep(settle)
            continue
        now = time.monotonic()
        active_deadline = challenge_deadline if blocked else normal_deadline
        if now >= active_deadline:
            break
        time.sleep(min(0.5, active_deadline - now))
    url = str(getattr(tab, "url", ""))[:2000]
    if blocked:
        # 盾没过就如实记失败，不再要求人工，也**绝不把标签页顶到前台**——
        # 用户可能正在这个浏览器里做别的事，抢焦点纯属打扰（早期"回落人工"时代的遗留）。
        if saw_widget:
            detail = (f"（交互式盾：点了 {clicks} 次 / 尝试 {press_attempts} 轮，"
                      f"{press_found} 轮找到可点元素）")
        else:
            detail = (f"（非交互式盾：整页没有可点组件，已刷新 {challenge_reloads} 次；"
                      "这种盾靠 IP 信誉与浏览器环境自动放行，点击无效）")
        blocked_result = _empty_check(url, status="failed", error=_CHALLENGE_ERROR.format(
            clicks=clicks, seconds=time.monotonic() - started_at) + detail)
        # 私有标记：调用方据此跳过存档。盾页上除了 "Just a moment" 什么都没有，存了也没用。
        blocked_result["_blocked"] = True
        return blocked_result
    if js_errors >= JS_MAX_CONSECUTIVE_ERRORS and not fields:
        return _empty_check(url, status="failed", error=(
            f"连续 {js_errors} 次读页面失败（浏览器/CDP 没响应）：{js_last_error}"))
    if bailed_empty:
        return _empty_check(url, status="failed", error=(
            f"页面在 {EMPTY_PAGE_BAIL_SECONDS:.0f} 秒内没有渲染出任何内容（代理多半不通）"))
    if not fields:
        return _empty_check(url, status="failed", error="一次性采集超时，未识别到标准白名单字段")
    if not stable_complete:
        base_required, any_of = SITE_MINIMUMS[site]
        minimum_met = ((base_required - waived).issubset(fields)
                       and bool(any_of.intersection(fields)))
        if minimum_met and conversion_failed:
            error = "页面部分检测项在时限内仍未算出真实值"
        elif minimum_met:
            error = f"字段已出现，但页面在时限内一直有变化（要求静默 {PAGE_QUIET_SECONDS:.0f} 秒）"
        else:
            error = "一次性采集超时，字段未达到站点最低完成条件"
        return {"status": "partial", "fields": fields, "url": url, "error": error, "page": ""}
    status = result_status(site, fields, conversion_failed, waived=waived)
    error = ""
    if conversion_failed:
        error = "部分页面值无法转换为标准类型"
    for field in sorted(waived):
        missed = f"{field} 等满 {grace.get(field, 0.0):.0f} 秒仍未出结果，已按超时放行"
        error = f"{error}；{missed}" if error else missed
    if busy_left > busy_tolerance:
        note = f"页面上仍有 {busy_left} 项显示检测中（已等满 {BUSY_WAIT_SECONDS:.0f} 秒）"
        if refreshes:
            note += f"，中途已刷新 {refreshes} 次"
        error = f"{error}；{note}" if error else note
    return {"status": status, "fields": fields, "url": url, "error": error, "page": ""}


def _write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    os.replace(temporary, path)


def _write_data_json(page_dir: str, extracted: dict, checks: dict) -> None:
    """把各站抽出来的键值写成 data.json，跟 HTML 放同一个目录。

    这是**附带品**：抽不全不影响存档，HTML 才是主证据。
    """
    if not page_dir or not extracted:
        return
    payload = {
        "captured_at": datetime.now(timezone.utc)
                       .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "sites": {
            site: {
                "title": data.get("title", ""),
                "status": (checks.get(site) or {}).get("status", ""),
                "url": (checks.get(site) or {}).get("url", ""),
                "fields": (checks.get(site) or {}).get("fields", {}),
                "items": data.get("items", []),
            }
            for site, data in sorted(extracted.items())
        },
    }
    try:
        _write_json_atomic(os.path.join(page_dir, "data.json"), payload)
    except Exception as exc:
        print(f"data.json 写入失败: {exc}", file=sys.stderr)


def collect_once(request: dict) -> dict:
    """执行一次 attach/并行采集/断开；绝不调用 browser.quit()。"""
    port = _request_port(request)
    requested_sites = request.get("sites")
    allowed_sites = {
        str(site) for site in (requested_sites if isinstance(requested_sites, list) else SITE_SCHEMAS)
        if str(site) in SITE_SCHEMAS
    }
    if not allowed_sites:
        raise ValueError("sites 不能为空")
    deadline_ms = int(request.get("deadline_ms", 20_000))
    # 上限放宽到 240 秒：四站并行，最慢一站要等 BrowserScan 的 DNS（整页最后一项）+ 可能的过盾。
    deadline_ms = max(1_000, min(deadline_ms, 240_000))
    overall_deadline = time.monotonic() + deadline_ms / 1000
    close_completed = bool(request.get("close_completed"))
    page_dir = str(request.get("page_dir", ""))
    assets_dir = str(request.get("assets_dir", ""))
    raw_urls = request.get("urls")
    urls: dict[str, Any] = raw_urls if isinstance(raw_urls, dict) else {}
    checks = {site: _empty_check(str(urls.get(site, ""))) for site in allowed_sites}
    closed_sites: list[str] = []
    extracted: dict[str, dict] = {}
    tabs: list[Any] = []
    closed_tabs: set[int] = set()
    attached = False
    try:
        # 此 import 只能存在于一次性 worker，避免 DP 进入 Qt/常驻监控进程。
        from DrissionPage import Chromium, ChromiumOptions
        from DrissionPage.common import Settings

        Settings.set_browser_connect_timeout(8)
        # CDP 级超时也要放宽：并发多个窗口时 5 秒经常不够（实测 3 窗并发整轮全废）
        Settings.set_cdp_timeout(30)
        options = ChromiumOptions(read_file=False).set_local_port(port).existing_only()
        options.headless(_browser_is_headless(port))
        browser = Chromium(options)
        attached = True
        tabs = list(browser.get_tabs())
        selected: dict[str, Any] = {}
        for tab in tabs:
            try:
                site = _site_for_url(str(getattr(tab, "url", "")), allowed_sites)
            except Exception:
                site = ""
            if site and site not in selected:
                selected[site] = tab
        for site in allowed_sites - set(selected):
            checks[site] = _empty_check(
                str(urls.get(site, "")), status="manual", error="未找到已打开的检测页"
            )
        # 关标签页要守住"至少留一个"，多线程同时收尾时用锁把计数改成原子的。
        guard = threading.Lock()
        remaining_tabs = [len(tabs)]

        def close_when_done(site: str, tab: Any, result: dict) -> None:
            """静态存档成功才关页；只剩检测页时先补空白页，保留浏览器窗口。"""
            if not close_completed or not result.get("page"):
                return
            with guard:
                if remaining_tabs[0] - 1 < MIN_TABS_LEFT_OPEN:
                    try:
                        blank = tab.run_cdp("Target.createTarget", url="about:blank")
                        if not blank.get("targetId"):
                            return
                        remaining_tabs[0] += 1
                    except Exception as exc:
                        print(f"保留空白标签页失败: {exc}", file=sys.stderr)
                        return
                remaining_tabs[0] -= 1
            try:
                tab.close()
            except Exception as exc:
                with guard:
                    remaining_tabs[0] += 1
                print(f"关闭 {site} 标签页失败: {exc}", file=sys.stderr)
                return
            closed_tabs.add(id(tab))
            closed_sites.append(site)

        def run_site(site: str) -> tuple[str, dict]:
            tab = selected[site]
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                return site, _empty_check(
                    str(getattr(tab, "url", urls.get(site, ""))), status="failed",
                    error="一次性采集超时",
                )
            try:
                result = _read_tab(tab, site, remaining)
            except Exception as exc:
                result = _empty_check(
                    str(getattr(tab, "url", urls.get(site, ""))), status="failed", error=str(exc)
                )
                if site == "creepjs":
                    return site, result
                archived = capture_page(tab, page_dir, assets_dir, site)
                result["page"] = archived["page"]
                with guard:
                    extracted[site] = archived
                return site, result
            if site == "creepjs" and result.get("status") != "complete":
                # 未跑完的占位页不能当作检测结果存档，更不能关闭。
                return site, result
            if result.pop("_blocked", False):
                # 盾页不存档：整页只有 "Performing security verification"，留一份没有意义
                result["page"] = ""
                print(f"{site} 停在盾页，按要求不保存存档", file=sys.stderr)
            else:
                # 存档必须赶在关标签页之前
                archived = capture_page(tab, page_dir, assets_dir, site)
                result["page"] = archived["page"]
                with guard:
                    extracted[site] = archived
                if page_dir and assets_dir and not result["page"]:
                    result["status"] = "failed"
                    result["error"] = "静态页面或本地 CSS 保存失败，标签页已保留"
            close_when_done(site, tab, result)
            return site, result

        if selected:
            # 每个标签页有独立的 CDP 连接，四站各测各的；总耗时取最慢的一站而不是相加。
            with ThreadPoolExecutor(max_workers=len(selected)) as pool:
                futures = [pool.submit(run_site, site) for site in sorted(selected)]
                for future in as_completed(futures):
                    site, result = future.result()
                    checks[site] = result
                    with guard:
                        _write_data_json(page_dir, dict(extracted), checks)
                    # 每个站点完成即通知父进程；文件原子落盘，父端不会读到半截 JSON。
                    progress_dir = str(request.get("progress_dir", ""))
                    if progress_dir:
                        progress = {
                            "protocol_version": PROTOCOL_VERSION,
                            "request_id": str(request.get("request_id", "")),
                            "environment_id": str(request.get("environment_id", "")),
                            "profile_id": str(request.get("profile_id", "")),
                            "ok": True, "checks": checks,
                            "completed_site": site, "closed_tabs": list(closed_sites),
                        }
                        _write_json_atomic(os.path.join(progress_dir, site + ".json"), progress)
        _write_data_json(page_dir, extracted, checks)
    finally:
        # disconnect 只断开 DP 与标签页的连接；已关闭的标签页不再触碰。
        for tab in tabs:
            if id(tab) in closed_tabs:
                continue
            try:
                tab.disconnect()
            except Exception:
                pass
    human_sites = [site for site, item in checks.items() if item.get("status") == "manual" and str(item.get("error", "")).startswith("human_required:")]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": str(request.get("request_id", "")),
        "environment_id": str(request.get("environment_id", "")),
        "profile_id": str(request.get("profile_id", "")),
        "ok": True,
        "attached": attached,
        "checks": checks,
        "human_required": bool(human_sites),
        "human_sites": human_sites,
        "closed_tabs": sorted(closed_sites),
        "error": "",
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("worker 请求超过 64KB")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict) or request.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("worker 协议无效")
        # 第三方库偶发打印信息时转到 stderr，stdout 只保留一条协议 JSON。
        with contextlib.redirect_stdout(sys.stderr):
            response = collect_once(request)
    except Exception as exc:
        response = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "",
            "ok": False,
            "attached": False,
            "checks": {},
            "human_required": False,
            "human_sites": [],
            "closed_tabs": [],
            "error": str(exc)[:MAX_ERROR_CHARS],
        }
    payload = (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return 0 if response.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
