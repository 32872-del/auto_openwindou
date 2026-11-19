"""把检测页静态化存档：主证据是渲染完的原页面，附带一份抽取出来的 JSON。

为什么这么设计（都是踩出来的）：
- **存档不能依赖抽取器**。站点一改版，逐站抽取规则就废，证据跟着丢。所以主证据是
  「渲染完的 DOM 原样静态化」——站点以后怎么改都不影响已经存下来的档。
- **脚本必须全删**。留着的话，打开存档时页面会用**打开者当时的网络**重新检测一遍，
  把采集那一刻的值覆盖掉。删掉后值就是烘死在 DOM 里的静态文本。
- **站点自己的 class + CSS 原样保留**，所以红/绿/黄的状态配色天然就在，不用去猜配色规则。
- **CSS 存进共享目录**（按 URL 的 sha1 命名），多次采集、多个站点共用；这些站的 CSS 基本
  不变，只有第一次真下载。
- 下载走**页面自己的 fetch**，即走该窗口的代理与会话，跟浏览器看到的是同一份。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit

ASSET_EXTENSION = ".css"          # 外部资源只存 CSS；CreepJS WebGL 检测图内嵌 PNG
MAX_ASSET_BYTES = 3 * 1024 * 1024
MAX_ASSETS_PER_PAGE = 60
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_JSON_ITEMS = 400              # 附带 JSON 每站最多这么多项
PAGE_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}\.html$")
ASSET_NAME_RE = re.compile(r"^[0-9a-f]{16}\.css$")

# 页面里跑：静态化 DOM + 顺手抽一份键值（含 state 语义）
COLLECT_JS = r"""
(() => {
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
  const abs = u => { try { return new URL(u, location.href).href } catch (e) { return '' } };

  // ── 1) 抽键值（附带品；抽不全不影响存档）──────────────────────────────
  const NOISE_LABEL = /^(home|demo|resources?|pricing|contact|blog|login|sign in|products?|api|docs?|工具|博客|登录|注册|深色|浅色)$/i;
  const CODE_LIKE = /[{}\[\]]|^["',:]+$|^\s*"[\w_]+"\s*$/;
  const state = el => {
    if (!el) return 'info';
    var cls = String(el.className || '').toLowerCase();
    if (/danger|error|fail|leak|bad|red|risk-high/.test(cls)) return 'bad';
    if (/warn|caution|orange|yellow|medium/.test(cls)) return 'warn';
    if (/success|ok|safe|pass|green|good|low/.test(cls)) return 'ok';
    var cs = getComputedStyle(el);
    var pick = function (raw) {
      var m = String(raw).match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
      if (!m) return null;
      var r = +m[1], g = +m[2], b = +m[3];
      if (r > 150 && g < 110 && b < 110) return 'bad';
      if (r > 175 && g > 130 && b < 110) return 'warn';
      if (g > 120 && r < 130 && b < 140) return 'ok';
      return null;
    };
    return pick(cs.color) || pick(cs.backgroundColor) || 'info';
  };
  // 导航栏/页脚里全是「菜单项 + 下一个菜单项」，会被当成键值对，先排掉
  const CHROME_SEL = 'nav,header,footer,aside,menu,[role=navigation],[role=banner],[role=contentinfo]';
  const chrome = Array.from(document.querySelectorAll(CHROME_SEL));
  const inChrome = el => !!el && chrome.some(box => box.contains(el));
  const items = [];
  const seen = new Set();
  const isMarkup = text => /<[a-z!/][^>]*>/i.test(text);
  const push = (label, value, el) => {
    label = norm(label).replace(/[:：]\s*$/, '');
    value = norm(value);
    if (!label || !value || label === value) return;
    if (inChrome(el)) return;
    if (label.length > 60 || value.length > 400) return;
    if (NOISE_LABEL.test(label) || NOISE_LABEL.test(value)) return;
    if (CODE_LIKE.test(label) || CODE_LIKE.test(value)) return;
    // <noscript> 里的内容在 JS 开着时是纯文本，textContent 会把整段标签源码交出来
    if (isMarkup(label) || isMarkup(value)) return;
    if (el && el.closest && el.closest('noscript')) return;
    const key = label.toLowerCase() + '\u0000' + value;
    if (seen.has(key) || items.length >= __MAX_ITEMS__) return;
    seen.add(key);
    items.push({label: label, value: value, state: state(el)});
  };
  const direct = node => norm(Array.from(node.childNodes || [])
      .filter(n => n.nodeType === 3).map(n => n.nodeValue || '').join(' '));

  document.querySelectorAll('tr').forEach(tr => {
    const cells = tr.querySelectorAll(':scope > th, :scope > td');
    if (cells.length >= 2) push(norm(cells[0].innerText), norm(cells[cells.length - 1].innerText),
                                cells[cells.length - 1]);
  });
  document.querySelectorAll('dt').forEach(dt => {
    const dd = dt.nextElementSibling;
    if (dd && dd.tagName === 'DD') push(norm(dt.innerText), norm(dd.innerText), dd);
  });
  document.querySelectorAll('div,span,td,li,p,h3,h4,label,strong,b').forEach(el => {
    if (el.children.length > 1) return;
    const label = direct(el) || norm(el.innerText);
    if (!label || label.length > 60) return;
    const sib = el.nextElementSibling;
    if (!sib || sib.children.length > 2) return;
    push(label, norm(sib.innerText), sib);
  });
  document.querySelectorAll('div,span,td,li,p').forEach(el => {
    if (el.children.length) return;
    const text = norm(el.innerText);
    const m = text.match(/^(.{1,60}?)\s*[:：]\s*(.{1,300})$/);
    if (m) push(m[1], m[2], el);
  });

  // ── 2) 静态化 DOM（主证据）────────────────────────────────────────
  const doc = document.documentElement.cloneNode(true);
  const preservedImages = new Set();
  if (__SITE__ === 'creepjs') {
    // 原站已将本次 WebGL 渲染结果转成 PNG；只保留该图，不重新绘制或运行检测。
    const originals = Array.from(document.querySelectorAll('img.gl-image'));
    const copies = Array.from(doc.querySelectorAll('img.gl-image'));
    originals.forEach((original, index) => {
      const source = original.currentSrc || original.src || '';
      if (!original.complete || !original.naturalWidth || !original.naturalHeight ||
          !/^data:image\/png;base64,[a-z0-9+/]+=*$/i.test(source) ||
          source.length > 4 * 1024 * 1024 || !copies[index]) {
        throw new Error('CreepJS WebGL 检测图尚未加载完成或不是可内嵌的 PNG，保留页面重试');
      }
      const copy = copies[index];
      copy.setAttribute('src', source);
      copy.removeAttribute('srcset');
      copy.removeAttribute('sizes');
      copy.removeAttribute('loading');
      preservedImages.add(copy);
    });
  }
  const assets = [];
  doc.querySelectorAll('link').forEach(el => {
    const rel = (el.getAttribute('rel') || '').toLowerCase();
    const u = abs(el.getAttribute('href') || '');
    if (rel.includes('stylesheet') && /\.css(?:[?#]|$)/i.test(u)) {
      el.setAttribute('href', u);
      el.removeAttribute('integrity');
      el.removeAttribute('crossorigin');
      assets.push(u);
    } else {
      el.remove();
    }
  });
  // 脚本全删：留着的话打开存档会用打开者的网络重新检测，把记录覆盖掉
  doc.querySelectorAll('script,noscript').forEach(el => el.remove());
  doc.querySelectorAll('*').forEach(el => {
    for (const attr of Array.from(el.attributes || [])) {
      if (attr.name.toLowerCase().startsWith('on')) el.removeAttribute(attr.name);
    }
  });
  doc.querySelectorAll('img,picture,source,svg,canvas,video,audio,object,embed,iframe')
     .forEach(el => { if (!preservedImages.has(el)) el.remove(); });

  return JSON.stringify({html: '<!doctype html>' + doc.outerHTML, assets: assets,
                         items: items, title: norm(document.title)});
})()
"""

FETCH_JS = r"""
(async () => {
  const urls = __URLS__;
  const out = {};
  for (const u of urls) {
    try {
      const response = await fetch(u, {credentials: 'omit'});
      if (!response.ok) { out[u] = ''; continue; }
      const buffer = await response.arrayBuffer();
      if (buffer.byteLength > __MAX__) { out[u] = ''; continue; }
      const bytes = new Uint8Array(buffer);
      let binary = '';
      for (let i = 0; i < bytes.length; i += 8192) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
      }
      out[u] = btoa(binary);
    } catch (e) { out[u] = ''; }
  }
  return JSON.stringify(out);
})()
"""


def asset_name(url: str) -> str:
    """CSS 按 URL 的 sha1 命名，多次采集自然复用同一份。"""
    if not urlsplit(url).path.lower().endswith(ASSET_EXTENSION):
        return ""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ASSET_EXTENSION


def _write_bytes_atomic(path: str, raw: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".archive-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _download(tab: Any, urls: list[str], folder: str, names: dict[str, str]) -> int:
    if not urls:
        return 0
    script = FETCH_JS.replace("__URLS__", json.dumps(urls)).replace("__MAX__", str(MAX_ASSET_BYTES))
    payload = json.loads(tab.run_js(script, as_expr=True, timeout=120))
    saved = 0
    for url, encoded in payload.items():
        name = names.get(url)
        if not name or not encoded:
            continue
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            continue
        if not raw or len(raw) > MAX_ASSET_BYTES:
            continue
        _write_bytes_atomic(os.path.join(folder, name), raw)
        saved += 1
    return saved


def capture_page(tab: Any, page_dir: str, assets_dir: str, site: str) -> dict:
    """静态化该站页面并抽一份键值。

    返回 ``{"page": 文件名, "items": [{label, value, state}], "title": 页面标题}``。
    """
    empty = {"page": "", "items": [], "title": "", "assets": 0}
    if not page_dir or not assets_dir:
        return empty
    try:
        os.makedirs(page_dir, exist_ok=True)
        os.makedirs(assets_dir, exist_ok=True)
        script = COLLECT_JS.replace("__MAX_ITEMS__", str(MAX_JSON_ITEMS))
        script = script.replace("__SITE__", json.dumps(site))
        data = json.loads(tab.run_js(script, as_expr=True, timeout=90))
        html = str(data.get("html", ""))
        if not html:
            return empty

        names: dict[str, str] = {}
        wanted: list[str] = []
        for url in data.get("assets", []):
            url = str(url)
            if url in names:
                continue
            name = asset_name(url)
            if not name:
                continue
            names[url] = name
            if not os.path.exists(os.path.join(assets_dir, name)):
                wanted.append(url)
        saved = _download(tab, wanted[:MAX_ASSETS_PER_PAGE], assets_dir, names)

        if site == "creepjs" and any(
                not os.path.isfile(os.path.join(assets_dir, name)) for name in names.values()):
            raise RuntimeError("CreepJS CSS 未全部保存到本地")

        # 页面在 <别名-编号>/<启动编号>/ 下，CSS 在共享目录，用相对路径指过去
        for url, name in names.items():
            if os.path.exists(os.path.join(assets_dir, name)):
                html = html.replace(url, f"../../_assets/{name}")
        raw = html.encode("utf-8", "replace")
        if len(raw) > MAX_HTML_BYTES:
            raw = raw[:MAX_HTML_BYTES]
        filename = f"{site}.html"
        _write_bytes_atomic(os.path.join(page_dir, filename), raw)
        items = [item for item in (data.get("items") or [])
                 if isinstance(item, dict) and item.get("label") and item.get("value")]
        return {"page": filename, "items": items[:MAX_JSON_ITEMS],
                "title": str(data.get("title", ""))[:200], "assets": saved}
    except Exception as exc:
        print(f"{site} 页面存档失败: {exc}", file=sys.stderr)
        return empty
