"""厂商适配器只按需加载，所有移植依赖来自本项目。"""
from dataclasses import fields, asdict, replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import random
import sys
from .catalog import BY_KEY, validate

RUNTIME = Path(__file__).resolve().parents[1] / 'vendor_runtime'
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

IMPLEMENTATIONS = {
    'octo':('o','O'), 'undetectable':('u','U'), 'morelogin':('morelogin','MoreLogin'),
    'adspower':('adspower','AdsPower'), 'bitbrowser':('bit','Bit'), 'ruyi':('ruyi','Ruyi'),
    'helix':('helix','Helix'), 'kameleo':('kameleo','Kameleo'), 'hidemium':('hidemium','Hidemium'),
    'vmlogin':('vmlogin','VMLogin'), 'hub':('hub','Hub'),
}


def initialize(root):
    os.environ['WINDOW_STUDIO_ROOT'] = str(Path(root).resolve())
    os.environ['WINDOW_STUDIO_CODE'] = str(RUNTIME.parent)
    os.environ['WINDOW_STUDIO_DATA'] = str(Path(root).resolve()/'专属配置')


def build_browser(key, config):
    config = validate(key, config)
    if key == 'chrome':
        from .local_chrome import LocalChrome
        return LocalChrome(config)
    module, cls = IMPLEMENTATIONS[key]
    package = f'server.python.browser_s.{module}_browser_s'
    runtime_type = getattr(importlib.import_module(f'{package}.{module}_browser_runtime_setting'), f'{cls}BrowserRuntimeSetting')
    values = {k:v for k,v in config.items() if k in {f.name for f in fields(runtime_type)}}
    if key == 'adspower':
        values['api_key'] = config['api_token']
    if key == 'ruyi':
        values['profile_root'] = './文件缓存/浏览器/ruyi'
    if key == 'helix' and not values.get('core_version'):
        values.pop('core_version', None)
    runtime = runtime_type(**values)
    if key == 'ruyi':
        from .local_ruyi import ManagedRuyi
        return ManagedRuyi(runtime)
    if key == 'helix':
        return importlib.import_module(f'{package}.helix_manager').create_helix_backend(runtime)
    return getattr(importlib.import_module(f'{package}.{module}_browser'),f'{cls}Browser')(runtime)


def check_proxy_update(key):
    if key not in {'octo','bitbrowser','undetectable','adspower','morelogin','hub','kameleo','vmlogin','chrome','ruyi'}:
        raise ValueError('此厂商暂不支持在原环境自动更新代理；原环境与资料仍保留，可在厂商客户端修改代理并打开该环境')


def update_browser_proxy(browser, identity, proxy):
    """只修改已停止的原环境代理，不发送指纹、Cookie、名称或创建参数。"""
    key=browser.key
    check_proxy_update(key)
    if key in ('chrome','ruyi'):
        return browser.update_proxy(identity,proxy)
    p=proxy
    if key=='octo':
        values={'proxy':{'type':p.protocol,'host':p.host,'port':p.port,'login':p.username,'password':p.password}}
    elif key=='bitbrowser':
        values={'proxyMethod':2,'proxyType':p.protocol,'host':p.host,'port':p.port,
                'proxyUserName':p.username,'proxyPassword':p.password}
    elif key=='undetectable':
        host='['+p.host+']' if ':' in p.host and not p.host.startswith('[') else p.host
        value=f'{p.protocol}://{host}:{p.port}'
        if p.username or p.password:value+=f':{p.username}:{p.password}'
        values={'proxy':value}
    elif key=='adspower':
        values={'user_proxy_config':{'proxy_soft':'other','proxy_type':p.protocol,'proxy_host':p.host,
                'proxy_port':str(p.port),'proxy_user':p.username,'proxy_password':p.password}}
    elif key=='morelogin':
        values={'proxy':{'proxyProvider':{'http':'0','https':'1','socks5':'2','ssh':'3'}[p.protocol],
                'proxyIp':p.host,'proxyPort':p.port,'username':p.username,'password':p.password}}
    elif key=='hub':
        from server.python.browser_s.hub_browser_s.hub_browser_default import HUB_PROXY_TYPES
        if p.protocol not in HUB_PROXY_TYPES:raise ValueError('Hubstudio 不支持此代理协议')
        values={'asDynamicType':'2','proxyTypeName':HUB_PROXY_TYPES[p.protocol],'proxyServer':p.host,
                'proxyPort':p.port,'proxyAccount':p.username,'proxyPassword':p.password}
    elif key=='kameleo':
        if p.protocol not in {'http','socks5','ssh'}:raise ValueError('Kameleo 不支持此代理协议')
        values={'proxy':{'value':p.protocol,'extra':{'host':p.host,'port':p.port,'id':p.username,'secret':p.password}}}
    elif key=='vmlogin':
        values={'proxyServer':{'setProxyServer':True,'type':p.protocol.upper(),'host':p.host,
                'port':str(p.port),'username':p.username,'password':p.password}}
    return browser.update_browser(identity,values)


def browser_fingerprint(browser, config, *, system=None):
    from server.python.browser_s.browser_module_s.browser_fingerprint import BrowserFingerprint
    from server.python.browser_s.browser_task_setting import BrowserProviderSettings, FingerprintSettings
    pairs = [(4,8),(8,16),(8,32),(12,32),(16,64)]
    if browser.key in ('undetectable','morelogin'):pairs=[(2,4),(4,4),(4,8),(8,8)]
    hardware = config.get('hardware','vendor')
    chosen = random.choice(pairs) if hardware == 'balanced' else tuple(map(int,hardware.split('/'))) if '/' in hardware else None
    if system is None:
        from .selection import allocate
        system=allocate(system_weights(config),1,mode=config.get('system_mode','weighted'))[0]
    settings = dict(os_weights={system:1},
                    chrome_versions=config.get('chrome_versions',[]), resolutions=config.get('resolutions',[]),noise=config.get('noise',True),
                    cpu_cores=[chosen[0]] if chosen else [],memory_gb=[chosen[1]] if chosen else [])
    for k in ('fp_source','config_ids','browser_product','fingerprint_ids','language'):
        if k in config:settings[k]=config[k]
    settings['android_brand_weights'] = {k:config.get('android_brand_weights',{}).get(k,1) for k in config.get('android_brands',[])} if config.get('android_mode','brands')=='brands' else {}
    if config.get('android_mode')=='models':settings['android_models']=config.get('android_models',[])
    if config.get('android_mode')!='fixed':settings['android_versions']=config.get('android_versions',[])
    if browser.key == 'ruyi':
        settings.update(ruyi_fingerprint_mode=config['fingerprint_mode'],ruyi_fingerprint_template=config['fingerprint_template'])
    result=BrowserFingerprint().build(browser, BrowserProviderSettings(local_api_url=config.get('local_api_url',''),fingerprint=FingerprintSettings(**settings)))
    if browser.key=='undetectable' and config.get('fp_source','pool')=='pool' and not result.options.get('config_id'):
        raise ValueError('U 账号配置库没有与本次系统匹配的配置；请调整系统范围、刷新配置库，或明确选择“厂商生成”')
    if system=='Android' and config.get('android_mode')=='fixed':
        models=browser.list_device_models()
        model=next((m for m in models if m.value==config['android_model']),None)
        if model is None:raise ValueError('指定 Android 型号不在当前厂商设备库，请刷新并重新选择')
        version=config.get('android_version')
        if version and version not in model.os_versions:raise ValueError('Android 版本与指定型号不匹配')
        result=replace(result,device_model=model.value,os_version=version or (random.choice(model.os_versions) if model.os_versions else ''))
    return result


def system_weights(config):
    if config.get('system_mode')=='fixed':return {config['fixed_system']:1}
    return {k:config.get('system_weights',{}).get(k,1) for k in config.get('systems',['Win/Win11'])}


def device_models(key,config,*,refresh=False,root=None):
    if key not in ('octo','hub'):raise ValueError('此厂商没有可核实的型号库，请使用厂商自动生成环境')
    from .catalog_cache import load
    return load(root or os.environ['WINDOW_STUDIO_ROOT'],key,'android',config,
                lambda:_device_models(key,config),refresh=refresh)


def _device_models(key,config):
    browser=build_browser(key,{**config,'android_mode':'all'})
    from .cleanup import close_adapter
    try:
        models=browser.list_device_models(refresh=True)
        return {'source':'内置型号表（非实时 API）' if key=='hub' else '厂商 API','models':[{'value':m.value,'brand':m.brand,'versions':list(m.os_versions)} for m in models]}
    finally:close_adapter(browser)


def browser_catalog(key,config,section,*,filters=None,refresh=False,root=None):
    if section in ('configs','fingerprints','templates','versions'):
        from .catalog_cache import load
        result=load(root or os.environ['WINDOW_STUDIO_ROOT'],key,'browser-'+section,config,
                    lambda:_browser_catalog(key,config,section,filters=filters,refresh=True),
                    version=json.dumps(filters or {},sort_keys=True),refresh=refresh)
        if key=='kameleo' and section=='fingerprints':
            for item in result['items']:
                if item.get('label')!=item.get('id'):continue
                system=item.get('os') or {};browser=item.get('browser') or {}
                device=item.get('device') or {}
                os_name=str(system.get('family') or '系统')
                os_version=str(system.get('version') or '')
                os_display=(('Windows（内核 '+os_version+'）') if os_name.lower()=='windows' and os_version else os_name+' '+os_version)
                item['label']=' · '.join(part for part in (
                    str(device.get('name') or '').strip(),
                    os_display,
                    str(browser.get('product') or '浏览器')+' '+str(browser.get('version') or browser.get('major') or ''))
                    if part)
        return result
    return _browser_catalog(key,config,section,filters=filters,refresh=refresh)


def _browser_catalog(key,config,section,*,filters=None,refresh=False):
    from server.python.browser_s.browser_module_s.browser_options_catalog import read_browser_catalog
    from .cleanup import close_adapter
    browser=build_browser(key,{**config,'android_mode':'all'} if key in ('octo','hub') else config)
    try:return read_browser_catalog(browser,section,filters=filters,refresh=refresh)
    finally:close_adapter(browser)


def build_cloud(key, config, root):
    from server.python.cloud_phone_s.providers import create_provider
    config = validate(key, config)
    converted = dict(apiKey=config['api_key'],authMode=config.get('auth_mode','token'),appId=config.get('app_id',''),
                     deviceSelection=config['device_selection'],deviceIds=config['device_ids'],managementOnly=True)
    provider = create_provider(key,converted,Path(root)/'专属配置/本机设置')
    # 两家接口各自有实例；不会修改类或其他软件的地址。
    provider.api.BASE=config['api_url']
    from .catalog_cache import load
    provider._catalog_cache_reader=lambda version,fetch,refresh=False:load(root,key,'cloud',config,fetch,version=version,refresh=refresh)
    return provider


def cloud_catalog(key,config,root,section,body):
    from server.python.cloud_phone_s.framework import device_catalog
    provider=build_cloud(key,{**config,'device_mode':'weighted'},root)
    try:
        if section=='systems':return {'items':device_catalog.systems(provider)}
        if section=='models':return device_catalog.models(provider,body.get('version',''),refresh=body.get('refresh',False))
        if section=='detail':return device_catalog.detail(provider,str(body.get('id',''))).public()
        if section=='reset':return device_catalog.reset(provider,str(body.get('id','')),body)
        raise ValueError('云手机目录操作无效')
    finally:provider.close()


def probe(key, config, root):
    if BY_KEY[key]['family'] == 'cloud':
        provider = build_cloud(key,config,root)
        try:
            devices = [d.public() for d in provider.list_devices()]
            return {'ok':True,'message':f'找到 {len(devices)} 台设备','devices':devices}
        finally:provider.close()
    browser = build_browser(key,config)
    from .cleanup import close_adapter
    try:
        result = browser.check_health()
        return {'ok':str(result.state.value if hasattr(result.state,'value') else result.state).lower() in {'online','healthy','ready','available','ok'},
                'message':result.message,'version':result.version}
    finally:close_adapter(browser)


def manager_identity(key, config, store=None):
    from .programs import ensure
    return ensure(key,config,store)
