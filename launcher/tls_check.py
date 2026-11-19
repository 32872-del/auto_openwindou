"""从目标浏览器的 TrackMe 页面读取显示值，不另发指纹检测请求。"""
import hashlib
import re
import time

from .page_query import navigate, close_page
from vendor_runtime.legacy_core.web_monitor import WebMonitorError, _cdp_command, _list_pages


TLS_URL = 'https://tls.peet.ws/'
PAGE_FIELDS = (
    'JA3', 'p0f', 'JA4', 'Akamai', 'PeetPrint', 'Used TLS', 'Used HTTP',
    'Total http/2 frames', 'Supported HTTP', 'Supported TLS', 'IP TTL',
    'TLS cipher suites', 'TLS extensions', 'TLS curves',
)

# 按可见标签读取，避免依赖网站的 CSS 类名。仅返回指定字段的文本。
READ_PAGE = r"""
(() => {
  const labels = new Map([
    ['ja3', 'JA3'], ['p0f', 'p0f'], ['ja4', 'JA4'],
    ['akamai http/2 fingerprint', 'Akamai'], ['akamai http fingerprint', 'Akamai'],
    ['peetprint', 'PeetPrint'], ['used tls', 'Used TLS'], ['used http', 'Used HTTP'],
    ['total http/2 frames', 'Total http/2 frames'], ['supported http', 'Supported HTTP'],
    ['supported tls', 'Supported TLS'], ['ip ttl', 'IP TTL'],
    ['tls cipher suites', 'TLS cipher suites'], ['tls extensions', 'TLS extensions'],
    ['tls curves', 'TLS curves']
  ]);
  const fields = {};
  if (!document.body) return {url: location.href, fields};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let active = null;
  while (walker.nextNode()) {
    const node = walker.currentNode, parent = node.parentElement;
    if (!parent || parent.closest('script,style,noscript,template,[hidden]')) continue;
    if (!parent.getClientRects().length || getComputedStyle(parent).visibility === 'hidden') continue;
    const text = node.textContent.trim();
    if (!text) continue;
    const label = text.replace(/\s+/g, ' ').toLowerCase();
    if (label === 'other useful sites' || label === 'api endpoints') break;
    if (label === 'request details' || label === 'your fingerprints') { active = null; continue; }
    if (labels.has(label)) {
      active = labels.get(label);
      fields[active] = [];
    } else if (active) {
      fields[active].push(text);
    }
  }
  return {url: location.href, fields};
})()
"""


def page_values(snapshot):
    """只接受已填好的页面；哈希/原始串匹配后再作为同一条检测保存。"""
    from urllib.parse import urlsplit
    location = urlsplit(snapshot.get('url', ''))
    if location.scheme != 'https' or location.hostname != 'tls.peet.ws' or location.path not in ('', '/'):
        raise ValueError('检测标签页已离开 tls.peet.ws 首页')
    fields = snapshot.get('fields', {})
    for label in PAGE_FIELDS:
        lines = fields.get(label)
        if not isinstance(lines, list) or not lines or any(not isinstance(line, str) for line in lines):
            raise ValueError('页面尚未提供 ' + label)
        if any(re.search(r'loading|加载中|undefined|\bnull\b', line, re.I) for line in lines):
            raise ValueError(label + ' 尚未加载完成')
    result = {}
    for label, key in (('JA3', 'ja3'), ('Akamai', 'akamai'), ('PeetPrint', 'peetprint')):
        lines = fields[label]
        # HTTP/1.x 不提供 Akamai HTTP/2 指纹，保留网页给出的缺失说明。
        if label == 'Akamai' and not re.fullmatch(r'[0-9a-fA-F]{32}', lines[0]):
            if not any(re.search(r'not |unavailable|n/a|none|unsupported', line, re.I) for line in lines):
                raise ValueError('Akamai 指纹尚未加载完成')
            result[key + '_hash'] = '\n'.join(lines)
            result[key] = ''
            continue
        fingerprint_hash = lines[0].strip()
        raw = ''.join(lines[1:]).strip()
        if not re.fullmatch(r'[0-9a-fA-F]{32}', fingerprint_hash) or not raw:
            raise ValueError(label + ' 的哈希或原始串不完整')
        if hashlib.md5(raw.encode('utf-8')).hexdigest() != fingerprint_hash.lower():
            raise ValueError(label + ' 的哈希与原始串不匹配')
        result[key + '_hash'] = fingerprint_hash
        result[key] = raw
    for label, key in (
        ('p0f', 'p0f'), ('JA4', 'ja4'), ('Used TLS', 'used_tls'),
        ('Used HTTP', 'used_http'), ('Total http/2 frames', 'http2_frames'),
        ('Supported HTTP', 'supported_http'), ('Supported TLS', 'supported_tls'),
        ('IP TTL', 'ip_ttl'), ('TLS cipher suites', 'ciphers'),
        ('TLS extensions', 'extensions'), ('TLS curves', 'curves'),
    ):
        result[key] = '\n'.join(fields[label])
    if any(len(value) > 32767 for value in result.values()):
        raise ValueError('检测字段超过 Excel 单元格容量，未截断保存')
    return result


def close_tls_page(debug_port, target):
    """只收起本次新建且仍停留在检测站点的标签页，保留用户的其它页面。"""
    from urllib.parse import urlsplit
    target_id = target.get('id')
    if not target_id:return
    pages = _list_pages(debug_port, timeout=3)
    current = next((page for page in pages if page.get('id') == target_id), None)
    if current is None:return
    location = urlsplit(current.get('url', ''))
    if location.scheme != 'https' or location.hostname != 'tls.peet.ws' or location.path not in ('', '/'):
        return
    # 用户可能已关掉其它页面；留一个空白页以免关闭整个浏览器窗口。
    if len(pages) == 1:navigate(debug_port, 'about:blank', timeout=3)
    close_page(debug_port, target_id)


def collect_tls(debug_port, *, timeout, cancel_event, on_target=None):
    deadline = time.monotonic() + timeout

    def remaining():
        if cancel_event.is_set():
            raise InterruptedError('TLS 检测已取消')
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('TLS 页面未在规定时间内提供完整检测值')
        return min(3, seconds)

    target = navigate(debug_port, TLS_URL, timeout=remaining())
    target_id = target.get('id')
    if not target_id:raise ValueError('TLS 检测标签页没有返回 ID')
    if on_target is not None:on_target(target)
    ws_url = target.get('webSocketDebuggerUrl')
    if not ws_url:
        pages = _list_pages(debug_port, timeout=remaining())
        ws_url = next((page['webSocketDebuggerUrl'] for page in pages if page['id'] == target_id), '')
    if not ws_url:
        raise ValueError('TLS 检测标签页没有可用的查询通道')
    last_error = ''
    while True:
        try:
            wait = remaining()
        except TimeoutError as exc:
            raise TimeoutError(str(exc) + ('；' + last_error if last_error else '')) from exc
        try:
            response = _cdp_command(ws_url, 'Runtime.evaluate', {
                'expression': READ_PAGE, 'returnByValue': True,
            }, timeout=wait)
        except WebMonitorError as exc:
            # 页面跳转时执行上下文可能短暂消失，只在原标签页仍存在时继续等。
            pages = _list_pages(debug_port, timeout=remaining())
            if not any(page['id'] == target_id for page in pages):
                raise ValueError('TLS 检测标签页已关闭') from exc
            last_error = str(exc)
            cancel_event.wait(min(.4, max(0, deadline - time.monotonic())))
            continue
        if response.get('exceptionDetails'):
            raise ValueError('TLS 页面字段读取失败')
        snapshot = response.get('result', {}).get('value')
        if isinstance(snapshot, dict):
            try:
                values = page_values(snapshot)
                remaining()
                return values
            except ValueError as exc:
                last_error = str(exc)
        cancel_event.wait(min(.4, max(0, deadline - time.monotonic())))
