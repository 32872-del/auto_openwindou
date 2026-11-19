"""每厂商一份连接配置；调度规则独立一份。保存沿用版本检查及原子替换。"""
from __future__ import annotations
import hashlib, json, re, threading, uuid
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .contracts import CloudPhoneError

PROVIDERS={'duoplus':'DuoPlus','geelark':'GeeLark'}

class AppPreparation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    package: str=Field(min_length=3,max_length=200,pattern=r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$')
    apkPath: str=Field(default='',max_length=2000)
    launchSelector: str=Field(default='',max_length=300)

class RunPolicy(BaseModel):
    model_config=ConfigDict(extra='forbid')
    provider: str='duoplus'
    maxConcurrent: int=Field(default=1,ge=1,le=32)
    idleWaitSeconds: int=Field(default=300,ge=5,le=3600)
    closeAfterRun: bool=True
    useTaskProxy: bool=False
    ipEnvironment: bool=False
    renamePattern: str=Field(default='',max_length=200)
    apps: list[AppPreparation]=Field(default_factory=list,max_length=20)

    @model_validator(mode='after')
    def validate_provider(self):
        if self.provider not in PROVIDERS:raise ValueError('不支持的云手机厂商')
        if self.provider!='duoplus' and self.ipEnvironment:
            raise ValueError('此云手机厂商暂不支持不重置数据的按IP更新环境，请关闭该设置')
        return self

class ProviderSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    apiKey: str=Field(default='',max_length=1000)
    authMode: str='token'
    appId: str=Field(default='',max_length=200)
    deviceId: str=Field(default='',max_length=100)
    deviceName: str=Field(default='',max_length=100)
    deviceIds: list[str]=Field(default_factory=list,max_length=200)
    deviceSelection: str='auto'
    revision: str=''

    @model_validator(mode='after')
    def validate_values(self):
        if self.authMode not in ('token','key'):raise ValueError('鉴权方式必须为token或key')
        if self.deviceSelection not in ('auto','selected'):raise ValueError('设备选择方式无效')
        for value in self.deviceIds+([self.deviceId] if self.deviceId else []):
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):raise ValueError('设备ID格式无效')
        return self

def allowed_devices(config):
    ids=list(dict.fromkeys(config.get('deviceIds') or ([config.get('deviceId')] if config.get('deviceId') else [])))
    if not ids and config.get('deviceSelection')=='selected':
        raise CloudPhoneError('尚未勾选任务设备，请在执行端“云手机”选择至少一台设备并保存')
    return ids

class CloudSettings:
    _lock=threading.RLock()
    def __init__(self, root):self.root=Path(root)/'配置文件'
    def path(self, provider=None):
        if provider is None:return self.root/'云手机运行.json'
        if provider not in PROVIDERS:raise CloudPhoneError('不支持的云手机厂商：'+str(provider))
        return self.root/(PROVIDERS[provider]+'云手机.json')
    def read(self,provider=None):
        with self._lock:
            p=self.path(provider);raw=p.read_bytes() if p.exists() else b''
            data=json.loads(raw.decode('utf-8-sig')) if raw else {}
            model=ProviderSettings if provider else RunPolicy
            result=model.model_validate(data).model_dump()
            result['revision']=hashlib.sha256(raw).hexdigest()
            return result
    def save(self,value,provider=None):
        with self._lock:
            if value.get('revision')!=self.read(provider)['revision']:
                raise CloudPhoneError('配置已在别处修改，请重新读取后保存')
            data={k:v for k,v in value.items() if k!='revision'}
            model=ProviderSettings if provider else RunPolicy
            data=model.model_validate(data).model_dump(exclude={'revision'})
            p=self.path(provider);p.parent.mkdir(parents=True,exist_ok=True)
            temp=p.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            try:temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(p)
            finally:temp.unlink(missing_ok=True)
            return self.read(provider)
