"""独立代理来源，来源选择不依赖执行端或浏览器。"""
import ipaddress
import random
import re
import secrets
import string
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse, urlsplit, urlunsplit
import requests
from . import iproyal
from .location import folded, normalize as normalize_location

_BOLT_COUNTRY_TAG = re.compile(r'(?:^|[-_])country[-_]([a-z]{2})(?=[-_]|$)', re.I)


class CityUnavailableError(ValueError):
    """供应商或地区检测明确表明城市级出口不可用。"""


class ProxyCountryConflict(ValueError):
    """固定国家的代理来源不适用于本行地址。"""


class ProxyDetectionError(ValueError):
    """将脱敏诊断随异常传回调度器，失败时也能落盘。"""
    def __init__(self, message, **diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


_SOCKS_REFUSAL_MESSAGE = 'SOCKS5 网关对 IPv4 和 IPv6 检测连接均返回 0x05（Connection refused），未取得出口 IP'


def _city_unavailable(message):
    value=str(message).lower()
    return bool(re.search(
        r'(?:city|城市|市级).{0,48}(?:not supported|unsupported|not available|unavailable|out of stock|no (?:proxy|ip|stock|inventory)|不支持|不可用|没有|无资源|无库存|无可用|售罄)'
        r'|(?:no|without|unavailable|unsupported).{0,20}city',value))


def _without_city_query_tokens(value):
    """州级降级移除明确表示城市或邮编的 URL 查询参数。"""
    if '?' not in value:return value
    parsed=urlsplit(value)
    removable={'city','town','zip','zipcode','postal','postcode'}
    parts=[]
    for part in parsed.query.split('&'):
        key,sep,item=part.partition('=')
        if sep and key.casefold() in removable:
            continue
        parts.append(part)
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'&'.join(parts),parsed.fragment))


def validate_sources(payload):
    sources = payload.get('sources', [])
    if not isinstance(sources,list) or len(sources)>500:
        raise ValueError('代理来源应为列表，最多 500 项')
    identifiers=set()
    normalized=[]
    for raw in sources:
        if not isinstance(raw,dict):raise ValueError('代理来源格式不正确')
        allowed={'id','name','enabled','protocol','host','port','username','password','source_type','api_endpoint','addresses','weight','geo_format','rotate_url','rotate_wait'}
        if set(raw)-allowed:raise ValueError('代理来源含不支持的字段')
        raw = normalize_source(raw)
        item={**dict(enabled=True,protocol='socks5',username='',password='',source_type='static',api_endpoint='',addresses=[],weight=1,host='',port=1080),**raw}
        if not item.get('id') or item['id'] in identifiers:raise ValueError('代理来源 ID 为空或重复')
        if not isinstance(item['enabled'],bool):raise ValueError('代理启用状态需要开关值')
        if not str(item.get('name','')).strip():raise ValueError('请填写代理别名')
        if item['protocol'] not in {'http','https','socks5'}:raise ValueError('代理协议不受支持')
        if item.get('geo_format','auto') not in ('auto','raw','bolt','nine','iproyal'):raise ValueError('代理地区格式不受支持')
        if item['source_type'] not in ('static','api','list','local','rotate'):raise ValueError('代理来源类型不受支持')
        host=item.get('host','')
        if item['source_type'] in ('static','local') and not host:raise ValueError('请填写代理主机')
        if host and (not isinstance(host,str) or any(c.isspace() for c in host) or any(c in host for c in '/?#')):raise ValueError('代理主机只填有效域名或 IP，不能含空白、路径或查询参数')
        endpoint=item.get('rotate_url','')
        if item['source_type']=='rotate' or (item['source_type']=='api' and endpoint):
            parsed=urlparse(endpoint) if isinstance(endpoint,str) else None
            if not parsed or parsed.scheme not in ('http','https') or not parsed.hostname or any(c.isspace() for c in endpoint):raise ValueError('换 IP 链接需要完整 http(s) 地址')
            delay=item.setdefault('rotate_wait',5)
            if isinstance(delay,bool) or not isinstance(delay,int) or not 0<=delay<=60:raise ValueError('换 IP 后等待时间应为 0～60 秒')
        if item['source_type']=='api':
            parsed=urlparse(item['api_endpoint']) if isinstance(item['api_endpoint'],str) else None
            if not parsed or parsed.scheme not in ('http','https') or not parsed.hostname:raise ValueError('提取接口需要完整 http(s) 地址')
        if not isinstance(item['addresses'],list) or any(not isinstance(s,str) for s in item['addresses']):raise ValueError('代理地址列表格式不正确')
        if item['source_type']=='list' and not item['addresses']:raise ValueError('代理地址列表不能为空')
        if isinstance(item['weight'],bool) or not isinstance(item['weight'],int) or not 0<=item['weight']<=1000:raise ValueError('代理权重应为 0～1000；0 不参与随机')
        if isinstance(item.get('port'),bool) or not isinstance(item.get('port'),int) or not 1<=item['port']<=65535:raise ValueError('代理端口应为 1～65535')
        for k in ('id','name','host','username','password'):
            if not isinstance(item[k],str):raise ValueError(k+' 必须是文本')
        identifiers.add(item['id']);normalized.append(item)
    defaults=payload.get('defaults',[])
    if not isinstance(defaults,list) or any(not isinstance(x,str) for x in defaults) or set(defaults)-identifiers:raise ValueError('全局代理选择含不存在的来源')
    return {'sources':normalized,'defaults':defaults}


def _geo_style(source):
    host = source.get('host', '').lower()
    if iproyal.residential_host(host):
        return 'iproyal'
    style = source.get('geo_format', 'auto')
    if style != 'auto':
        return style
    if 'boltproxy.' in host:
        return 'bolt'
    return 'nine' if any(name in host for name in ('9proxy.', 'niceproxy.')) else 'raw'


def _requested_level(location):
    return 'city' if location.get('city') else 'state' if location.get('state') else 'country'


def _sources_for_location(document, selected, location, geo_level=None):
    candidates = [s for s in document['sources'] if s['id'] in selected and s['enabled']]
    if not candidates:
        raise ValueError('指定的代理来源均未启用；请启用至少一个来源')
    if len(selected) > 1:
        candidates = [s for s in candidates if s.get('weight', 1) > 0]
        if not candidates:
            raise ValueError('所选代理来源的随机权重全部为 0')
    country = normalize_location(location or {})['country'].upper()
    compatible = []
    conflicts = []
    invalid = []
    for source in candidates:
        host = source.get('host', '').lower().rstrip('.')
        bolt = source.get('geo_format') == 'bolt' or host == 'boltproxy.org' or host.endswith('.boltproxy.org')
        royal = _geo_style(source) == 'iproyal'
        fixed = set()
        if royal and source.get('source_type', 'static') == 'static':
            if iproyal.residential_host(host):
                try:
                    iproyal.gateway_protocol(source['port'], source['protocol'])
                except ValueError as exc:
                    invalid.append(exc)
                    continue
            fixed = {iproyal.fixed_country(source.get('password', ''))} - {''}
        elif bolt and source.get('source_type', 'static') == 'static':
            fields = (source.get('username', ''), source.get('password', ''))
            fixed = {code.upper() for field in fields for code in _BOLT_COUNTRY_TAG.findall(field)}
        if country and fixed - {country}:
            conflicts.append('「' + source['name'] + '」固定国家 ' + ', '.join(sorted(fixed)))
            continue
        if royal and geo_level == 'state' and country and country != 'US':
            invalid.append(iproyal.UnsupportedRegion(iproyal.US_STATES_ONLY))
            continue
        compatible.append(source)
    if not compatible and invalid:
        raise invalid[0]
    if not compatible:
        raise ProxyCountryConflict(f'代理来源国家与目标 {country} 不一致：' + '、'.join(conflicts)
                                   + '。请选择支持该国家的来源，或将代理模板的国家值改为 ${country} 后重试')
    return compatible


def choose(document, selected=None, location=None, *, geo_level=None):
    # None 继承全局，空列表明确直连；单个指定不受随机权重影响。
    selected = document.get('defaults',[]) if selected is None else selected
    if not selected:return None
    requested_level = geo_level or _requested_level(location or {})
    candidates = _sources_for_location(document, selected, location, requested_level)
    if len(candidates) == 1:
        raw = dict(candidates[0])
    else:
        raw = dict(random.choices(candidates, weights=[s.get('weight', 1) for s in candidates], k=1)[0])
    from .location import tokens as location_tokens
    style = _geo_style(raw)
    royal_gateway = style == 'iproyal' and raw.get('source_type', 'static') == 'static'
    if royal_gateway:
        geo_level = requested_level
        raw['password'] = iproyal.route_password(raw.get('password', ''), normalize_location(location or {}), geo_level)
    elif geo_level=='state':
        # 从凭据中删除有明确标签的城市/邮编段；模板可能写在密码或列表地址里。
        segment=(r'[-_](?:city|town|zip|zipcode|postal|postcode)[-_][^-_:+/\s]+' if style=='bolt'
                 else r'[-_](?:city|town)[-_]\$\{(?:city|citySlug|proxyCity)\}|[-_](?:zip|zipcode|postal|postcode)[-_]\$\{(?:zip|proxyZip)\}')
        before='\n'.join(str(raw.get(field,'')) for field in ('host','username','password','api_endpoint','rotate_url'))+'\n'+'\n'.join(raw.get('addresses',[]))
        for field in ('username','password'):
            raw[field]=re.sub(segment,'',str(raw.get(field,'')),flags=re.I)
        for field in ('api_endpoint','rotate_url'):
            value=str(raw.get(field,''))
            parsed=urlsplit(value)
            path=re.sub(segment,'',parsed.path,flags=re.I)
            raw[field]=_without_city_query_tokens(urlunsplit((parsed.scheme,parsed.netloc,path,parsed.query,parsed.fragment)))
        raw['addresses']=[re.sub(segment,'',str(value),flags=re.I) for value in raw.get('addresses',[])]
        after='\n'.join(str(raw.get(field,'')) for field in ('host','username','password','api_endpoint','rotate_url'))+'\n'+'\n'.join(raw.get('addresses',[]))
        if any('${'+k+'}' in after for k in ('city','citySlug','proxyCity','zip','proxyZip')):
            raise ValueError('此代理模板没有已验证的州级降级语法，无法安全去掉城市；请为州代理建立独立来源')
        if before!=after and not (any('${'+k+'}' in after for k in ('state','stateCode','stateName','proxyState','proxyStateCode')) or re.search(r'[-_](?:state|st|region|province)[-_][^-_:+\s]+',after,re.I)):
            raise ValueError('城市段已移除，但凭据没有州约束，不能作为州代理；请建立独立的州代理来源')
    if geo_level == 'state':
        location={**(location or {}),'city':'','zip':''}
    if style == 'bolt':
        session = str(secrets.randbelow(900000000) + 100000000)
    elif style == 'iproyal':
        session = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
    else:
        session = secrets.token_hex(6)
    tokens={**location_tokens(location or {},style),'session':session}
    templates='\n'.join(str(raw.get(k,'')) for k in ('host','username','password','api_endpoint','rotate_url'))+'\n'+'\n'.join(raw.get('addresses',[]))
    aliases={'country':('country','countryCode','countryUpper','proxyCountry'),'state':('state','stateCode','stateName','proxyState','proxyStateCode'),'city':('city','citySlug','proxyCity'),'zip':('zip','proxyZip')}
    labels={'country':'国家','state':'州省','city':'城市','zip':'邮编'}
    missing=[labels[k] for k,keys in aliases.items() if (location or {}).get(k) and not any('${'+token+'}' in templates for token in keys)]
    raw['geo_warning']=('当前来源没有'+ '、'.join(missing)+'模板，未向代理供应商指定这些地区；实际出口以检测结果为准') if missing else ''
    def expand(value):
        value=re.sub(r'@\{([1-9]\d?)\}', lambda m:''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(int(m[1]))),str(value))
        for key,val in tokens.items():
            if '${'+key+'}' in value and not str(val).strip():
                raise ValueError('本条数据缺少代理模板需要的地区字段：'+key+'；请补全该行，不会沿用示例地区')
            value=value.replace('${'+key+'}',str(val))
        if '${' in value:raise ValueError('代理模板含未知占位符')
        return value
    raw['host'] = expand(raw['host'])
    if royal_gateway:
        secret, options = iproyal.split_password(raw['password'])
        raw['password'] = secret + expand(options)
    else:
        raw.update(username=expand(raw['username']),password=expand(raw['password']))
    if raw.get('rotate_url') and raw.get('source_type') in ('rotate','api'):
        raw['rotate_url']=expand(raw['rotate_url'])
    if raw.get('source_type')=='list':raw.update(parse_address(expand(random.choice(raw['addresses'])),raw['protocol']))
    elif raw.get('source_type')=='api':
        with requests.Session() as session:
            session.trust_env=False
            response=session.get(expand(raw['api_endpoint']),timeout=(5,15))
            if geo_level=='city' and response.status_code>=400 and _city_unavailable(response.text[:1024]):
                raise CityUnavailableError('供应商提取接口明确返回城市代理不可用（HTTP '+str(response.status_code)+'）')
            response.raise_for_status()
            if len(response.content)>65536:raise ValueError('提取代理响应过大')
            text=response.text.strip()
            if geo_level=='city' and _city_unavailable(text[:1024]):
                raise CityUnavailableError('供应商提取接口明确返回城市代理不可用')
            try:
                obj=response.json()
                if isinstance(obj,dict):
                    obj=obj.get('data',obj)
                    if isinstance(obj,dict):obj=obj.get('list',obj)
                if isinstance(obj,list):obj=obj[0] if obj else ''
                if isinstance(obj,dict):
                    host=obj.get('ip') or obj.get('host');port=int(obj.get('port',0))
                    if not host or not 1<=port<=65535:raise ValueError('接口未返回有效 ip/port')
                    raw.update(_endpoint_from_parts(host,port,raw['protocol']))
                    for field in ('username','password'):
                        if field in obj:raw[field]=expand(obj[field])
                    return raw
                if isinstance(obj,str):text=obj
            except requests.exceptions.JSONDecodeError:pass
            lines=[line.strip() for line in text.splitlines() if line.strip()]
            if not lines:raise ValueError('提取接口未返回代理地址')
            raw.update(parse_address(expand(lines[0]),raw['protocol']))
    return raw


def validate_template_location(document, selected, location):
    """提交前检查所选来源的地区占位符，避免排队后重复失败。"""
    if not selected:return
    from .location import tokens as location_tokens
    labels={'country':'国家','countryCode':'国家','countryUpper':'国家','proxyCountry':'国家',
            'state':'州/省','stateCode':'州/省','stateName':'州/省','proxyState':'州/省','proxyStateCode':'州/省',
            'city':'城市','citySlug':'城市','proxyCity':'城市',
            'zip':'邮编','proxyZip':'邮编'}
    level = _requested_level(location)
    for source in _sources_for_location(document,selected,location,level):
        style = _geo_style(source)
        if style == 'iproyal' and source.get('source_type', 'static') == 'static':
            routed = iproyal.route_password(source.get('password', ''), normalize_location(location), level)
            _, options = iproyal.split_password(routed)
            source = {**source, 'username': '', 'password': options}
        values=location_tokens(location,style)
        template='\n'.join(str(source.get(key,'') or '') for key in ('host','username','password','api_endpoint','rotate_url'))+'\n'+'\n'.join(source.get('addresses',[]))
        missing=[(token,labels[token]) for token in labels if '${'+token+'}' in template and not str(values.get(token,'')).strip()]
        if missing:
            fields='、'.join(dict.fromkeys(label for _,label in missing))
            names='、'.join('${'+token+'}' for token,_ in missing)
            raise ValueError('代理来源「'+str(source.get('name') or source.get('id'))+'」需要'+fields+'（'+names+'）；请在创建窗口选择地区，或补齐表格本行。未加入队列')


def rotate_ip(proxy, *, cancelled=lambda:False, geo_level=None):
    """请求换 IP；响应有完整代理时更新本次副本，随后仍须检测出口。"""
    if proxy.get('source_type') not in ('rotate','api') or not proxy.get('rotate_url'):
        raise ValueError('此来源没有配置链接换 IP')
    if cancelled():raise InterruptedError('已取消换 IP')
    try:
        with requests.Session() as session:
            session.trust_env=False
            with session.get(proxy['rotate_url'],timeout=(5,15),stream=True) as response:
                content=bytearray()
                for block in response.iter_content(4096):
                    if cancelled():raise InterruptedError('已取消换 IP')
                    content.extend(block)
                    if len(content)>65536:raise ValueError('换 IP 接口响应超过 64 KB')
                text=content.decode(response.encoding or 'utf-8',errors='replace').strip()
                if geo_level=='city' and _city_unavailable(text[:1024]):
                    raise CityUnavailableError('供应商换 IP 接口明确返回城市代理不可用（HTTP '+str(response.status_code)+'）')
                response.raise_for_status()
                # 识别明确拒绝；不同厂商的成功代码各异，HTTP 2xx 不能证明出口已改变。
                import json
                try:obj=json.loads(text)
                except ValueError:obj=None
                if isinstance(obj,dict) and (obj.get('success') is False or obj.get('ok') is False or obj.get('status') in ('error','failed') or obj.get('error')):
                    raise ValueError('供应商拒绝换 IP：'+text[:1000])
                if text.casefold() in ('false','error','failed','failure'):raise ValueError('供应商拒绝换 IP：'+text)
                returned=_rotation_response_proxy(obj if obj is not None else text,proxy['protocol'])
                if returned:
                    proxy.update({key:value for key,value in returned.items() if key not in ('username','password') or value})
                elif not proxy.get('host'):
                    raise ValueError('换 IP 接口没有返回可用代理地址，且此来源未配置固定网关')
    except requests.RequestException as exc:
        if isinstance(exc,requests.HTTPError) and exc.response is not None and exc.response.status_code in (400,401,403,404,407):
            raise ValueError('换 IP 链接配置或权限错误（HTTP '+str(exc.response.status_code)+'），未启动环境') from exc
        raise ValueError('换 IP 链接请求失败：'+str(exc)) from exc
    deadline=time.monotonic()+proxy.get('rotate_wait',5)
    while time.monotonic()<deadline:
        if cancelled():raise InterruptedError('已取消换 IP 后等待，未启动环境')
        time.sleep(min(.1,max(0,deadline-time.monotonic())))
    if cancelled():raise InterruptedError('已取消换 IP，未启动环境')
    return {'message':('换 IP 请求返回新代理；实际出口以下方检测结果为准' if returned else '换 IP 请求已返回；沿用当前代理地址，实际出口以下方检测结果为准'),'response':text[:1000]}


def _rotation_response_proxy(value, protocol):
    """沿用原双端的响应优先规则：完整代理可替换端点，状态响应不改端点。"""
    if isinstance(value,list):value=value[0] if value else ''
    if isinstance(value,dict):
        if not any(key in value for key in ('host','ip','proxy','url')):
            for key in ('data','result','proxyData'):
                if key in value:return _rotation_response_proxy(value[key],protocol)
        host=value.get('host') or value.get('ip')
        port=value.get('port')
        if host and port:
            try:port=int(port)
            except (TypeError,ValueError):port=0
            if 1<=port<=65535:
                result=_endpoint_from_parts(host,port,str(value.get('protocol') or value.get('type') or protocol).lower())
                for field,alias in (('username','user'),('password','pass')):
                    data=value.get(field) or value.get(alias)
                    if data:result[field]=str(data)
                return result
        value=value.get('proxy') or value.get('url') or ''
    if isinstance(value,str) and value.strip():
        try:return parse_address(value.strip(),protocol)
        except ValueError:pass
    return None


def _endpoint_from_parts(host,port,protocol):
    host=str(host)
    address=f'[{host}]:{port}' if ':' in host and not host.startswith('[') else f'{host}:{port}'
    return parse_address(address,protocol)


def parse_address(value,protocol='socks5'):
    from urllib.parse import unquote
    if not isinstance(value,str) or not value.strip():raise ValueError('代理地址不能为空')
    value=value.lstrip()
    if any(ord(c)<32 for c in value):raise ValueError('代理地址包含控制字符')
    encoded='://' in value
    if encoded:protocol,value=value.split('://',1)
    protocol=protocol.lower()
    if protocol not in ('socks5','http','https'):raise ValueError('代理协议应为 socks5、http 或 https')
    result={'protocol':protocol}
    credentials=None
    if not encoded and '##' in value:
        parts=value.split('##')
        if len(parts)!=3:raise ValueError('## 代理格式应为 主机:端口##账号##密码')
        value,username,password=parts
        credentials=(username,password)
    elif not encoded and '@' in value:
        left,right=value.split('@',1)
        left_host=re.fullmatch(r'(\[[^\]]+\]|[^:@/\s]+):\d+',left)
        right_host=re.fullmatch(r'(\[[^\]]+\]|[^:@/\s]+):\d+',right)
        looks_like_host=lambda host:host.lower()=='localhost' or '.' in host or host.startswith('[')
        if left_host and (not right_host or (looks_like_host(left_host[1]) and not looks_like_host(right_host[1]))):
            value=left
            credentials=right.partition(':')[::2]
    if not encoded and credentials is None and '@' not in value:
        parts=value.split(':')
        if len(parts)==4 and not parts[1].isdigit() and parts[3].isdigit():
            username,password,host,port=parts
            value=f'{host}:{port}'
            credentials=(username,password)
    # 四段格式先匹配主机和数字端口，密码允许冒号/@；IPv6必须有方括号。
    match=re.fullmatch(r'(\[[^\]]+\]|[^:@/]+):(\d+):([^:]*):(.*)',value)
    if match:
        host,port,user,password=match.groups()
        result.update(username=unquote(user) if encoded else user,password=unquote(password) if encoded else password)
    else:
        if '@' in value:
            auth,value=value.rsplit('@',1)
            user,separator,password=auth.partition(':')
            if not separator:raise ValueError('认证信息请使用 账号:密码@主机:端口')
            result.update(username=unquote(user) if encoded else user,password=unquote(password) if encoded else password)
        match=re.fullmatch(r'(\[[^\]]+\]|[^:@/\s]+):(\d+)/?',value)
        if not match:raise ValueError('代理格式应为 主机:端口、主机:端口:账号:密码 或 协议://账号:密码@主机:端口；IPv6需加[]')
        host,port=match.groups()
    if host.startswith('['):
        try:host=str(ipaddress.IPv6Address(host[1:-1]))
        except ValueError as exc:raise ValueError('IPv6代理地址不正确') from exc
    if not host or any(c.isspace() for c in host) or any(c in host for c in '/?#'):raise ValueError('代理主机不正确')
    if not 1<=int(port)<=65535:raise ValueError('代理端口应为 1～65535')
    result.update(host=host,port=int(port))
    if credentials is not None:result.update(username=credentials[0],password=credentials[1])
    return result


def parse_source(value, protocol='socks5', *, preview=False):
    """完整代理拆分并整理用户名和密码里的地区；预览时返回逐项说明。"""
    parsed=parse_address(value,protocol)
    host=parsed['host'].lower().rstrip('.')
    bolt=host=='boltproxy.org' or host.endswith('.boltproxy.org')
    original={field:parsed.get(field,'') for field in ('username','password')}
    from .proxy_template import convert_credentials
    if iproyal.residential_host(host):
        parsed['geo_format'] = 'iproyal'
        parsed['protocol'] = iproyal.gateway_protocol(parsed['port'], parsed['protocol'], infer='://' not in value)
        converted = iproyal.convert_credentials(original['username'], original['password'])
    else:
        converted=convert_credentials(original['username'],original['password'],dynamic_country=True,bolt_session=bolt)
    if 'kookeey' in host:
        pieces=converted['password'].split('-')
        if len(pieces) in (3,4) and '_' in pieces[1] and re.fullmatch(r'[A-Za-z0-9]{6,}',pieces[2]):
            old_session=pieces[2]
            pieces[2]='@{8}'
            converted['password']='-'.join(pieces)
            converted['changes'].append({'field':'password','kind':'session','original':'-'+old_session,'replaced':'-@{8}','note':'Kookeey 会话号每次重新生成','applied':True})
    parsed.update(username=converted['username'],password=converted['password'])
    if bolt:
        parsed['geo_format']='bolt'
    if any(change['applied'] for change in converted['changes']):
        parsed['template_notice']='已识别账号和密码中的地区或会话段；请核对每处转换，确认后保存。'
    if preview:
        parsed.update(original=original,suggested={field:converted[field] for field in ('username','password')},changes=converted['changes'])
    return parsed


def normalize_source(raw):
    raw=dict(raw)
    host=raw.get('host','')
    if not isinstance(host,str):raise ValueError('代理主机必须是文本')
    complete='://' in host or bool(re.match(r'^(?:\[[^\]]+\]|[^:]+):\d+(?::|$)',host)) or '@' in host
    if raw.get('source_type','static') in ('static','local','rotate') and complete:
        parsed=parse_source(host,raw.get('protocol','socks5'))
        parsed.pop('template_notice',None)
        for key,value in parsed.items():
            inferred_protocol = key == 'protocol' and parsed.get('geo_format') == 'iproyal' and '://' not in host
            if key != 'host' and not inferred_protocol and raw.get(key) not in (None,'',value) and not (key=='geo_format' and raw.get(key)=='auto'):
                raise ValueError(f'完整代理中的{key}与单独填写的值不一致，请先确认要使用哪一份')
            raw[key]=value
    return raw


def resolve_selection(document, request, plan):
    """返回最终 ID 范围与来源层级，空列表始终代表直连。"""
    for level,value in (('本次',request),('环境方案',plan)):
        mode=value.get('proxy_mode','selected' if value.get('proxy_sources') else 'inherit')
        if mode=='direct':return [],level+'直连'
        if mode=='selected':
            ids=value.get('proxy_sources',[])
            if not isinstance(ids,list) or not ids:raise ValueError(level+'选择了指定代理，请至少勾选一个来源')
            known={s['id']:s for s in document['sources']}
            if any(not isinstance(i,str) or i not in known or not known[i]['enabled'] for i in ids):raise ValueError(level+'包含已删除或停用的代理来源')
            return list(dict.fromkeys(ids)),level
        if mode!='inherit':raise ValueError(level+'代理选择方式无效')
    ids=list(document.get('defaults',[]))
    known={s['id']:s for s in document['sources']}
    if any(i not in known or not known[i]['enabled'] for i in ids):
        raise ValueError('全局选择包含已删除或停用的代理来源，请重新选择；不会改用其他来源或直连')
    return ids,'全局'


def proxy_url(proxy):
    auth=(quote(proxy['username'],safe='')+':'+quote(proxy['password'],safe='')+'@') if proxy['username'] or proxy['password'] else ''
    host=proxy['host']
    if ':' in host and not host.startswith('['):host='['+host+']'
    scheme='socks5h' if proxy['protocol']=='socks5' else proxy['protocol']
    return f"{scheme}://{auth}{host}:{proxy['port']}"


def safe_error(error,proxy=None):
    """连接错误可能携带 URL/认证信息，记录前去除凭据。"""
    value=str(error)
    for field in ('username','password','api_endpoint','rotate_url'):
        secret=str((proxy or {}).get(field,'') or '')
        if secret:
            value=value.replace(secret,'[已隐藏]').replace(quote(secret,safe=''),'[已隐藏]')
    return re.sub(r'(https?|socks5h?)://[^\s/]*@',r'\1://[已隐藏]@',value)


def _fetch_ip(proxy, url, timeout):
    if urlparse(url).scheme not in {'http','https'}:raise ValueError('代理检测 URL 不正确')
    with requests.Session() as session:
        session.trust_env=False
        address=proxy_url(proxy)
        response=session.get(url,proxies={'http':address,'https':address},timeout=(min(5,timeout),timeout))
        response.raise_for_status()
        if len(response.content)>65536:raise ValueError('代理检测响应过大')
        try:data=response.json()
        except ValueError:data={'ip':response.text.strip()}
        if not isinstance(data,dict):raise ValueError('代理检测响应不是 IP 对象或地址文本')
        ip=str(data.get('ip') or data.get('query') or '').strip()
        ipaddress.ip_address(ip)
        return {**data,'ip':ip}


def _family_probe(proxy, version, timeout):
    urls={4:('https://api.ipify.org?format=json','https://ipv4.icanhazip.com'),
          6:('https://api6.ipify.org?format=json','https://ipv6.icanhazip.com')}
    failures=[];service_error=False
    socks_refusals=0
    gateway_dns_failures=0
    for url in urls[version]:
        try:
            data=_fetch_ip(proxy,url,timeout)
            address=ipaddress.ip_address(data['ip'])
            if address.version!=version:raise ValueError('检测服务返回的 IP 版本不符')
            return {'ip':str(address),'state':'available','errors':failures}
        except (requests.HTTPError,ValueError) as exc:
            service_error=True;failures.append(f'{urlparse(url).hostname}: {type(exc).__name__}: {safe_error(exc,proxy)}')
        except requests.RequestException as exc:
            gateway = proxy['host'].casefold().rstrip('.')
            if isinstance(exc, requests.exceptions.ProxyError) and f"failed to resolve '{gateway}'" in str(exc).casefold():
                gateway_dns_failures += 1
            if (proxy.get('protocol')=='socks5' and isinstance(exc,requests.ConnectionError)
                    and re.search(r'\b0x05:\s*connection refused\b',str(exc),re.IGNORECASE)):
                socks_refusals+=1
            failures.append(f'{urlparse(url).hostname}: {type(exc).__name__}: {safe_error(exc,proxy)}')
    return {'ip':'','state':'unknown' if service_error else 'unreachable','errors':failures,
            'socks_refused':socks_refusals==len(urls[version]),
            'gateway_dns_failure':gateway_dns_failures==len(urls[version])}


def test(proxy, settings, *, ip_family='any', geo_fields=()):
    """同一份代理凭据分别探测两种出口；双栈不会误标为仅 IPv4。"""
    if ip_family not in ('any','ipv4','ipv6','dual'):raise ValueError('代理 IP 类型无效')
    started=time.monotonic()
    timeout=settings['proxy_timeout']
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='代理出口检测') as pool:
        jobs={v:pool.submit(_family_probe,proxy,v,timeout) for v in (4,6)}
        probes={v:job.result() for v,job in jobs.items()}
    available=[v for v,p in probes.items() if p['ip']]
    errors={f'ipv{v}':p['errors'] for v,p in probes.items() if p['errors']}
    if not available and all(p.get('gateway_dns_failure') for p in probes.values()):
        raise ProxyDetectionError('本机 DNS 无法解析代理网关 '+proxy['host']+'，尚未连接到代理商',
                                  failure_type='gateway_dns',probe_errors=errors)
    if not available and all(p.get('socks_refused') for p in probes.values()):
        raise ProxyDetectionError(_SOCKS_REFUSAL_MESSAGE,
                                  failure_type='socks_connection_refused',probe_errors=errors)
    if not available:raise ProxyDetectionError('两个出口检测均未成功：'+str(errors),probe_errors=errors)
    actual='dual' if len(available)==2 else 'ipv'+str(available[0])
    if ip_family!='any':
        if any(p['state']=='unknown' for p in probes.values()):raise ProxyDetectionError('检测服务返回异常，暂不能确认是否为单栈出口：'+str(errors),probe_errors=errors)
        if ip_family!=actual:raise ProxyDetectionError(f'可用出口为 {actual}，本次要求 {ip_family}',probe_errors=errors)
    main=6 if ip_family=='ipv6' else available[0]
    result={'ip':probes[main]['ip'],'ipv4':probes[4]['ip'],'ipv6':probes[6]['ip'],
            'ip_family':actual,'protocol':{'dual':'IPv4 + IPv6','ipv4':'IPv4','ipv6':'IPv6'}[actual],
            'latency':round((time.monotonic()-started)*1000),'source':proxy['name'],'probe_errors':errors}
    url=settings['proxy_test_url']
    try:
        if urlparse(url).hostname not in {'api.ipify.org','api64.ipify.org','api6.ipify.org'}:
            result['geo']=_fetch_ip(proxy,url,timeout)
        if geo_fields:
            reported=result.get('geo') or {}
            exits=[ipaddress.ip_address(probes[v]['ip']) for v in available]
            reported_ip=ipaddress.ip_address(reported['ip']) if reported else None
            if reported_ip and reported_ip not in exits:
                raise ValueError(f'地区检测请求的出口 {reported_ip} 与先前探测的出口不一致')
            geos=[]
            for ip in exits:
                geo=_geo_fields(reported) if ip==reported_ip else {}
                if any(not geo.get(field) for field in geo_fields):
                    lookup=_fetch_ip(proxy,f'https://api.country.is/{ip}?fields=subdivision,city',timeout)
                    if ipaddress.ip_address(lookup['ip'])!=ip:
                        raise ValueError(f'地区服务未返回所查出口 {ip} 的记录')
                    extra=_geo_fields(lookup)
                    for field,label in (('country','国家'),('state','州/省'),('city','城市')):
                        if geo.get(field) and extra[field] and folded(geo[field])!=folded(extra[field]):
                            reason=f'出口 {ip} 的两份地区回包{label}不一致'
                            raise ProxyDetectionError(reason,geo_conflict_ip=str(ip),probe_errors={**errors,'geo':[reason]})
                    geo={**extra,**{key:value for key,value in geo.items() if value}}
                geos.append({'ip':str(ip),'countryCode':geo.get('country',''),
                             'region':geo.get('state',''),'city':geo.get('city','')})
            result['exit_geos']=geos
            result['geo']=geos[available.index(main)]
    except ProxyDetectionError:
        raise
    except Exception as exc:
        reason=safe_error(exc,proxy)
        raise ProxyDetectionError('代理地区检测失败：'+reason,probe_errors={**errors,'geo':[reason]}) from None
    return result


def _geo_fields(geo):
    # 出口国家必须来自检测服务，不能通过邮编或同名州反推。
    from .location import CA, US
    country=str(geo.get('countryCode') or geo.get('country_code') or geo.get('country') or '').strip().lower()
    country={'us':'us','usa':'us','united states':'us','united states of america':'us','美国':'us',
             'ca':'ca','can':'ca','canada':'ca','加拿大':'ca'}.get(country,country)
    state=str(geo.get('region_code') or geo.get('region') or geo.get('regionName') or geo.get('subdivision') or '').strip()
    if state.upper().startswith(country.upper()+'-'):state=state[len(country)+1:]
    regions={'us':US,'ca':CA}.get(country,{})
    state=next((code for code,name in regions.items() if folded(state) in (folded(code),folded(name))),state)
    return {'country':country,'state':state,'city':str(geo.get('city') or '').strip()}


def _target_fields(location,level):
    fields={'city':('country','state','city'),'state':('country','state'),'country':('country',)}
    target=normalize_location(location)
    return {key:target[key] for key in fields[level] if target[key]}


def check_location(measured,location,level):
    measured['geo_verified']=False
    fields=_target_fields(location,level)
    if not fields:return
    geos=measured.get('exit_geos') or [measured.get('geo') or {}]
    for geo in geos:
        actual=_geo_fields(geo)
        for field,expected in fields.items():
            value=actual[field]
            if not value:
                if field=='city' and 'state' in fields:
                    raise CityUnavailableError(f'出口已确认州/省，但检测服务未返回城市，要求 {expected}')
                label={'country':'国家','state':'州/省','city':'城市'}[field]
                raise ValueError(f'出口{label}未能核实，要求 {expected}')
            if re.sub(r'[^\w]','',folded(value))!=re.sub(r'[^\w]','',folded(expected)):
                raise ValueError(f'出口地区不符：{field} 要求 {expected}，检测为 {value}')
    measured['geo_verified']=True


_check_location=check_location


def measure_location(proxy,settings,location,level,*,ip_family='any',probe=None):
    measured=(probe(proxy,settings,ip_family=ip_family) if probe else
              test(proxy,settings,ip_family=ip_family,geo_fields=tuple(_target_fields(location,level))))
    check_location(measured,location,level)
    if level == 'state' and proxy.get('geo_region_strategy') == 'province_filter':
        measured.update(proxy_region_strategy='province_filter', source=proxy['name']+'-省内筛选',
                        proxy_fallback=bool(location.get('city')),
                        proxy_fallback_reason='复用加拿大候选出口，已重新核验原省份 '+str(location.get('state', '')))
    return measured


def _choose_attempt(document, selected, location, level, *, province_fallback=False):
    """优先供应商地区定向；显式省级回退允许加拿大国家池候选，仍须逐个验省。"""
    try:
        return choose(document, selected, location, geo_level=level)
    except iproyal.UnsupportedRegion:
        target = normalize_location(location)
        if not (province_fallback and level == 'state' and target['country'] == 'ca' and target['state']):
            raise
        ids = document.get('defaults', []) if selected is None else selected
        candidates = _sources_for_location(document, ids, location, 'country')
        ids = [source['id'] for source in candidates
               if _geo_style(source) == 'iproyal' and source.get('source_type', 'static') == 'static']
        if not ids:
            raise
        chosen = choose(document, ids, location, geo_level='country')
        base, options = iproyal.split_password(chosen['password'])
        chosen['password'] = base + re.sub(r'_(city|state)-[^_]+', '', options)
        chosen['geo_request_level'] = 'country'
        chosen['geo_region_strategy'] = 'province_filter'
        chosen['geo_warning'] = 'IPRoyal 未直接指定加拿大省份；从加拿大候选出口中严格核验省份 ' + target['state']
        return chosen


def acquire(document, selected, location, settings, *, ip_family='any', cancelled=lambda:False, on_attempt=lambda *args:None, probe=None, request_rotation=True, purpose='launch', allow_state_fallback=True):
    """每条数据独立预检；成功才返回可挂载代理，失败绝不回退直连。"""
    if ip_family not in ('any','ipv4','ipv6','dual'):raise ValueError('请选择不限、IPv4、IPv6 或双栈')
    if not selected:return None,{}
    initial_level = _requested_level(location)
    errors=[];history=[];last_probe_errors={};conflicting_ips=set()
    # 五次是整条数据的上限，不是每一级必须跑满的次数。
    # 明确无城市立即回退；超时/连接等一般失败最多试两次城市，给州保留三次。
    # 认证、协议和模板错误直接结束，不通过换地区掩盖配置问题。
    level=initial_level
    city_failures=0
    fallback_reason=''
    labels={'city':'城市代理','state':'州代理','country':'国家代理'}
    for attempt in range(1,6):
        label=labels[level]
        if cancelled():raise InterruptedError('已取消代理检测')
        on_attempt(label,attempt,'')
        chosen=None;measured={}
        try:
            chosen=_choose_attempt(document,selected,location,level,
                                   province_fallback=initial_level=='city' and allow_state_fallback)
            rotation=rotate_ip(chosen,cancelled=cancelled,geo_level=level) if request_rotation and chosen.get('source_type') in ('rotate','api') and chosen.get('rotate_url') else None
            measured=measure_location(chosen,settings,location,level,ip_family=ip_family,probe=probe)
            observed={measured['ip'],measured.get('ipv4'),measured.get('ipv6')}
            if conflicting_ips.intersection(observed):
                raise ValueError('本次检测已发现该出口的地区回包冲突，请更换出口')
            if rotation:measured['rotation']=rotation
            if cancelled():raise InterruptedError('已取消代理检测，未启动环境')
            actual=ipaddress.ip_address(measured['ip'])
            expected={'ipv4':4,'ipv6':6}.get(ip_family)
            if expected and actual.version!=expected:raise ValueError(f'实际 IPv{actual.version}，要求 IPv{expected}')
            if measured.get('ip_family') and ip_family!='any' and measured['ip_family']!=ip_family:raise ValueError('代理出口类型不符')
            chosen['geo_level']=level
            measured['source']=chosen['name']+'-'+('省内筛选' if chosen.get('geo_region_strategy')=='province_filter' else label)
            measured['proxy_level']=level
            measured['proxy_attempts']=attempt
            measured['proxy_fallback']=bool(fallback_reason)
            measured['proxy_history']=history
            if chosen.get('geo_region_strategy'):
                measured['proxy_region_strategy']=chosen['geo_region_strategy']
            if fallback_reason:
                chosen['geo_warning']='；'.join(filter(None,[chosen.get('geo_warning',''),'城市代理不可用，已回退到州/省代理：'+str(location.get('state',''))]))
                measured['proxy_fallback_reason']=fallback_reason
            return chosen,measured
        except InterruptedError:raise
        except Exception as exc:
            conflict_ip=getattr(exc,'diagnostics',{}).get('geo_conflict_ip')
            if conflict_ip:conflicting_ips.add(conflict_ip)
            reason=f'{type(exc).__name__}: {safe_error(exc,chosen)}'
            for source in document.get('sources',[]):reason=safe_error(reason,source)
            errors.append(f'{label} {attempt}/5：{reason}')
            def redact(value):
                if isinstance(value,dict):return {k:redact(v) for k,v in value.items()}
                if isinstance(value,list):return [redact(v) for v in value]
                result=safe_error(value,chosen)
                for source in document.get('sources',[]):result=safe_error(result,source)
                return result
            last_probe_errors=redact(getattr(exc,'diagnostics',{}).get('probe_errors',measured.get('probe_errors',{})))
            history.append({'attempt':attempt,'level':level,'error':reason,'probe_errors':last_probe_errors,
                'source_name':redact((chosen or {}).get('name','')),
                'proxy_protocol':(chosen or {}).get('protocol',''),
                'failure_type':getattr(exc,'diagnostics',{}).get('failure_type','proxy_error')})
            on_attempt(label,attempt,reason)
            fatal_http=(isinstance(exc,requests.HTTPError) and exc.response is not None
                        and exc.response.status_code in (400,401,403,404,407))
            if history[-1]['failure_type'] == 'gateway_dns' and len(selected) == 1 and chosen:
                source = next(s for s in document['sources'] if s['id'] == chosen['id'])
                if source.get('source_type', 'static') == 'static' and source['host'] == chosen['host']:
                    break
            if (isinstance(exc,(ProxyCountryConflict,iproyal.UnsupportedRegion,iproyal.ProtocolMismatch)) or fatal_http or 'WRONG_VERSION_NUMBER' in reason or 'CERTIFICATE_VERIFY_FAILED' in reason
                    or re.search(r'(?i)\b(?:401|403|407)\b|authentication (?:failed|required)|invalid credentials|unauthorized|forbidden|余额不足|insufficient balance',reason)
                    or '本条数据缺少代理模板' in reason
                    or '代理模板含未知占位符' in reason
                    or '供应商拒绝换 IP' in reason
                    or '换 IP 链接配置或权限错误' in reason
                    or '没有已验证的州级降级语法' in reason):
                break
            if level=='city' and allow_state_fallback:
                city_failures+=1
                if location.get('state') and (city_failures>=2 or isinstance(exc,CityUnavailableError) or '出口地区不符：city' in reason or _city_unavailable(reason)):
                    level='state';fallback_reason=reason
    if all(h['failure_type']=='socks_connection_refused' for h in history):
        target=' / '.join(filter(None,(str(location.get('country') or '').upper(),location.get('state'),location.get('city'))))
        sources='、'.join(dict.fromkeys(h['source_name'] for h in history if h['source_name']))
        attempts_by_level=Counter(h['level'] for h in history)
        counts='、'.join(f'{labels[level]} {count} 次' for level,count in attempts_by_level.items())
        context='，'.join(filter(None,(sources,target,counts)))
        message=f'本机代理预检（{context}）失败。{_SOCKS_REFUSAL_MESSAGE}。请向代理商核查所选地区的路由和可用出口。'
    else:
        message='；'.join(errors)
    if initial_level == 'city' and not allow_state_fallback:
        target = ' / '.join(filter(None, (str(location.get('country') or '').upper(), location.get('state'), location['city'])))
        if all('503 Service Unavailable' in entry['error'] for entry in history):
            message = '代理网关返回 503，未取得可检测的出口。'
        elif all(entry['error'] == history[-1]['error'] for entry in history) and history[-1]['failure_type'] != 'socks_connection_refused':
            message = history[-1]['error']
        message = f'要求精确匹配 {target}。已尝试 {attempt} 次城市代理，未取得符合条件的出口。' + message
    raise ProxyDetectionError(('代理不可用。' if purpose=='test' else '代理不可用，未启动浏览器或云手机。')+message,
        proxy_attempts=attempt,proxy_level=history[-1]['level'],probe_errors=last_probe_errors,
        proxy_history=history,proxy_fallback=any(h['level']=='state' for h in history) and initial_level=='city',
        proxy_fallback_reason=fallback_reason,proxy_last_error=history[-1]['error'])
