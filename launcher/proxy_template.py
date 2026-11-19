"""代理账号和密码中的地区模板转换。

沿用浏览器自动化项目的地区标签规则；无标签国家仅在紧邻州全名时识别；
每处转换都返回说明，供粘贴确认弹窗逐项核对。
"""
import re
from dataclasses import asdict, dataclass

from .location import CA, US


STATE_LABELS = {'state', 'region', 'st', 'province'}
CITY_LABELS = {'city', 'town'}
ZIP_LABELS = {'zip', 'zipcode', 'postal', 'postcode'}
COUNTRY_LABELS = {'country', 'nation', 'geo'}
SESSION_LABELS = {'session', 'sessionid', 'sess', 'sid'}
LABELS = STATE_LABELS | CITY_LABELS | ZIP_LABELS | COUNTRY_LABELS | SESSION_LABELS
STATE_NAMES = {name.replace(' ', '').lower(): name for name in list(US.values()) + list(CA.values())}
COUNTRY_STATE_NAMES = {country:{name.replace(' ', '').lower() for name in regions.values()}
                       for country,regions in (('us',US),('ca',CA))}
STATE_CODES = {code.lower() for code in list(US) + list(CA)}
COUNTRY_CODES = {'us', 'ca', 'uk', 'gb', 'au', 'de', 'fr', 'it', 'es', 'nl', 'jp', 'kr', 'sg', 'hk', 'tw', 'in', 'br', 'mx'}
SPLIT = re.compile(r'([-_])')


@dataclass
class Change:
    field: str
    kind: str
    original: str
    replaced: str
    note: str
    applied: bool = True


def convert_credentials(username, password, *, dynamic_country=False, bolt_session=False):
    """返回转换后的账号密码和逐条说明；不改动无把握的缩写。"""
    changes = []
    user = _convert_one(str(username or ''), 'username', changes, dynamic_country, bolt_session)
    secret = _convert_one(str(password or ''), 'password', changes, dynamic_country, bolt_session)
    return {'username': user, 'password': secret, 'changes': [asdict(change) for change in changes]}


def _convert_one(value, field, changes, dynamic_country, bolt_session):
    if not value:
        return value
    parts = SPLIT.split(value)
    handled = set()
    index = 0
    while index < len(parts):
        label = parts[index]
        target_index = index + 2
        if index % 2 or label.lower() not in LABELS or target_index >= len(parts) or not parts[target_index]:
            index += 2
            continue
        separator = parts[index + 1]
        target = parts[target_index]
        original = f'{label}{separator}{target}'
        handled.update((index, target_index))
        lowered = label.lower()
        if target.startswith(('${', '@{')):
            index = target_index + 2
            continue
        replacement = target
        kind = ''
        note = ''
        applied = True
        if lowered in STATE_LABELS:
            if len(target) == 2 and target.isalpha():
                replacement = '${proxyStateCode}'
                kind, note = 'stateCode', f'州缩写 {target.upper()} 按本条数据填写'
            else:
                replacement = '${proxyState}'
                kind, note = 'stateName', f'州名 {target} 按本条数据填写'
        elif lowered in CITY_LABELS:
            replacement, kind, note = '${proxyCity}', 'city', f'城市 {target} 按本条数据填写'
        elif lowered in ZIP_LABELS:
            replacement, kind, note = '${proxyZip}', 'zip', f'邮编 {target} 按本条数据填写'
        elif lowered in SESSION_LABELS:
            replacement = '${session}' if bolt_session else '@{' + str(max(6, min(12, len(target)))) + '}'
            kind, note = 'session', '每次重新生成会话号'
        elif lowered in COUNTRY_LABELS:
            if dynamic_country and len(target) == 2 and target.lower() in COUNTRY_CODES and (target.islower() or target.isupper()):
                replacement = '${countryUpper}' if target.isupper() else '${country}'
                kind, note = 'country', '国家按本条数据填写，保留原来的大小写格式'
            else:
                kind, note, applied = 'country', '国家格式不能安全转换，保持原样；可在确认弹窗中手动修改', False
        if kind:
            parts[target_index] = replacement
            changes.append(Change(field, kind, original, f'{label}{separator}{replacement}', note, applied))
        index = target_index + 2

    for index, part in enumerate(parts):
        if index % 2 or not part or index in handled or part.startswith(('${', '@{')):
            continue
        lowered = part.lower()
        if lowered in STATE_NAMES:
            parts[index] = '${proxyState}'
            changes.append(Change(field, 'stateName', part, parts[index], f'州名 {STATE_NAMES[lowered]} 按本条数据填写'))
        elif lowered in STATE_CODES:
            changes.append(Change(field, 'ambiguous', part, part, '无标签的两位缩写不自动猜测；如果是州，可手动改成 ${proxyStateCode}', False))
        elif lowered in COUNTRY_CODES:
            next_part=parts[index+2].replace(' ', '').lower() if index+2<len(parts) else ''
            if (dynamic_country and lowered in COUNTRY_STATE_NAMES
                    and next_part in COUNTRY_STATE_NAMES[lowered]
                    and (part.islower() or part.isupper())):
                parts[index]='${countryUpper}' if part.isupper() else '${country}'
                changes.append(Change(field, 'country', part, parts[index], '紧邻已识别州名，国家按本条数据填写并保留大小写格式'))
            else:
                changes.append(Change(field, 'country', part, part, '无标签国家代码无法确认，保持原样', False))
    return ''.join(parts)
