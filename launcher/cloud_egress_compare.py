"""在同一次手机端出口异常诊断中，通过任务原代理访问相同检测站。"""
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_address
import re
import time
from urllib.parse import urlsplit

import requests

from .proxy import proxy_url
from vendor_runtime.server.python.cloud_phone_s.providers.duoplus.egress import SITES, parse_response


CHROME_RECHECK_SECONDS = 120


def merge_chrome_read(previous, reading, *, launch, expected, checked_at, failure='', automatic=False):
    """Compare each page's latest snapshot from this launch, retaining earlier conflicting evidence."""
    if previous.get('launch') != launch or previous.get('expected_ip') != expected:
        previous = {}
    probes = {(p['page_host'], p['site']): p for p in previous.get('probes', [])}
    page = reading.get('page_host', '')
    required = [('ip111.cn', 'ip111 ' + route + '线路') for route in ('国内', '国外', '谷歌')]
    if expected:
        required.append(('ip.sb', 'IP.SB IPv' + str(ip_address(expected).version)))
    prior_page = {site: probe for (host, site), probe in probes.items() if host == page}
    if all(probe['checked_at'] <= checked_at for probe in prior_page.values()):
        current = {item['site']: item for item in reading.get('probes', [])}
        page_sites = [site for host, site in required if host == page]
        for site in [*page_sites, *prior_page]:
            current.setdefault(site, {'site': site, 'ip': ''})
        for site, item in current.items():
            prior = prior_page.get(site, {})
            sample = {**item, 'page_host': page, 'checked_at': checked_at, 'automatic': automatic}
            if not item.get('ip') and (previous_ip := prior.get('ip') or prior.get('previous_ip')):
                sample['previous_ip'] = previous_ip
                sample['previous_checked_at'] = prior.get('previous_checked_at') or prior['checked_at']
                sample['previous_automatic'] = prior.get('automatic') if prior.get('ip') else prior.get('previous_automatic', prior.get('automatic'))
            probes[page, site] = sample
    if previous.get('checked_at', 0) > checked_at:
        checked_at = previous['checked_at']
        page = previous.get('page_host', '')
        failure = previous.get('last_read_error', '')
    result = {'method': 'chrome_page', 'launch': launch, 'expected_ip': expected,
              'page_host': page, 'checked_at': checked_at, 'last_read_error': failure,
              'probes': list(probes.values()), 'status': 'unavailable', 'error': failure}
    if not probes:
        return result

    by_family = {4: set(), 6: set()}
    manual = {4: set(), 6: set()}
    for probe in probes.values():
        value = probe.get('ip') or probe.get('previous_ip')
        if value:
            address = ip_address(value)
            by_family[address.version].add(address)
            from_startup = probe.get('automatic') if probe.get('ip') else probe.get('previous_automatic', probe.get('automatic'))
            if not from_startup:
                manual[address.version].add(address)
    if not any(by_family.values()):
        result['error'] = failure or '当前栏目的 IP 读数不明确，请刷新检测页后重读'
        return result
    if expected:
        address = ip_address(expected)
        by_family[address.version].add(address)
        manual[address.version].add(address)
    if any(len(ips) > 1 for ips in manual.values()):
        result.update(status='mismatch', error='本次启动的 Chrome 页面读数出现不同出口，请刷新相应页面并重新读取')
        return result
    if any(len(ips) > 1 for ips in by_family.values()):
        result.update(status='cached', error='开机时读取的页面可能保留上次的 IP。请在手机 Chrome 刷新对应检测页，等读数显示后再读取')
        return result

    result.update(status='partial', error=failure or '请在手机 Chrome 刷新并读取 ip111.cn 各线路和 ip.sb 地址')
    if not expected or failure or any(probe.get('error') for probe in probes.values()):
        return result
    family = ip_address(expected).version
    if any(key not in probes or probes[key]['automatic'] or probes[key]['ip'] != expected for key in required):
        return result
    if any(p.get('ip') and ip_address(p['ip']).version != family for p in probes.values()):
        result['error'] = '还读到了另一地址族；厂商仅提供一个地址族的比对 IP'
        return result
    expires = min(probes[key]['checked_at'] for key in required) + CHROME_RECHECK_SECONDS
    if expires <= checked_at:
        result['error'] = '两页读数间隔超过两分钟，请刷新并重新读取较早的页面'
        return result
    result.update(status='verified', error='', expires_at=expires)
    return result


def _probe(address, site, url):
    result = {'site': site, 'host': urlsplit(url).hostname, 'ip': ''}
    deadline = time.monotonic() + 8
    headers = {'Cache-Control': 'no-cache', 'User-Agent': 'WindowStudio-EgressCheck/1.0'}
    if url in (SITES[1][1], SITES[2][1]):
        headers['Referer'] = 'https://ip111.cn/'
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, proxies={'http': address, 'https': address},
                             headers=headers, allow_redirects=False,
                             timeout=(3, 4), stream=True) as response:
                if response.status_code != 200:
                    return {**result, 'error': f'HTTP {response.status_code}'}
                body = bytearray()
                for chunk in response.iter_content(1):
                    if time.monotonic() >= deadline or len(body) + len(chunk) > 16384:
                        return {**result, 'error': '检测页超时或响应过大'}
                    body.extend(chunk)
                result['ip'] = parse_response(url, body.decode('utf-8', errors='replace'))
    except requests.RequestException:
        result['error'] = '通过原代理访问检测站失败'
    except ValueError:
        result['error'] = '检测页没有返回可确认的公网 IP'
    return result


def _session_warning(proxy, assigned_at):
    host = str(proxy.get('host') or '').lower().rstrip('.')
    if not host.endswith('.iproyal.com') and host != 'iproyal.com':
        return ''
    duration = re.search(r'(?:^|_)lifetime-(\d{1,5})([mhd])(?:_|$)', str(proxy.get('password') or ''))
    if not duration:
        return ''
    if not isinstance(assigned_at, (float, int)) or assigned_at <= 0:
        return '旧记录缺少代理分配时间，无法确认是否仍在原会话有效期内'
    seconds = int(duration[1]) * {'m': 60, 'h': 3600, 'd': 86400}[duration[2]]
    if time.time() >= assigned_at + seconds:
        return '原代理会话已超过配置时长，重新连接可能取得另一出口'
    return ''


def compare_guest_egress(guest_report, selected_proxy, *, assigned_at=None):
    report = {'status': 'inconclusive', 'summary': '', 'probes': []}
    if guest_report.get('status') != 'mismatch':
        return {**report, 'summary': '手机检测没有报告出口不一致'}
    if not selected_proxy or not selected_proxy.get('host'):
        return {**report, 'summary': '没有任务代理快照，无法从本机复测设备现有代理'}
    try:
        address = proxy_url(selected_proxy)
    except (KeyError, TypeError, ValueError):
        return {**report, 'summary': '任务代理快照不完整，无法从本机复测'}

    guest_ips = {}
    for item in guest_report.get('probes', []):
        try:
            ip = ip_address(item.get('ip', ''))
        except ValueError:
            continue
        if ip.is_global:
            guest_ips[(item.get('site'), item.get('host'))] = str(ip)
    sites = [(site, url) for site, url in SITES if (site, urlsplit(url).hostname) in guest_ips]
    if not sites and any(host == 'ip.sb' for _, host in guest_ips):
        return {**report, 'summary': 'IP.SB 首页与命令行 API 的地址不同。请在同一台手机刷新 ip111.cn，读取国外和谷歌两栏以逐站对照原代理'}
    if not sites:
        return {**report, 'summary': '缺少可与本机对照的手机端站点读数'}
    with ThreadPoolExecutor(max_workers=len(sites), thread_name_prefix='cloud-egress') as pool:
        futures = [pool.submit(_probe, address, site, url) for site, url in sites]
        probes = [future.result() for future in futures]
    for probe in probes:
        probe['guest_ip'] = guest_ips[(probe['site'], probe['host'])]
    report['probes'] = probes
    warning = _session_warning(selected_proxy, assigned_at)
    if warning:
        return {**report, 'summary': warning}
    paired = [p for p in probes if p['ip']]
    if len(paired) == 1:
        pair = paired[0]
        return {**report, 'summary': f"仅取得 {pair['host']} 一组对照。手机读数 {pair['guest_ip']}，本机通过原代理读数 {pair['ip']}，还需另一线路才能判断分流"}
    if len(paired) != len(probes):
        return {**report, 'summary': '完整的成对读数不足，无法区分代理出口变化与设备侧分流'}

    local = [item['ip'] for item in probes]
    guest = [guest_ips[(item['site'], item['host'])] for item in probes]
    if len({ip_address(ip).version for ip in local + guest}) > 1:
        return {**report, 'summary': '各站点返回的 IP 地址族不同，无法用这些读数判断是否分流'}
    if local == guest:
        if len(set(guest)) > 1:
            return {**report, 'status': 'matched_split', 'summary': '本机通过原代理访问相同站点，也复现了手机端逐站相同的不同出口'}
        if guest[0] == guest_report.get('expected_ip') or not guest_report.get('expected_ip'):
            return {**report, 'summary': '可逐站对照的线路读数一致，其他页面的出口差异仍待核实'}
        return {**report, 'status': 'matched_uniform', 'summary': '可逐站对照的线路在两端均读到同一出口，与开机前记录的地址不同'}
    if len(set(guest)) > 1 and len(set(local)) == 1 and local[0] in guest:
        return {**report, 'status': 'guest_split_only', 'summary': '本机通过原代理访问这些站点时出口一致，仅手机端测到分流'}
    return {**report, 'summary': '本机与手机的逐站结果未对应，无法区分代理轮换和设备侧路由差异'}
