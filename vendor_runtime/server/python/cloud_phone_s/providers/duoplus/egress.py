"""Measure egress at the phone, with a separate read-only view of Chrome's current test page."""
from html.parser import HTMLParser
from ipaddress import ip_address
from urllib.parse import urlsplit
import re
import shlex

from ...duoplus import DuoPlusError


SITES = (
    ('IP.SB', 'https://api.ip.sb/ip'),
    ('ip111 国外线路', 'https://us.ip111.cn/ip.php'),
    ('ip111 谷歌线路', 'https://sspanel.net/ip.php'),
)


def _public_ip(value):
    try:
        address = ip_address(value.strip())
    except ValueError:
        return ''
    if not address.is_global or address.is_multicast or '%' in str(address):
        return ''
    return str(address)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.texts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head'):
            self.skip_depth = max(0, self.skip_depth - 1)

    def handle_data(self, data):
        if not self.skip_depth:
            self.texts.extend(data.splitlines())


def parse_response(url, body):
    if not isinstance(body, str) or not body or len(body) > 16384:
        raise ValueError('检测页未返回可识别的 IP')
    if url == SITES[0][1]:
        address = _public_ip(body)
        if address:
            return address
    elif url in (SITES[1][1], SITES[2][1]):
        parser = _VisibleText()
        parser.feed(body)
        # ip111 embeds a single address followed by a location. Ignore markup/diagnostic addresses.
        lines = [line.strip() for line in parser.texts if line.strip()]
        if lines and (address := _public_ip(lines[0])):
            additional = {_public_ip(line) for line in lines[1:]} - {''}
            if not (additional - {address}):
                return address
    raise ValueError('检测页未返回可确认的单一公网 IP')


def classify(method, expected, probes):
    expected = _public_ip(expected or '')
    by_version = {4: set(), 6: set()}
    for probe in probes:
        if probe.get('ip'):
            address = ip_address(probe['ip'])
            by_version[address.version].add(str(address))
    if expected:
        by_version[ip_address(expected).version].add(expected)
    count = sum(bool(probe.get('ip')) for probe in probes)
    if any(len(values) > 1 for values in by_version.values()):
        status, message = 'mismatch', '同一地址族测得不同出口，请在详情中核对各线路和厂商检测 IP'
    elif not count:
        status, message = 'unavailable', '尚未读到可确认的公网 IP'
    elif not expected or count < len(probes) or count < 2 or all(by_version.values()):
        status, message = 'partial', '已有部分出口读数，尚未确认各线路与厂商 IP 一致'
    else:
        status, message = 'verified', '已测线路与厂商检测 IP 一致'
    return {'method': method, 'status': status, 'expected_ip': expected, 'probes': probes,
            'error': '' if status == 'verified' else message}


def measure_guest(api, device_id, expected, check, *, selected_sites=None):
    probes = []
    for site, url in SITES:
        if selected_sites is not None and (site, url) not in selected_sites:
            continue
        check()
        probe = {'site': site, 'host': urlsplit(url).hostname, 'ip': ''}
        probes.append(probe)
        # Do not pass proxy credentials here: use the device's own network path.
        # The provider caps device commands at 10 s. Accept only HTTP 200 with a complete payload.
        referrer = ' --referer https://ip111.cn/' if site != 'IP.SB' else ''
        command = ("curl -fsS --connect-timeout 3 --max-time 7 --max-filesize 16384"
                   " -H 'Cache-Control: no-cache' -w '\\nDPLUS_HTTP_%{http_code}'" + referrer + ' ' + shlex.quote(url))
        try:
            output = api.command(device_id, command).strip()
            body, sep, code = output.rpartition('\nDPLUS_HTTP_')
            if not sep or code != '200':
                raise ValueError('设备 HTTPS 检测未取得有效响应' + ('（HTTP ' + code + '）' if re.fullmatch(r'[1-5]\d\d', code) else ''))
            probe['ip'] = parse_response(url, body)
        except DuoPlusError:
            probe['error'] = '厂商未完成设备 HTTPS 查询'
        except ValueError as exc:
            probe['error'] = str(exc)
    return classify('android_https', expected, probes)


_IP111_ROUTES = {
    '国内': ('ip111 国内线路', 'ip111.cn'),
    '国外': (SITES[1][0], urlsplit(SITES[1][1]).hostname),
    '谷歌': (SITES[2][0], urlsplit(SITES[2][1]).hostname),
}


def _chrome_probe(site, host, addresses):
    probe = {'site': site, 'host': host, 'ip': ''}
    if len(addresses) == 1:
        probe['ip'] = next(iter(addresses))
    elif len(addresses) > 1:
        probe['error'] = '同一栏目显示多个不同 IP，无法确认出口'
    return probe


def _ip111_page(lines):
    addresses = {route: set() for route in _IP111_ROUTES}
    current = None
    for line in lines:
        heading = re.fullmatch(r'从(国内|国外|谷歌)测试(?:[\s，,:：]+(.*))?', line)
        if heading:
            current = heading[1]
            line = heading[2] or ''
        if line.startswith(('这是您访问', '您访问没有被封', '本站仅提供')):
            current = None
        if current and line:
            # Chrome may group the iframe address and its location into one label.
            tokens = re.split(r'[\s,，]+', line)
            if _public_ip(tokens[0]):
                addresses[current].update(address for token in tokens if (address := _public_ip(token)))
    return [_chrome_probe(site, host, addresses[route]) for route, (site, host) in _IP111_ROUTES.items()]


def _ipsb_home(lines):
    # Match the Address row of a connectivity card. The rest of the same card can contain
    # other legitimate IPs (Hostname, Network) that do not report the visitor's address.
    family = None
    want_address = False
    addresses = {4: set(), 6: set()}
    for line in lines:
        section = re.fullmatch(r'IPv([46])\s*(?:connectivity|连接性|连通性|连接)'
                               r'(?:[\s,，·]+(?:[✓✔]\s*)?(?:Supported|Unsupported|已支持|不支持))?',
                               line, re.I)
        if section:
            family, want_address = int(section[1]), False
            continue
        if line.lower() in ('more info', '更多信息'):
            family, want_address = None, False
        if not family:
            continue
        field = re.fullmatch(r'(?:Address|IP\s*地址|地址)(?:[\s,:，：]+(.*))?', line, re.I)
        if field:
            want_address = True
            if not field[1]:
                continue
            line = field[1]
        if want_address:
            value = re.sub(r'[\s,，]+(?:copy|复制)$', '', line, flags=re.I)
            values = [_public_ip(item) for item in re.split(r'[\s,，]+', value)]
            if not all(values):
                want_address = False
                continue
            addresses[family].update(address for address in values if ip_address(address).version == family)
    return [_chrome_probe('IP.SB IPv' + str(family), 'ip.sb', values)
            for family, values in addresses.items() if values]


def read_chrome(nodes, expected):
    chrome = [n for n in nodes if n.get('package') == 'com.android.chrome']
    bars = [n for n in chrome if n.get('id') == 'com.android.chrome:id/url_bar']
    if len(bars) != 1:
        raise ValueError('请让一个云手机 Chrome 检测页和地址栏同时可见，再读取当前页')
    bar = bars[0]
    value = (bar.get('text') or '').strip()
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    if (bar.get('focused') or parsed.query or parsed.fragment or parsed.scheme not in ('https', 'http') or parsed.username is not None or
            parsed.password is not None or parsed.port not in (None, 443 if parsed.scheme == 'https' else 80)):
        raise ValueError('请在云手机 Chrome 打开检测站首页，收起地址栏键盘后再读取')
    # Read only text from the recognised page. Never use an IP from Chrome's toolbar/omnibox.
    lines = [line.strip() for n in chrome if n.get('web_content') and not n.get('password') and
             n.get('class') != 'android.widget.EditText'
             for line in (n.get('text') or n.get('description') or '').splitlines() if line.strip()]
    host = (parsed.hostname or '').removeprefix('www.')
    probes = []
    if host == 'ip111.cn' and parsed.path in ('', '/', '/index.php'):
        probes = _ip111_page(lines)
    elif parsed.hostname == 'api.ip.sb' and parsed.path == '/ip':
        values = {address for line in lines if (address := _public_ip(line))}
        probes = [_chrome_probe('IP.SB', parsed.hostname, values)]
    elif host == 'ip.sb' and parsed.path in ('', '/'):
        probes = _ipsb_home(lines)
    else:
        raise ValueError('请在云手机 Chrome 打开 ip111.cn 或 IP.SB 的本机 IP 页面，再读取当前页')
    ambiguous = any(probe.get('error') for probe in probes)
    report = classify('chrome_page', expected, probes)
    if not ambiguous and not any(probe['ip'] for probe in probes):
        report['error'] = '检测页当前没有清晰的出口 IP，请等页面加载完成，并将检测结果和栏目标题滚到可见处'
    elif ambiguous and report['status'] != 'mismatch':
        report['error'] = '同一检测栏目出现多个不同 IP，请刷新页面后再读取'
    return {**report, 'page_host': host}
