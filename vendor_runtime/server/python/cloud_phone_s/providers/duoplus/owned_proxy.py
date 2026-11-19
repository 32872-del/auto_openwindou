"""每台设备复用本程序创建的专用代理，不改用户原有或其它设备共用的代理。"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from ...framework.contracts import CloudPhoneError


def bind_proxy(provider, device_id, proxy):
    identity=hashlib.sha256((provider.api_key+'\0'+device_id).encode()).hexdigest()
    path=Path(provider.config_root).parent/'运行状态'/'云手机专用代理'/(identity+'.json')
    record={'protocol':proxy['type'],'host':proxy['host'],'port':proxy['port'],
            'user':proxy.get('username',''),'password':proxy.get('password','')}
    owned=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    proxy_id=owned.get('proxyId')
    if proxy_id:
        if owned.get('deviceId')!=device_id:raise CloudPhoneError('专用代理归属记录不一致，未修改代理')
        data=provider.api.request('/api/v1/cloudPhone/list',{'proxy_id':proxy_id,'page':1,'pagesize':100})
        if any(str(d.get('id'))!=device_id for d in data.get('list',[])) or int(data.get('total',0))>1:
            raise CloudPhoneError('本程序的设备专用代理已被其它云手机使用，未改动共享代理；请先在厂商处解除其它设备绑定')
        provider.api.request('/api/v1/proxy/update',{'id':proxy_id,**record,'ip_scan_channel':'ip2location'})
    else:
        data=provider.api.request('/api/v1/proxy/add',{'proxy_list':[{**record,'name':'AutoDrssion-'+device_id}], 'ip_scan_channel':'ip2location'})
        rows=[r for r in data.get('success',[]) if r.get('index')==0 and r.get('id')]
        if len(rows)!=1:raise CloudPhoneError('厂商未确认添加设备专用代理，请核对代理配置',provider=provider.label,device=device_id)
        proxy_id=rows[0]['id']
        path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.'+uuid4().hex+'.tmp')
        try:
            temp.write_text(json.dumps({'deviceId':device_id,'proxyId':proxy_id},ensure_ascii=False),encoding='utf-8')
            temp.replace(path)
        finally:temp.unlink(missing_ok=True)
    result = provider.update(device_id,{'proxy':{'id':proxy_id,'dns':1}})
    return {**result, 'bound_proxy_id': proxy_id}
