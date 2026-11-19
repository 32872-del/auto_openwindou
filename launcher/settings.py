from pathlib import Path
from urllib.parse import urlsplit

REMOVED_FIELDS={'concurrency','monitor_seconds'}

DEFAULTS={'revision':0,'common_revision':0,'alias':'自动创建窗口',
    'proxy_test_url':'https://api.ipify.org?format=json','proxy_timeout':12,
    'archive_dir':'./专属配置/网页存档','proxy_check_default':True,'website_check_default':False,
    'sync_url':'','sync_alias':'','sync_auto':False,'sync_timeout':10,
    'v2_url':'https://api.capsolver.com','v2_key':'','v2_wait_count':50,
    'cleanup_desktop':True,'cleanup_phone':True,'copy_format':'table','double_click_copy':True,
    'full_errors':True,'site_timeout':180}


def local_path(root,value):
    p=Path(value)
    if p.is_absolute():raise ValueError('保存目录请使用相对路径，例如 ./专属配置/网页存档')
    result=(Path(root)/p).resolve()
    if not result.is_relative_to((Path(root)/'专属配置').resolve()):raise ValueError('保存目录必须位于专属配置内')
    return result


def validate_system(raw,root):
    raw={k:v for k,v in raw.items() if k not in REMOVED_FIELDS|{'operator_required'}}
    if set(raw)-set(DEFAULTS):raise ValueError('系统设置包含未知字段')
    value={**DEFAULTS,**raw}
    for key,default in DEFAULTS.items():
        v=value[key]
        if isinstance(default,bool):
            if not isinstance(v,bool):raise ValueError(f'{key} 需要开关值')
        elif isinstance(default,int):
            if isinstance(v,bool) or not isinstance(v,int):raise ValueError(f'{key} 需要整数')
        else:
            if not isinstance(v,str):raise ValueError(f'{key} 需要文本')
            value[key]=v.strip()
    for key,lo,hi in [('proxy_timeout',1,120),('sync_timeout',1,120),('v2_wait_count',1,100),('site_timeout',10,240)]:
        if not lo<=value[key]<=hi:raise ValueError(f'{key} 应在 {lo}～{hi} 之间')
    if not value['alias']:raise ValueError('请填写软件别名')
    for key in ('sync_url','proxy_test_url','v2_url'):
        if not value[key] and key=='sync_url':continue
        p=urlsplit(value[key])
        if p.scheme not in ('http','https') or not p.hostname or any(c.isspace() for c in value[key]):raise ValueError(f'{key} 应为完整 http(s) 地址')
    value['sync_auto']=bool(value['sync_url'])
    if value['copy_format'] not in ('table','short'):raise ValueError('默认复制格式无效')
    if not value['archive_dir']:raise ValueError('请填写检测存档目录')
    local_path(root,value['archive_dir'])
    return value
