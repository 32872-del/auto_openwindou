"""四站检测结果的公开白名单、类型转换与完成度判定。"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any, Iterable


SITE_SCHEMAS: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "creepjs": {
        "fingerprint_id": ("hash", ("FP ID",)),
        "fuzzy": ("hash", ("Fuzzy",)),
    },
    "browserscan": {
        "ip_address": ("ip", ("IP", "IP Address", "Your IP")),
        "country": ("str", ("Country",)),
        "region": ("str", ("Region", "State")),
        "city": ("str", ("City",)),
        "postal_code": ("str", ("Postal Code", "ZIP Code", "ZIP")),
        "isp": ("str", ("ISP", "Internet Service Provider")),
        "proxy_detected": ("bool", ("Proxy", "Proxy Detected", "VPN / Proxy")),
        "anonymous": ("bool", ("Anonymous", "Anonymity")),
        "blacklisted": ("bool", ("Blacklisted", "Blacklist")),
        "dns_servers": ("dns", ("DNS Leak", "DNS Leaking")),
        "bot_detected": ("bool", ("Bot", "Bot Detected")),
        "browser": ("str", ("Browser", "Browser Name")),
        "browser_version": ("str", ("Browser Version",)),
        "operating_system": ("str", ("Operating System", "OS")),
        "platform": ("str", ("Platform",)),
        "ip_timezone": ("str", ("IP Timezone", "IP Time Zone", "Time Zone Based on IP")),
        "browser_timezone": ("str", ("Browser Timezone", "Browser Time Zone", "Timezone", "Time Zone")),
        "languages": ("str", ("Languages", "Language")),
        "webrtc_public_ip": ("ip", ("WebRTC Public IP", "WebRTC IP", "WebRTC")),
        "canvas": ("hash", ("Canvas", "Canvas Fingerprint")),
        "webgl": ("hash", ("WebGL", "WebGL Fingerprint")),
        "webgl_vendor": ("str", ("WebGL Vendor", "Unmasked Vendor")),
        "webgl_renderer": ("str", ("WebGL Renderer", "Unmasked Renderer")),
        # 指纹面板：这些都是 js 最后才算出来的，值是 8 位十六进制
        "visitor_id": ("hash", ("visitor ID", "Visitor ID")),
        "webgl_report": ("hash", ("WebGL Report",)),
        "audio": ("hash", ("Audio", "Audio Fingerprint")),
        "client_rects": ("hash", ("Client Rects", "ClientRects")),
        "webgpu_report": ("hash", ("WebGPU Report", "WebGPU")),
        "screen_resolution": ("size", ("Screen Resolution",)),
        "available_screen_size": ("size", ("Available Screen Size",)),
        "color_depth": ("int", ("Color Depth",)),
        "touch_support": ("str", ("Touch Support",)),
        "ip_count_7days": ("int", ("IP Count (7 days)", "IP Count")),
    },
    "browserleaks": {
        "ip_address": ("ip", ("IP Address", "Your IP Address", "IP")),
        "country": ("str", ("Country",)),
        "region": ("str", ("Region", "State", "State/Region")),
        "city": ("str", ("City",)),
        "isp": ("str", ("ISP", "Internet Service Provider")),
        "organization": ("str", ("Organization", "Organisation")),
        "asn": ("asn", ("ASN", "AS Number")),
        "usage_type": ("str", ("Usage Type",)),
        "timezone": ("str", ("Timezone", "Time Zone")),
        "ipv6_address": ("ip", ("IPv6 Address", "IPv6")),
        "webrtc_local_ip": ("ip", ("WebRTC Local IP", "Local IP Address")),
        "webrtc_public_ip": ("ip", ("WebRTC Public IP", "Public IP Address")),
        "tcp_os": ("str", ("TCP/IP OS", "TCP OS", "Operating System")),
        "tcp_mtu": ("int", ("TCP/IP MTU", "TCP MTU", "MTU")),
        "tcp_distance_hops": ("int", ("TCP/IP Distance", "Distance", "Hops")),
        "ja4t": ("str", ("JA4T", "TCP JA4T")),
        "tls_ja4": ("str", ("TLS JA4", "JA4")),
        "tls_ja3_hash": ("str", ("TLS JA3 Hash", "JA3 Hash", "JA3")),
        "http2_akamai_hash": ("str", ("HTTP/2 Akamai Hash", "Akamai Hash", "HTTP/2")),
        "tor_relay": ("bool", ("Tor Relay", "Tor Exit Node")),
        # Whois 面板是整页最后补上的一块，拿它当"跑完了"的信号
        "whois_source_registry": ("str", ("Source Registry",)),
        "whois_net_range": ("str", ("Net Range",)),
        "whois_cidr": ("str", ("CIDR",)),
        "whois_org_name": ("str", ("Full Name", "Org Name")),
    },
    "scamalytics": {
        "ip_address": ("ip", ("IP Address", "IP")),
        "fraud_score": ("score", ("Fraud Score", "IP Fraud Score")),
        "risk": ("str", ("Risk", "Risk Level")),
        "country": ("str", ("Country", "Country Name")),
        "operator": ("str", ("Operator", "ISP", "ISP Name", "Internet Service Provider")),
        "anonymizing_vpn": ("bool", ("Anonymizing VPN", "VPN")),
        "tor_exit_node": ("bool", ("Tor Exit Node", "Tor")),
        "server": ("bool", ("Server", "Hosting Server")),
        "public_proxy": ("bool", ("Public Proxy",)),
        "web_proxy": ("bool", ("Web Proxy",)),
    },
    "leakdig": {
        # 主证据是静态 HTML 存档；这里的字段只是附带的结构化数据
        "ip_address": ("ip", ("IP", "IP 地址", "IP地址")),
        "authenticity": ("str", ("指纹真实性", "浏览器指纹真实度", "真实度")),
        "dns_leak": ("str", ("DNS 泄漏", "DNS泄漏", "DNS Leak")),
        "timezone": ("str", ("时区", "本地时间", "IP时间", "IP 时间")),
        "isp": ("str", ("运营商", "ISP", "网络运营商")),
        "country": ("str", ("国家", "国家/地区")),
        "city": ("str", ("城市",)),
    },
    "ip2proxy": {
        "ip_address": ("ip", ("IP Address", "IP")),
        "proxy_detected": ("bool", ("Proxy Detected", "VPN or Proxy", "Proxy")),
        "proxy_type": ("str", ("Proxy Type",)),
        "country_code": ("country_code", ("Country Code",)),
        "country": ("str", ("Country", "Country Name")),
        "region": ("str", ("Region", "Region Name", "State")),
        "city": ("str", ("City", "City Name")),
        "isp": ("str", ("ISP", "Internet Service Provider")),
        "domain": ("str", ("Domain",)),
        "usage_type": ("str", ("Usage Type",)),
        "asn": ("asn", ("ASN", "AS Number")),
        "as_name": ("str", ("AS Name", "AS")),
        "last_seen_days": ("int", ("Last Seen", "Last Seen Days")),
        "threat": ("str", ("Threat", "Threat Type")),
        "provider": ("str", ("Provider",)),
        "fraud_score": ("score", ("Fraud Score",)),
    },
}

SITE_HOSTS = {
    "creepjs": {"abrahamjuliot.github.io"},
    "browserscan": {"browserscan.net", "www.browserscan.net"},
    "browserleaks": {"browserleaks.com", "www.browserleaks.com"},
    "scamalytics": {"scamalytics.com", "www.scamalytics.com"},
    "ip2proxy": {"ip2proxy.com", "www.ip2proxy.com"},
    "leakdig": {"leakdig.com", "www.leakdig.com"},
}

SITE_MINIMUMS = {
    "creepjs": ({"fingerprint_id", "fuzzy"}, {"fingerprint_id"}),
    "browserscan": ({"ip_address", "dns_servers"}, {"browser", "operating_system", "proxy_detected"}),
    # TLS 指纹与 Whois 都是异步补上的（实测 0.8~3.5 秒），必须等到它们才算这站读完；
    # Hostname 不列进来——没有反查记录时站点自己就显示 n/a，等它会白等到超时。
    "browserleaks": ({"ip_address", "ipv6_address", "tls_ja4", "tls_ja3_hash",
                      "http2_akamai_hash", "whois_net_range"},
                     {"isp", "asn", "tcp_os", "ja4t"}),
    "scamalytics": ({"fraud_score"}, {"country", "operator"}),
    "ip2proxy": ({"ip_address"}, {"proxy_detected", "proxy_type"}),
    # leakdig 组件化渲染，通用抽取拿不全；存档以 HTML 为准，这里门槛放低
    "leakdig": (set(), {"ip_address", "authenticity", "dns_leak", "timezone"}),
}

# 结果页就绪标记：五秒盾放行后页面才会出现的固定标题，用来确认盾已过、可以开始读字段。
# 取值全部来自真实页面实测（2026-08-21，Chrome 打开四站抓 h1/h2/h3）：
#   ip2proxy    div h2 = "Enter an IP Address:" / "IP2Proxy Proxy Detection Result"
#   browserleaks h1    = "What Is My IP Address"
#   scamalytics  h1    = "<IP> Fraud Risk"
#   browserscan  h2    = "IP address lookup"（同页还有 h3 "Browser:"/"Proxy:"）
# 盾页只有 Cloudflare 自己的内容，这些标题一律不存在，所以能干净地区分"盾"和"真页"。
SITE_READY_MARKERS: dict[str, dict[str, str]] = {
    "ip2proxy": {"selector": "div h2", "contains": "ip add"},
    "browserleaks": {"selector": "h1", "contains": "what is my ip address"},
    "scamalytics": {"selector": "h1", "contains": "fraud risk"},
    "browserscan": {"selector": "h2", "contains": "ip address lookup"},
    # leakdig 首页自动检测，出现「指纹」相关标题即说明主体已渲染
    "leakdig": {"selector": "h1,h2,h3", "contains": "指纹"},
}

# 页面自己标注"还在跑"的文案。出现就说明这一站没跑完，得继续等。
SITE_BUSY_MARKERS: dict[str, tuple[str, ...]] = {
    "leakdig": ("检测中",),
}
# 允许残留几个"还在跑"。leakdig 的 DNS 泄漏检测在代理下基本出不来——实测 16.6 秒时
# 其余 53 个全部跑完、只剩它一个，之后卡着 83 秒纹丝不动（直连时约 25 秒能出结果）。
# 为它一个人干等没有意义，所以允许剩 1 个就放行，等不到就在 error 里如实写明。
SITE_BUSY_TOLERANCE: dict[str, int] = {
    "leakdig": 1,
}

# 必需字段的等待上限：等满这么久还没出来，就**不再要求它**，照常收工。
# BrowserScan 的 DNS 那行是整页最后一项，出不来的时候能拖满 150 秒，
# 结果是页面一直开着、也存不下来——为一项等到超时不划算，等够 60 秒就放行并记账。
SITE_REQUIRED_GRACE: dict[str, dict[str, float]] = {
    "browserscan": {"dns_servers": 60.0},
    # BrowserLeaks 的 TLS 指纹装在 tls.browserleaks.com 的 iframe 里、Whois 也是异步补的，
    # 某些代理下这个子域根本连不上：实测有一轮整整卡满 150 秒才 partial。等够就放行。
    # IPv6 那一行也是异步补的，但**多数住宅代理压根没有 IPv6**，加载完就是 n/a
    # （n/a 会被当成"站点说没有这项"直接丢掉，字段永远不会出现）。所以等它，但要有上限，
    # 否则没有 IPv6 的代理每次都会拖满。
    "browserleaks": {"tls_ja4": 60.0, "tls_ja3_hash": 60.0,
                     "http2_akamai_hash": 60.0, "whois_net_range": 60.0,
                     "ipv6_address": 45.0},
}

# 等这么久还没算出结果就原地刷新一次（只刷当前这一页，不跳任何别的地址）。
# LeakDig 的地理位置/指纹面板偶尔会卡在"检测中"再也不动，刷一次基本就出来了。
SITE_REFRESH_AFTER: dict[str, float] = {
    # 25 秒而不是 45 秒：刷新必须发生在"检测中"预算（BUSY_WAIT_SECONDS）到期**之前**，
    # 否则刷完还没来得及等它算完就放行了——实测 10 轮每轮都剩 2~5 项检测中就是这么来的。
    "leakdig": 25.0,
}
SITE_MAX_REFRESH = 1

PUBLIC_FIELD_TYPES = {
    site: {field: kind for field, (kind, _aliases) in fields.items()}
    for site, fields in SITE_SCHEMAS.items()
}
# TLS 由独立采集器生成 Excel，只扩展存档元数据，不加入网页采集站点集合。
PUBLIC_FIELD_TYPES['tls'] = {'email': 'str', 'captured_at': 'str'}

_TRUE_VALUES = {
    "yes", "true", "detected", "positive", "proxy detected", "vpn detected",
    "anonymous", "blacklisted", "leak detected", "tor exit node",
}
_FALSE_VALUES = {
    "no", "false", "not detected", "negative", "clean", "not anonymous",
    "not blacklisted", "no leak", "no proxy", "no vpn", "no vpn or proxy detected",
    "not a proxy", "not a tor exit node",
}
# BrowserScan 把 DNS 服务器和后面的 "10 more" 直接拼在一起（"111.172.239.14710 more"），
# 所以最后一段先多吃几位数字，再逐位回退到合法八位组。
_DNS_V4_RE = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.)(\d{1,5})")
_DNS_V6_RE = re.compile(r"[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}")
_DNS_MORE_RE = re.compile(r"(\d+)\s*more", re.IGNORECASE)


def _extract_dns_servers(text: str) -> tuple[list[str], str]:
    """从 DNS 那行抽出服务器地址，并返回把地址切开后的文本。

    BrowserScan 会把服务器和后面的计数直接粘在一起（``111.172.239.14710 more``），
    所以最后一段先多吃几位数字再逐位回退到合法八位组，剩下的数字还给后文，
    否则 "10 more" 会被读成 "14710 more"。
    """
    servers: list[str] = []
    pieces: list[str] = []
    cursor = 0
    for match in _DNS_V4_RE.finditer(text):
        head, tail = match.group(1), match.group(2)
        for length in (3, 2, 1):
            candidate = head + tail[:length]
            try:
                servers.append(str(ipaddress.ip_address(candidate)))
            except ValueError:
                continue
            pieces.append(text[cursor:match.start()])
            pieces.append(" " + tail[length:] if tail[length:] else " ")
            cursor = match.end()
            break
    pieces.append(text[cursor:])
    for match in _DNS_V6_RE.finditer(text):
        token = match.group(0).strip(":")
        if ":" not in token:
            continue
        try:
            servers.append(str(ipaddress.ip_address(match.group(0))))
        except ValueError:
            continue
    return list(dict.fromkeys(servers)), "".join(pieces)


_EMPTY_VALUES = {"", "-", "--", "—", "n/a", "na", "none", "null", "unknown", "not available"}
# 站点自己标注“该项不提供/需登录/缺少运行条件”的固定文案，等同于没有值，不算转换失败。
_UNAVAILABLE_VALUE_RE = re.compile(
    r"^(sign\s*in( first)?(sign\s*in)?|log\s*in|login required|requires? login|"
    r"no\s*js|javascript|not supported|unsupported|unavailable|disabled|hidden)$"
)
# 站点自己写的"还在跑"占位。它跟"没有这项"是两回事：
# 没有这项 → 就此定案，不用等；还在跑 → 绝不能当结果存下来，必须继续等。
# `---.---.---.---` 这种纯符号骨架也归这类（LeakDig 的 IP 还没算出来时就长这样）。
_PENDING_VALUE_RE = re.compile(
    r"^(检测中|正在检测|检测ing|加载中|正在加载|计算中|正在计算|请稍候|请稍等|"
    r"loading|checking|calculating|pending|测试中)[.。·…\s]*$"
    r"|^[-—.·\s]{3,}$"
)
# 值里带 HTML 标签一定是抓错了（典型来源：JS 开着时 <noscript> 的 textContent
# 就是那段标签源码）。采集端已经挡了一道，这里再挡一道，两边都不认。
_MARKUP_VALUE_RE = re.compile(r"<[a-z!/][^>]*>", re.I)
_NEGATIVE_PROXY_VALUES = {"no proxy", "no vpn", "no vpn or proxy detected", "not a proxy"}
_BROWSER_VALUE_RE = re.compile(r"\b(?:chrome|chromium|firefox|safari|edge|opera|brave|vivaldi)\b", re.I)


def normalize_label(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().rstrip(":：-").strip()
    return text.casefold()


def all_aliases(site: str) -> list[str]:
    aliases: list[str] = []
    for _field, (_kind, names) in SITE_SCHEMAS[site].items():
        aliases.extend(names)
    return list(dict.fromkeys(aliases))


def _is_absent_value(folded_value: str) -> bool:
    """站点显示为空、N/A 或“需登录/无 JS”等占位时，视为该项没有值。"""
    core = re.sub(r"\s*\([^()]*\)\s*$", "", folded_value).strip()
    return (folded_value in _EMPTY_VALUES or core in _EMPTY_VALUES
            or bool(_UNAVAILABLE_VALUE_RE.fullmatch(folded_value))
            or bool(_UNAVAILABLE_VALUE_RE.fullmatch(core)))


def is_pending_value(value: Any) -> bool:
    """页面还在算这一项（"检测中……" / "---.---.---.---"），不是最终结果。"""
    text = unicodedata.normalize("NFKC", str("" if value is None else value))
    text = re.sub(r"\s+", " ", text).strip().strip(":：")
    return bool(_PENDING_VALUE_RE.match(text.casefold()))


def _parse_value(kind: str, value: Any) -> Any | None:
    text = unicodedata.normalize("NFKC", str("" if value is None else value))
    text = re.sub(r"\s+", " ", text).strip().strip(":：")
    folded = text.casefold()
    if folded in _EMPTY_VALUES or len(text) > 300:
        return None
    if kind == "str":
        return text
    if kind == "bool":
        if folded in _TRUE_VALUES:
            return True
        if folded in _FALSE_VALUES:
            return False
        return None
    if kind == "ip":
        # BrowserScan/BrowserLeaks 会在地址后附加 ``(USA)`` 等显示元数据；
        # 只接受开头的一个完整地址，绝不从任意正文中搜索 IP。
        candidates = [text.strip("[]")]
        before_parenthesis = text.split("(", 1)[0].strip().strip("[]")
        first_token = re.split(r"\s+", before_parenthesis, maxsplit=1)[0].strip("[],;")
        candidates.extend((before_parenthesis, first_token))
        for candidate in dict.fromkeys(candidates):
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
        return None
    if kind == "dns":
        # BrowserScan 的 DNS 那行在检测跑完前显示占位（"No"），跑完才列出解析服务器。
        # 没有 IP 就说明还没跑完，返回 None 让采集端继续等，不把占位当结果存下来。
        servers, leftover = _extract_dns_servers(text)
        if not servers:
            return None
        more = _DNS_MORE_RE.search(leftover)
        result = ", ".join(servers)
        if more:
            result += f" (+{more.group(1)} more)"
        return result[:300]
    if kind == "size":
        # 分辨率形如 2561×1601 / 1707x1019，页面上常和下一项标题粘一起，只取前面那段
        match = re.match(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})", text)
        return f"{match.group(1)}x{match.group(2)}" if match else None
    if kind == "hash":
        # 指纹哈希只可能是十六进制；这一项没算完时抓到的往往是隔壁那行的标题
        return text.upper() if re.fullmatch(r"[0-9a-fA-F]{6,64}", text) else None
    if kind == "country_code":
        return text.upper() if re.fullmatch(r"[A-Za-z]{2}", text) else None
    if kind in {"int", "score", "asn"}:
        match = re.search(r"(?<!\d)(\d{1,10})(?!\d)", text.replace("AS", "", 1) if kind == "asn" else text)
        if not match:
            return None
        number = int(match.group(1))
        if kind == "score" and not 0 <= number <= 100:
            return None
        if kind == "int" and not 0 <= number <= 1_000_000:
            return None
        if kind == "asn" and not 0 < number <= 4_294_967_295:
            return None
        return number
    return None


def normalize_candidates(site: str, candidates: Iterable[Iterable[Any]]) -> tuple[dict[str, Any], bool]:
    """把页面语义行转换成站点规范字段；返回字段及是否发生过转换失败。"""
    schema = SITE_SCHEMAS[site]
    aliases: dict[str, tuple[str, str]] = {}
    for field, (kind, names) in schema.items():
        for alias in names:
            aliases[normalize_label(alias)] = (field, kind)
    result: dict[str, Any] = {}
    failed_fields: set[str] = set()
    for pair in candidates:
        pair = list(pair)
        if len(pair) < 2:
            continue
        match = aliases.get(normalize_label(pair[0]))
        if not match:
            continue
        field, kind = match
        raw_value = pair[1]
        folded_value = normalize_label(raw_value)
        if field == "proxy_type" and folded_value in _NEGATIVE_PROXY_VALUES:
            result.setdefault("proxy_detected", False)
            continue
        if _is_absent_value(folded_value):
            continue
        if _MARKUP_VALUE_RE.search(str(raw_value)):
            failed_fields.add(field)
            continue
        if is_pending_value(raw_value):
            # 还在跑：算作"这一项没取到"，采集端会继续等，超时才退成 partial
            failed_fields.add(field)
            continue
        parsed = _parse_value(kind, raw_value)
        if parsed is None or (field == "browser" and not _BROWSER_VALUE_RE.search(str(parsed))):
            failed_fields.add(field)
            continue
        result.setdefault(field, parsed)
        if field == "proxy_type":
            result.setdefault("proxy_detected", True)
    # 同名标签在页面上常出现多次且顺序不定；只有最终一个可用值都没取到的字段才算转换失败。
    return result, bool(failed_fields - set(result))


def result_status(site: str, fields: dict[str, Any], conversion_failed: bool = False,
                  waived: set[str] | None = None) -> str:
    """``waived``：等超时后不再要求的必需字段（如 BrowserScan 的 DNS），不影响完成判定。"""
    required, any_of = SITE_MINIMUMS[site]
    required = required - (waived or set())
    complete = required.issubset(fields) and bool(any_of.intersection(fields))
    if complete and not conversion_failed:
        return "complete"
    return "partial"


def sanitize_canonical_fields(site: str, fields: Any) -> tuple[dict[str, Any], list[str]]:
    """只保留本进程认识、且类型对得上的字段，返回 ``(干净字段, 丢掉的键)``。

    为什么要有它：一次性采集在**独立子进程**里跑（DP 不能进 GUI 进程）。从源码运行时，
    父进程是启动那一刻的内存快照，子进程却是每次现读磁盘 —— 边开着软件边改代码，
    子进程就可能返回父进程还不认识的新字段。以前这会让整份采集连同五站存档一起作废，
    代价远大于"少一个字段"。现在丢掉不认识的那几个，其余照常入库。

    （打包成 exe 后父子是同一个二进制，不会错配；这层保险只为源码运行和版本不同步兜底。）
    """
    if site not in PUBLIC_FIELD_TYPES or not isinstance(fields, dict):
        return {}, []
    expected = PUBLIC_FIELD_TYPES[site]
    clean: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in fields.items():
        kind = expected.get(key)
        if not kind:
            dropped.append(str(key))
            continue
        if validate_canonical_fields(site, {key: value}):
            dropped.append(str(key))
            continue
        clean[key] = value
    return clean, dropped


def validate_canonical_fields(site: str, fields: dict[str, Any]) -> str:
    if site not in PUBLIC_FIELD_TYPES:
        return "未知检测站"
    if not isinstance(fields, dict):
        return "fields 必须是对象"
    expected = PUBLIC_FIELD_TYPES[site]
    for key, value in fields.items():
        kind = expected.get(key)
        if not kind:
            return f"fields 含未允许字段: {key}"
        if kind == "bool" and type(value) is not bool:
            return f"fields.{key} 必须是布尔值"
        if kind in {"int", "score", "asn"} and (type(value) is not int):
            return f"fields.{key} 必须是整数"
        if kind in {"str", "ip", "country_code", "dns", "hash", "size"} and not isinstance(value, str):
            return f"fields.{key} 必须是字符串"
        if isinstance(value, str) and len(value) > 300:
            return f"fields.{key} 过长"
        if _parse_value(kind, value) is None:
            return f"fields.{key} 值无效"
    return ""
