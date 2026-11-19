"""IPRoyal residential gateway credentials and per-row location parameters."""
import re


US_STATES_ONLY = 'IPRoyal 住宅代理仅支持美国州级定位，无法按本行所在的省份分配；请使用具体城市或选择支持该省的来源'


class UnsupportedRegion(ValueError):
    pass


class ProtocolMismatch(ValueError):
    pass


def residential_host(host):
    host = host.lower().rstrip('.')
    return host in ('geo.iproyal.com', 'proxy.iproyal.com') or host.endswith('.proxy.iproyal.com')


def gateway_protocol(port, protocol, *, infer=False):
    if port not in (12321, 32325):
        return protocol
    if (port == 12321 and protocol in ('http', 'https')) or (port == 32325 and protocol == 'socks5'):
        return protocol
    if infer:
        return 'http' if port == 12321 else 'socks5'
    raise ProtocolMismatch('IPRoyal 住宅代理的 12321 端口使用 HTTP，32325 端口使用 SOCKS5，请核对协议和端口')


def split_password(password):
    # These gateways append options to the account password, starting with _country-.
    # Treat the entire prefix as opaque even if it resembles a city, state or session.
    prefix, separator, suffix = password.rpartition('_country-')
    return (prefix, separator + suffix) if separator else (password, '')


def convert_credentials(username, password):
    base, options = split_password(password)
    changes = []
    parameters = {
        'country': ('country', '${country}', '国家按本行地址填写'),
        'city': ('city', '${proxyCity}', '城市按本行地址填写'),
        'state': ('stateName', '${proxyState}', '州名按本行地址填写'),
        'session': ('session', '${session}', '每次分配生成 8 位会话号'),
    }

    def convert(match):
        key, value = match.groups()
        if value.startswith(('${', '@{')) or key == 'country' and not re.fullmatch('[a-zA-Z]{2}', value):
            return match[0]
        kind, replacement, note = parameters[key]
        before = match[0][1:]
        after = key + '-' + replacement
        # Browser string indexes count UTF-16 code units.
        start = len((base + options[:match.start() + 1]).encode('utf-16-le')) // 2
        end = start + len(before.encode('utf-16-le')) // 2
        changes.append(dict(field='password', kind=kind, original=before, replaced=after,
                            note=note, applied=True, start=start, end=end))
        return '_' + after

    options = re.sub(r'_(country|city|state|session)-([^_]+)', convert, options)
    return dict(username=username, password=base + options, changes=changes)


def fixed_country(password):
    _, options = split_password(password)
    match = re.match(r'_country-([a-z]{2})(?:_|$)', options, flags=re.I)
    return match[1].upper() if match else ''


def route_password(password, location, level):
    """Turn a city template into a state route only where the gateway supports it."""
    country = location['country']
    if level == 'state' and country != 'us':
        raise UnsupportedRegion(US_STATES_ONLY)
    base, options = split_password(password)
    if not options:
        return password
    if level == 'state':
        if '_state-' in options:
            options = re.sub(r'_city-[^_]+', '', options)
        elif '_city-' in options:
            options = re.sub(r'_city-[^_]+', '_state-${proxyState}', options)
        else:
            options = re.sub(r'(_country-[^_]+)', r'\1_state-${proxyState}', options, count=1)
    elif level == 'country' and country:
        options = re.sub(r'_(city|state)-\$\{(?:city|citySlug|proxyCity|state|stateName|proxyState)\}', '', options)
    return base + options
