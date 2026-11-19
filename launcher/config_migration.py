"""用户授权的 auto 配置迁入。只读取列明的配置，不迁任务数据库。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from .catalog import BY_KEY, defaults, validate
from .paths import read_json, atomic_json
from .proxy import validate_sources
from .storage import LOCAL_PROVIDER_FIELDS
from .selection import validate_weights

FILES={'octo':'O','undetectable':'U','morelogin':'MoreLogin','adspower':'AdsPower',
       'bitbrowser':'BitBrowser','ruyi':'Ruyi','helix':'Helix','kameleo':'Kameleo',
       'hidemium':'Hidemium','vmlogin':'VMLogin','hub':'Hub','chrome':'Chrome'}
FIELDS={'programPath':'manager_path','browserPath':'browser_path','localApiUrl':'local_api_url',
        'remoteApiUrl':'remote_api_url','requestTimeoutSeconds':'request_timeout','fingerprintDirectory':'fingerprint_directory',
        'coreRoot':'core_root','coreFamily':'core_family','coreVersion':'core_version','autoDownloadCore':'auto_download_core',
        'humanize':'humanize','downloadBaseUrl':'download_base_url','isLocal':'is_local','appId':'app_id',
        'coreDirectory':'core_directory','groupCode':'group_code'}
REFERENCES={'apiTokenRef':'api_token','apiKeyRef':'api_token','authorizationRef':'authorization',
            'appSecretRef':'app_secret','groupCodeRef':'group_code'}


def prepare(source,store):
    source=Path(source).resolve();files=[];warnings=[];extensions={};retained={};local_retained={}
    def read(relative):
        path=source/relative
        if not path.exists():return {}
        raw=read_json(path,{})
        files.append({'file':relative,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        return raw
    client_creds=read('client/配置/配置文件/客户端凭据.json')
    browser_creds=read('server/配置/配置文件/浏览器凭据.json')
    creds={**client_creds,**browser_creds}
    providers={};paths={}
    for key,name in FILES.items():
        raw=read(f'server/配置/配置文件/浏览器/{name}浏览器.json').get('settings',{})
        if not raw:continue
        values=defaults(key)
        for old,new in FIELDS.items():
            if old in raw and new in values:
                v=raw[old]
                if new=='request_timeout' and isinstance(v,(int,float)):v=int(v)
                if new=='core_version' and isinstance(v,int):v=str(v)
                values[new]=v
        for old,new in REFERENCES.items():
            if old in raw and new in values:
                ref=raw[old];values[new]=creds.get(ref,'')
                if ref and ref not in creds:warnings.append(f'{key} 的 {old} 未找到凭据，保持空白')
        if key=='chrome' and raw.get('browserPath'):values['browser_path']=raw['browserPath']
        providers[key]=validate(key,values)
        # 原始厂商配置保存在专属迁移记录；未接入字段明确列出，不假称已生效。
        unused=sorted(set(raw)-set(FIELDS)-set(REFERENCES)-{'schemaVersion','enabled','monitor','allowSafeStart','programCandidateId','programSha256','programVersion','programPublisher','programSource','programSignatureStatus','programConfirmedAt'})
        if unused:
            extensions[key]={'not_applied':unused}
            local_keys={'profileRoot','iniPath','browserPath','coreRoot','fingerprintDirectory','coreDirectory'}
            retained[key]={k:raw[k] for k in unused if k not in local_keys}
            local_retained[key]={k:raw[k] for k in unused if k in local_keys}
    plan=read('client/状态/任务/固定运行方案.json').get('globalBrowserPlan',{})
    weights={}
    for item in plan.get('providers',[]):
        key={'hubstudio':'hub'}.get(item.get('providerKey'),item.get('providerKey'))
        if key not in providers:continue
        values=providers[key];fp=item.get('fingerprint',{})
        if 'systems' in values:
            options=next(f['options'] for f in BY_KEY[key]['fields'] if f['key']=='systems')
            old_weights=fp.get('os_weights',{})
            mode=fp.get('environment_mode',fp.get('os_mode','weighted'))
            if mode not in ('fixed','weighted','sequential','random'):
                raise ValueError(f'{key} 的旧系统分配模式不受支持：{mode}')
            values['system_mode']='weighted' if mode=='random' else mode
            if mode=='fixed':
                fixed=fp.get('fixed_environment')
                if fixed not in dict(options):raise ValueError(f'{key} 的旧固定系统不受支持：{fixed}；未替换成默认系统')
                values['fixed_system']=fixed
                values['systems']=[fixed]
            elif old_weights:
                values['system_weights']=validate_weights(old_weights,allowed=dict(options))
                values['systems']=list(old_weights)
        if 'android_brands' in values:
            brand_weights=fp.get('android_brand_weights',{})
            if brand_weights:
                values['android_brand_weights']=validate_weights(brand_weights,require_positive=False)
                values['android_brands']=[k for k,v in brand_weights.items() if v>0]
                values['android_mode']='brands' if values['android_brands'] else 'all'
        for old,new in [('android_mode','android_mode'),('android_model','android_model'),('android_version','android_version')]:
            if old in fp and new in values:values[new]=fp[old]
        for name in ('chrome_versions','resolutions','noise','fp_source','browser_product','fingerprint_ids','language'):
            if name in fp and name in values:values[name]=fp[name]
        for old,new in [('ruyi_fingerprint_mode','fingerprint_mode'),('ruyi_fingerprint_template','fingerprint_template')]:
            if old in fp and new in values:values[new]=fp[old]
        providers[key]=validate(key,values);weights[key]=max(0,int(item.get('weight',0)))
    for key,name in [('duoplus','DuoPlus'),('geelark','GeeLark')]:
        raw=read(f'server/配置/配置文件/{name}云手机.json')
        raw=raw.get('settings',raw)
        if not raw:continue
        values=defaults(key)
        for old,new in [('apiKey','api_key'),('authMode','auth_mode'),('appId','app_id'),('deviceSelection','device_selection'),('deviceIds','device_ids'),('maxConcurrent','max_concurrent'),('idleWaitSeconds','idle_wait')]:
            if old in raw and new in values:values[new]=raw[old]
        if not values['device_ids'] and raw.get('deviceId'):values['device_ids']=[raw['deviceId']]
        providers[key]=validate(key,values)
    raw_proxy=read('client/配置/配置文件/代理.json').get('settings',{})
    sources=[];unapplied=[]
    for item in raw_proxy.get('sources',[]):
        source_type='api' if item.get('sourceType')=='api' else 'local' if item.get('sourceType')=='pool' else 'static'
        if source_type=='api' and not item.get('apiEndpoint'):
            unapplied.append({'id':item['sourceKey'],'reason':'原配置未填写提取接口'});continue
        value={'id':item['sourceKey'],'name':item['displayName'],'enabled':item['enabled'],
               'protocol':item.get('protocol','socks5'),'host':item.get('host',''),'port':item.get('port') or 1080,
               'username':creds.get(item.get('usernameSecretRef'),''),'password':creds.get(item.get('passwordSecretRef'),''),
               'source_type':source_type,'api_endpoint':item.get('apiEndpoint',''),'addresses':[],'weight':item.get('weight',1)}
        if source_type=='local':
            value.update(host='127.0.0.1',port=item.get('startPort') or 1080,api_endpoint='')
            warnings.append(f"{item['sourceKey']} 已迁入本机代理端口；旧软件的专用换 IP 接口尚未接入")
        sources.append(value)
    fixed=raw_proxy.get('fixedSource');ids={s['id'] for s in sources}
    selected=[fixed] if fixed in ids else []
    proxies=validate_sources({'sources':sources,'defaults':selected if raw_proxy.get('enabled',True) else []})
    services={'v2_key':client_creds.get('services/captcha/capsolver','')}
    return {'providers':providers,'proxies':proxies,'weights':weights,'services':services,'retained':retained,
            'report':{'source':str(source),'files':files,'providers':list(providers),'proxy_count':len(sources),
                      'warnings':warnings,'unapplied_proxies':unapplied,'unapplied_browser_fields':extensions,'local_unapplied_paths':local_retained}}


def apply(source,store):
    data=prepare(source,store)
    # 迁入前先全部校验；保留新版已有的用户编辑，只有还未保存的厂商自动迁入。
    before=store.shared();report=data['report'];report['kept_existing']=[]
    atomic_json(store.paths.state/'迁入前通用配置.json',before)
    for key,settings in data['providers'].items():
        if key in before['providers'] and before['providers'][key].get('revision',0)>0:
            report['kept_existing'].append(key);continue
        store.save_provider(key,{'revision':store.provider(key)['revision'],'settings':settings})
    existing=store.proxies();known={s['id'] for s in existing['sources']}
    merged=existing['sources']+[s for s in data['proxies']['sources'] if s['id'] not in known]
    store.write('配置/代理来源.json',validate_sources({'sources':merged,'defaults':existing['defaults'] or data['proxies']['defaults']}))
    if not store.read('配置/随机比例.json',{}).get('weights'):store.write('配置/随机比例.json',{'weights':data['weights']})
    service=store.read('配置/服务.json',{})
    service.update({k:v for k,v in data['services'].items() if not service.get(k)})
    store.write('配置/服务.json',service)
    common=store.shared()
    common.setdefault('extensions',{})['auto_import']={'unapplied_browser_parameters':data['retained']}
    common['revision']+=1
    atomic_json(store.paths.shared,common)
    atomic_json(store.paths.local/'配置迁入记录.json',report)
    return report
