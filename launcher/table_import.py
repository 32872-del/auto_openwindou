"""固定五列或八列粘贴；空格文本按州内城市目录确认地址边界。"""
from functools import lru_cache
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from .location import normalize as normalize_location, US, CA

FIELDS = {
    'name': '名称', 'address': '地址', 'city': '城市', 'state': '州 / 省',
    'zip': '邮编', 'country': '国家', 'url': '打开网址', 'note': '备注',
    'email': '邮箱', 'firstName': '名', 'lastName': '姓', 'phone': '电话',
}
COLUMNS = ['email', 'address', 'city', 'state', 'zip']
FULL_COLUMNS = ['email', 'firstName', 'lastName', 'address', 'city', 'state', 'zip', 'phone']
FORMAT_HINT = '按固定五列（邮箱、地址、城市、州/省、邮编）或固定八列（邮箱、名、姓、地址、城市、州/省、邮编、电话）粘贴。每行一条，不要表头。Excel 用 Tab 分列。纯文本中邮箱后可用一个空格，多词姓名、地址或城市之间请用两个以上空格分列。美国四位邮编自动补零，如 4743 → 04743。加拿大省份使用 ON 等两位代码，邮编可写 M3N 2P8 或 M3N2P8。'
FORMAT_EXAMPLE = 'alice@example.com\t123 Example St\tBoston\tMA\t02108'
FULL_EXAMPLE = 'alice@example.com  ANTHONY  HALL  999 N IOWA ST  OLATHE  KS  66061  9135550110'
US_POSTAL = r'(?:[0-9]{5}(?:-[0-9]{4})?|[0-9]{4})'
CA_POSTAL = r'[A-Za-z][0-9][A-Za-z] ?[0-9][A-Za-z][0-9]'


def clean_spaces(value):
    return re.sub(r'[^\S\r\n\t]+', ' ', value).strip(' ')


@lru_cache(maxsize=1)
def city_names():
    data = json.loads((Path(__file__).parent / 'resources/美加城市目录.json').read_text(encoding='utf-8'))
    return {state['id']: sorted({clean_spaces(city).casefold() for city in state['cities']}, key=len, reverse=True)
            for country in data['countries'] for state in country['states']}


def space_columns(line):
    """单空格没有列边界；只接受已知州内城市，不猜最后一个词是城市。"""
    line = ' '.join(line.split())
    match = re.fullmatch(
        r'(?P<head>.+) (?P<state>[A-Z]{2}) '
        rf'(?P<zip>{US_POSTAL}|{CA_POSTAL})'
        r'(?: (?P<phone>\+?[0-9(][0-9() .-]*))?', line)
    if not match:
        raise ValueError('无法确认州/省、邮编或电话，请检查列顺序')
    extended = match['phone'] is not None
    parts = match['head'].split(' ', 3 if extended else 1)
    if len(parts) != (4 if extended else 2):
        raise ValueError('缺少姓名、地址或城市')
    prefix, address_city = parts[:-1], parts[-1]
    # 多词姓名在纯单空格文本中没有可靠边界，须保留 Tab / 双空格分列。
    if not re.match(r'(?:[0-9]|P\.?O\.? BOX\b|RR\b|HC\b)', address_city, re.I):
        raise ValueError('无法确认姓名与地址边界；姓名含多个词时请用 Tab 或两个以上空格分列')
    folded = address_city.casefold()
    for city in city_names().get(match['state'], []):
        if folded.endswith(' ' + city):
            address = address_city[:-len(city)].strip()
            actual_city = address_city[-len(city):]
            return prefix + [address, actual_city, match['state'], match['zip']] + ([match['phone']] if extended else [])
    raise ValueError('无法确认地址与城市边界；请在两列之间用 Tab 或两个以上空格分隔')


def split_columns(line, number):
    # Excel 的 Tab 是明确列边界；列内空格不能再拆列，错误也不回退猜测。
    line = re.sub(r'[^\S\r\n\t ]', ' ', line).strip(' ')
    if '\t' in line:
        cells = [clean_spaces(cell) for cell in line.split('\t')]
        if len(cells) not in (5, 8):
            raise ValueError(f'第 {number} 行：按 Tab 分隔得到 {len(cells)} 列，需要完整复制 Excel 中的五列或八列，单元格内的空格不会另拆列。' + FORMAT_HINT)
        return cells
    cells = re.split(r' {2,}', line)
    if len(cells) in (4, 7):
        email, _, next_field = cells[0].partition(' ')
        if next_field and is_email(email):
            cells[:1] = [email, next_field]
    if len(cells) in (5, 8):
        cleaned = [clean_spaces(cell) for cell in cells]
        columns = FULL_COLUMNS if len(cells) == 8 else COLUMNS
        try:
            fixed_item(dict(zip(columns, cleaned)), row_number=number, columns=columns)
            return cleaned
        except ValueError:
            pass
    try:
        return space_columns(line)
    except ValueError as exc:
        if len(cells) in (5, 8):
            return [clean_spaces(cell) for cell in cells]
        raise ValueError(f'第 {number} 行：{exc}。' + FORMAT_HINT + '\n五列示例：' + FORMAT_EXAMPLE + '\n八列示例：' + FULL_EXAMPLE) from exc


def is_email(value):
    return bool(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value.strip()))


def validate_item(raw, *, row_number=None):
    if not isinstance(raw, dict):
        raise ValueError('每条表格数据必须是字段对象')
    if set(raw) - set(FIELDS) - {'source_row'}:
        raise ValueError('表格数据包含未知字段')
    source_row = raw.get('source_row', row_number or 1)
    if isinstance(source_row, bool) or not isinstance(source_row, int) or source_row < 1:
        raise ValueError('原始行号无效')
    result = {'source_row': source_row}
    for key, label in FIELDS.items():
        value = raw.get(key, '')
        if not isinstance(value, str):
            raise ValueError(f'第 {source_row} 行：{label}需要文本，邮编不要转成数字')
        value = value.strip()
        limit = 80 if key == 'name' else 2048 if key in ('url', 'note') else 300
        if len(value) > limit:
            raise ValueError(f'第 {source_row} 行：{label}超过 {limit} 字')
        if any(ord(c) < 32 for c in value) and key != 'note':
            raise ValueError(f'第 {source_row} 行：{label}包含换行或控制字符')
        result[key] = value
    if result['country'] and not re.fullmatch(r'[a-zA-Z]{2}', result['country']):
        raise ValueError(f'第 {source_row} 行：国家请使用两位代码，例如 us、ca')
    result['country'] = result['country'].lower()
    try:location=normalize_location(result)
    except ValueError as exc:raise ValueError(f'第 {source_row} 行：{exc}') from exc
    result.update({k:location[k] for k in ('country','state','city','zip')})
    if result['email'] and not is_email(result['email']):
        raise ValueError(f'第 {source_row} 行：邮箱格式不正确，请检查列对应')
    if result['url'] and result['url'] != 'about:blank':
        try:
            parsed = urlsplit(result['url'])
            valid = parsed.scheme in ('http', 'https') and parsed.hostname and not any(c.isspace() for c in result['url'])
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f'第 {source_row} 行：网址请填写完整 http(s) 地址或 about:blank')
    if not any(result.get(key) for key in FIELDS):
        raise ValueError(f'第 {source_row} 行：没有可导入的内容，请检查列对应')
    return result


def fixed_item(raw, *, row_number=1, columns=None):
    """按五列/八列校验；美国四位邮编交由地区规范化补零，不调换列、不猜州名。"""
    number=raw.get('source_row',row_number)
    if columns is None:
        columns = FULL_COLUMNS if any(raw.get(key) for key in ('firstName', 'lastName', 'phone')) else COLUMNS
    for field in columns:
        if not isinstance(raw.get(field),str) or not raw[field].strip():
            raise ValueError(f'第 {number} 行：{FIELDS[field]}不能为空')
    state=raw['state'].strip()
    if state not in US and state not in CA:
        raise ValueError(f'第 {number} 行：州/省请填大写两位代码，例如 MA、NY、ON；请检查列顺序')
    country='us' if state in US else 'ca'
    postal=raw['zip'].strip()
    pattern=US_POSTAL if country=='us' else CA_POSTAL
    if not re.fullmatch(pattern,postal):
        raise ValueError(f'第 {number} 行：邮编格式错误；美国例 02108 或 02108-1234，四位数字自动在前面补零（4743 → 04743）；加拿大例 M5V 3L9')
    if not is_email(raw['email']):
        raise ValueError(f'第 {number} 行：第一列必须是有效邮箱，不能包含表头')
    if is_email(raw['address']) or is_email(raw['city']) or not re.search(r'[A-Za-z]',raw['city']):
        raise ValueError(f'第 {number} 行：请检查地址和城市列；城市请填写英文名称')
    if columns == FULL_COLUMNS:
        for field in ('firstName', 'lastName'):
            if is_email(raw[field]) or not any(c.isalpha() for c in raw[field]) or any(c.isdigit() for c in raw[field]):
                raise ValueError(f'第 {number} 行：{FIELDS[field]}格式错误，请检查列顺序')
        phone = raw['phone'].strip()
        digits = re.sub(r'\D', '', phone)
        if not re.fullmatch(r'\+?[0-9() .-]+', phone) or not (len(digits) == 10 or len(digits) == 11 and digits.startswith('1')):
            raise ValueError(f'第 {number} 行：电话请填写美加 10 位号码，可带国家码 1、空格、括号或连字符')
    return validate_item({**raw,'country':country},row_number=row_number)


def preview(payload):
    text=payload.get('text','')
    if not isinstance(text,str) or not text.strip():raise ValueError('请先粘贴邮箱资料。'+FORMAT_HINT)
    if len(text)>1_000_000:raise ValueError('内容过大，每批最多 100 行。'+FORMAT_HINT)
    if set(payload)-{'text','email_mode'}:
        raise ValueError('粘贴格式已固定，不再接受自动识别、表头或列映射选项。'+FORMAT_HINT)
    if 'email_mode' in payload and payload['email_mode'] is not True:
        raise ValueError(FORMAT_HINT)
    lines=text.lstrip('\ufeff').rstrip('\r\n').splitlines()
    if not 1<=len(lines)<=100:raise ValueError('每批需要 1～100 行。'+FORMAT_HINT)
    rows=[]
    for number,line in enumerate(lines,1):
        cells=split_columns(line,number)
        columns=FULL_COLUMNS if len(cells)==8 else COLUMNS
        raw=dict(zip(columns,cells));raw['source_row']=number
        try:item=fixed_item(raw,row_number=number,columns=columns);error=''
        except ValueError as exc:item=raw;error=str(exc)
        rows.append({'line':number,'cells':cells,'columns':columns[:],'item':item,'error':error,'location':normalize_location(raw,strict=False)})
    groups={}
    for row in rows:
        email=row['item'].get('email','').strip().casefold()
        if email:groups.setdefault(email,[]).append(row)
    for group in groups.values():
        if len(group)>1:
            for row in group:row['error']=row['error'] or '邮箱重复，请只保留一条：第 '+'、'.join(str(r['line']) for r in group)+' 行'
    columns = FULL_COLUMNS if any(row['columns']==FULL_COLUMNS for row in rows) else COLUMNS
    for row in rows:
        row['cells'] = [row['item'].get(key, '') for key in columns]
    return {'columns':columns[:],'fields':FIELDS,'rows':rows,'valid':all(not r['error'] for r in rows),
            'format':'email8' if columns==FULL_COLUMNS else 'email5','format_hint':FORMAT_HINT,
            'example':FULL_EXAMPLE if columns==FULL_COLUMNS else FORMAT_EXAMPLE}
