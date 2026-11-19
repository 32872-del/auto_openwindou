"""云手机只读型号与显式换机契约；普通开机不调用换机。"""
from dataclasses import replace
import re,time

DUO_SYSTEMS={'1':'Android 10','2':'Android 12（区域 A）','3':'Android 11','4':'Android 15','5':'Android 12（区域 B）','10':'Android 15 Pro'}

def normalized_version(value,provider_key=''):
    value=str(value).strip()
    if provider_key=='duoplus':value=DUO_SYSTEMS.get(value,value)
    value=value.casefold().replace('andriod','android').replace('android','').strip()
    region=re.fullmatch(r'12\s*[（(]?\s*(?:区域|region)?\s*([ab])\s*[)）]?',value)
    if region:return '12'+region.group(1)
    return re.sub(r'[（(].*?[）)]','',value).strip()

def systems(provider):
    values=DUO_SYSTEMS if provider.key=='duoplus' else {str(v):'Android '+str(v) for v in range(10,17)}
    return [{'id':k,'label':v} for k,v in values.items()]

def models(provider,version,*,refresh=False):
    if not isinstance(refresh,bool):raise ValueError('刷新型号标记应为开关值')
    reader=getattr(provider,'_catalog_cache_reader',None)
    if reader:return reader(str(version),lambda:_fetch_models(provider,version),refresh=refresh)
    return _fetch_models(provider,version)


def _fetch_models(provider,version):
    version=str(version)
    if version not in {v['id'] for v in systems(provider)}:raise ValueError('请选择此厂商支持的 Android 版本')
    rows=[]
    if provider.key=='duoplus':
        data=provider.api.request('/api/v1/mobile/modelList',{'os':int(version)})
        for brand,values in data.items():
            if not isinstance(values,dict):continue
            for model,info in values.items():
                rows.append({'id':str(model),'label':str(info.get('name') or model),'brand':str(brand),'version':version})
    elif provider.key=='geelark':
        # 官方 Markdown 使用 androidVer；OpenAPI 的 mobileType 文档已落后。
        data=provider.api.request('/phone/brand/list',{'androidVer':int(version)})
        for value in data:
            brand=str(value.get('surfaceBrandName') or '')
            name=str(value.get('surfaceModelName') or '')
            if brand and name:rows.append({'id':name,'label':name,'brand':brand,'version':version})
    else:raise ValueError('此云手机厂商尚未接入型号列表')
    return {'provider':provider.key,'items':rows,'total':len(rows),'source':'厂商 API','systems':systems(provider),
            'canSpecifyResetModel':provider.key=='duoplus',
            'note':'DuoPlus 可显式换为指定型号；GeeLark 已有设备换机只支持保留型号或厂商随机，指定型号只在新建设备 API 开放。'}

def detail(provider,identity,*,device=None):
    device=device or provider.device(identity)
    if provider.key=='duoplus':
        data=provider.api.request('/api/v1/cloudPhone/info',{'image_id':identity})
        device=replace(device,raw={**device.raw,**data})
        # 目录给型号代码，设备详情给营销名称；在同系统、同品牌内对应。
        version=normalized_version(device.raw.get('os',''),'duoplus')
        cache=getattr(provider,'_device_model_catalog_cache',{})
        entry=cache.get(version)
        if entry is None or time.monotonic()-entry[0]>60:
            rows=[]
            for key in DUO_SYSTEMS:
                catalog_version=normalized_version(key,'duoplus')
                if catalog_version==version or (version=='12' and catalog_version in ('12a','12b')):
                    rows.extend(models(provider,key)['items'])
            entry=(time.monotonic(),rows);cache[version]=entry;provider._device_model_catalog_cache=cache
        info=device.public()
        ids={r['id'] for r in entry[1] if r['brand'].casefold()==info['brand'].casefold() and info['model'].casefold() in (r['id'].casefold(),r['label'].casefold())}
        device=replace(device,raw={**device.raw,'_catalog_model_ids':sorted(ids)})
    return device

def reset(provider,identity,options):
    if options.get('acknowledge') is not True:raise ValueError('换机可能改变设备资料，请在界面确认后执行')
    device=provider.device(identity)
    if provider.key=='duoplus':
        if device.status!='running':raise ValueError('DuoPlus 官方要求开机后换机；请先在厂商软件打开设备，再单独执行')
        data_type=options.get('data_type')
        if type(data_type) is not int or data_type not in (1,2,3):raise ValueError('必须明确选择重装、清理应用数据或保留应用数据')
        model=str(options.get('model','')).strip()
        if not model:raise ValueError('请选择目标型号')
        if normalized_version(device.raw.get('os',''),'duoplus')!=normalized_version(options.get('version',''),'duoplus'):
            raise ValueError('所选型号目录与设备系统版本不同；此操作只换型号，请选择设备当前系统的目录')
        # 换机前实时校验操作目标，不使用刷新失败时保留的旧目录。
        available=_fetch_models(provider,options.get('version',''))['items']
        if model not in {m['id'] for m in available}:raise ValueError('指定型号已不在当前版本目录中，请刷新')
        result=provider.api.request('/api/v1/cloudPhone/newPhone',{'image_id':identity,'model':model,'data_type':data_type,'keep_gp':1})
    elif provider.key=='geelark':
        if device.status!='idle':raise ValueError('请先关闭设备及厂商远控，再执行 GeeLark 换机')
        if options.get('model'):raise ValueError('GeeLark 已有设备换机 API 不支持指定型号，未发送错误参数')
        randomize=options.get('randomize',True)
        if not isinstance(randomize,bool):raise ValueError('随机型号需要开关值')
        result=provider.api.request('/phone/newOne',{'id':identity,'changeBrandModel':randomize,
            'keepNetType':True,'keepPhoneNumber':True,'keepRegion':True,'keepLanguage':True},version=2)
    else:raise ValueError('此厂商尚未接入换机')
    return {'accepted':True,'message':'厂商已接受换机请求；设备可能仍在处理，请刷新状态确认完成','result':result}

def matches(device,config,*,provider_key=''):
    info=device.public()
    for field,key in (('brand','device_brands'),('model','device_models'),('system','device_versions')):
        choices=[str(v).casefold() for v in config.get(key,[])]
        if not choices:continue
        actual={str(info.get(field,'')).casefold()}
        if field=='model':
            possible_ids={str(v).casefold() for v in device.raw.get('_catalog_model_ids',[])}
            # 同一营销名可以对应多个型号 ID，必须全部获准才能按名称放行。
            if possible_ids and possible_ids.issubset(choices):continue
        if field=='system':
            actual={normalized_version(v,provider_key) for v in actual}
            if provider_key=='duoplus':
                listed=normalized_version(device.raw.get('os',''),provider_key)
                if listed in ('12a','12b'):actual={listed}
                if actual.intersection(('12a','12b')):actual.add('12')
            choices=[normalized_version(v,provider_key) for v in choices]
        if not actual.intersection(choices):return False
    return True
