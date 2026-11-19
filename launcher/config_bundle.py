"""一份可复制的通用 JSON；导入预览从不修改本机身份或运行数据。"""
from copy import deepcopy
import hashlib
import json
import re
import time
import uuid
from .catalog import BY_KEY, validate
from .paths import atomic_json, read_json
from .proxy import validate_sources
from .settings import validate_system
from .storage import Store, Conflict, LOCAL_PROVIDER_FIELDS, SHARED_SYSTEM_FIELDS, shared_default


def snapshot(store):
    return hashlib.sha256(store.paths.shared.read_bytes()).hexdigest()


def validate_shared(raw, store):
    if not isinstance(raw,dict) or raw.get('format')!='window-studio-shared' or raw.get('version')!=1:
        raise ValueError('请选择本软件的“共用配置.json”；格式或版本不受支持')
    allowed=set(shared_default())|{'extensions'}
    if set(raw)-allowed:raise ValueError('共用配置包含未支持的顶层字段')
    result=shared_default()
    for key in ('providers','plans','proxies','weights','services'):
        if not isinstance(raw.get(key),dict):raise ValueError('共用配置缺少有效的 '+key)
    result['proxies']=validate_sources(raw['proxies']);result['proxies']['revision']=0
    for key,doc in raw['providers'].items():
        if key not in BY_KEY or not isinstance(doc,dict) or not isinstance(doc.get('settings'),dict):
            raise ValueError('厂商配置格式不正确：'+str(key))
        values=validate(key,doc['settings'])
        result['providers'][key]={'revision':0,'settings':{k:v for k,v in values.items() if k not in LOCAL_PROVIDER_FIELDS}}
    proxy_ids={s['id'] for s in result['proxies']['sources']}
    for key,doc in raw['plans'].items():
        if not isinstance(key,str) or '-' not in key or not isinstance(doc,dict):raise ValueError('启动方案格式不正确')
        kind,vendor=key.split('-',1);Store.check_kind(kind,vendor)
        settings=doc.get('settings')
        if not isinstance(settings,dict):raise ValueError('启动方案缺少设置：'+key)
        # 复用生产校验，同时不建立临时数据库或触碰正式配置。
        class PlanCheck:
            check_kind=staticmethod(Store.check_kind)
            envelope=staticmethod(Store.envelope)
            def plan(self,*args):return {'settings':{'url':'about:blank','package':'','apk_path':'','launch_label':'','proxy_sources':[],'preserve':False}}
            def proxies(self):return result['proxies']
            def write(self,path,value,revision):return value
        checked=Store.save_plan(PlanCheck(),kind,vendor,{'revision':0,'settings':settings})
        # 本机附件路径不分发到同事电脑。
        checked['settings']['apk_path']=''
        result['plans'][key]={'revision':0,**checked}
    weights=raw['weights'].get('weights',{})
    if not isinstance(weights,dict):raise ValueError('随机比例格式无效')
    for key,value in weights.items():
        Store.check_kind('desktop',key)
        if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=1000:raise ValueError('随机比例应为 0～1000 的整数')
    result['weights']={'revision':0,'weights':weights}
    services={k:v for k,v in raw['services'].items() if k!='revision'}
    if set(services)-SHARED_SYSTEM_FIELDS:raise ValueError('通用服务参数包含本机专属字段')
    normalized=validate_system({**store.system(),**services,'alias':'配置校验'},store.root)
    result['services']={'revision':0,**{k:normalized[k] for k in SHARED_SYSTEM_FIELDS}}
    if 'extensions' in raw:
        if not isinstance(raw['extensions'],dict):raise ValueError('扩展配置格式应为对象')
        result['extensions']=deepcopy(raw['extensions'])
    return result


def export_bundle(store):
    value=deepcopy(store.shared())
    for doc in value['plans'].values():
        doc.get('settings',{}).pop('apk_path',None)
        doc.get('settings',{}).pop('files',None)
    return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')


def preview_bundle(store,content):
    if len(content)>20_000_000:raise ValueError('共用配置超过 20 MB')
    try:raw=json.loads(content.decode('utf-8-sig'))
    except (ValueError,UnicodeError) as exc:raise ValueError('请选择有效的 UTF-8 JSON 配置文件') from exc
    value=validate_shared(raw,store)
    token=uuid.uuid4().hex
    folder=store.paths.cache/'配置导入'
    journal={'value':value,'before':snapshot(store),'created':time.time()}
    atomic_json(folder/(token+'.json'),journal)
    files=[{'name':BY_KEY[k]['name'],'action':'更新' if k in store.shared()['providers'] else '新增'} for k in value['providers']]
    files += [{'name':f"代理来源 {len(value['proxies']['sources'])} 个 / 启动方案 {len(value['plans'])} 个",'action':'替换通用部分'}, {'name':'服务参数与随机比例','action':'更新'}]
    return {'token':token,'files':files,'message':'通用配置将整套替换；操作人、本机程序路径、日志、运行记录和网页存档保留'}


def apply_bundle(store,token):
    if not isinstance(token,str) or not re.fullmatch(r'[a-f0-9]{32}',token):raise ValueError('导入预览已失效，请重新选择文件')
    path=store.paths.cache/'配置导入'/(token+'.json')
    if not path.is_file():raise ValueError('请先预览配置文件')
    journal=read_json(path,{})
    if time.time()-journal.get('created',0)>1800:raise ValueError('导入预览已过期，请重新选择文件')
    with store.lock:
        if snapshot(store)!=journal['before']:raise Conflict('预览后配置已改变，请重新预览；没有覆盖当前配置')
        before=store.shared();value=validate_shared(journal['value'],store)
        # 替换后旧界面持有的所有配置版本失效。
        for section in ('providers','plans'):
            for key,doc in value[section].items():doc['revision']=before[section].get(key,{}).get('revision',0)+1
        for section in ('proxies','weights','services'):value[section]['revision']=before[section].get('revision',0)+1
        value['revision']=before['revision']+1
        atomic_json(store.paths.state/'配置导入前一份.json',before)
        atomic_json(store.paths.shared,value)
        path.unlink()
    return {'message':'已导入共用配置；本机身份与记录保留','count':len(value['providers'])}
