"""GeeLark POST/JSON + traceId，Token/Key鉴权；不重试有副作用的请求。"""
import hashlib, threading, time, uuid
import requests
from ...framework.contracts import CloudPhoneError

class GeeLarkAPI:
    BASE='https://openapi.geelark.cn/open/v1'
    def __init__(self,config,session=None):
        self.config=dict(config);self.session=session or requests.Session()
        self._lock=threading.Lock();self._last={}
    def headers(self):
        trace=str(uuid.uuid4()).upper();result={'traceId':trace}
        key=str(self.config.get('apiKey','')).strip()
        if not key:raise CloudPhoneError('请保存GeeLark API Token或Key')
        if self.config.get('authMode','token')=='key':
            app=str(self.config.get('appId','')).strip()
            if not app:raise CloudPhoneError('GeeLark Key模式还需要AppId')
            stamp=str(int(time.time()*1000));nonce=trace[:6]
            sign=hashlib.sha256((app+trace+stamp+nonce+key).encode()).hexdigest().upper()
            result.update(appId=app,ts=stamp,nonce=nonce,sign=sign)
        else:result['Authorization']='Bearer '+key
        return result
    def request(self,path,body,*,partial=False,version=1):
        if version not in (1,2):raise ValueError('GeeLark API 版本无效')
        with self._lock:
            delay=self._last.get(path,0)+.35-time.monotonic()
            if delay>0:time.sleep(delay)
            self._last[path]=time.monotonic()
        headers=self.headers()
        try:
            base=self.BASE if version==1 else self.BASE.rsplit('/v1',1)[0]+'/v2'
            response=self.session.post(base+path,json=body,headers=headers,timeout=(8,30))
            response.raise_for_status();payload=response.json()
        except (requests.RequestException,ValueError) as exc:
            raise CloudPhoneError(f'{path} 请求失败：{type(exc).__name__}: {exc}',provider='GeeLark') from exc
        if not isinstance(payload,dict):raise CloudPhoneError('厂商响应不是JSON对象',provider='GeeLark')
        code=payload.get('code')
        if code!=0 and not(partial and code in (40006,40009)):
            raise CloudPhoneError(f'{path} [{code}] {payload.get("msg")}；traceId={payload.get("traceId",headers["traceId"])}',provider='GeeLark',code=str(code))
        data=payload.get('data')
        if path=='/phone/brand/list':
            if not isinstance(data,list):raise CloudPhoneError('型号接口未返回列表',provider='GeeLark')
            return data
        data=data or {}
        if not isinstance(data,dict):raise CloudPhoneError('响应data类型不正确',provider='GeeLark')
        return data
