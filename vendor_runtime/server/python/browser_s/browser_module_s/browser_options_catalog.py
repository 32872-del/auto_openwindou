"""只读厂商目录契约。固定查询入口，不接受任意 URL 或写操作。"""
from collections.abc import Mapping
from urllib.parse import urlencode

CATALOGS={
    'ruyi':('templates',),
    'undetectable':('configs','groups'),
    'kameleo':('versions','fingerprints','groups'),
    'morelogin':('versions','groups'),
    'octo':('models','groups'),
    'hub':('models','versions','groups'),
    'adspower':('groups',),'bitbrowser':('groups',),'vmlogin':('groups',),
    'hidemium':('groups',),'helix':('groups',),
}

def read_browser_catalog(browser,section,*,filters=None,refresh=False):
    key=browser.key;filters=dict(filters or {})
    if section not in CATALOGS.get(key,()):raise ValueError(f'{key} 没有已接入的 {section} 目录接口；不能把静态填写值当作厂商列表')
    items=[];source='厂商 API';partial=False;note=''
    if section=='templates':
        from ..ruyi_browser_s.ruyi_browser_fingerprint import _read_fingerprint
        source='执行端本机模板目录';errors=[]
        for path in sorted(browser._fingerprint_root().glob('*.txt')):
            if path.stat().st_size>2_000_000:
                errors.append(path.name+' 超过2MB');continue
            try:data=_read_fingerprint(path)
            except (ValueError,OSError) as exc:errors.append(str(exc));continue
            items.append({'id':path.name,'label':path.name,'detail':' · '.join(str(data.get(k,'')) for k in ('font_system','hardwareConcurrency','webglrenderer','width','height'))})
        note='按整套模板使用，系统与硬件不重新拆分随机。'+('无可用模板时请先检测程序，或检查模板目录。' if not items else '')
        if errors:note+=' 跳过无效模板：'+'；'.join(errors[:10])
    elif section=='groups':
        items=[{'id':g.group_id,'label':g.name,'detail':g.group_id} for g in browser.list_groups()]
    elif section=='versions':
        versions=browser.available_core_versions(refresh=refresh) if key=='hub' else browser.available_core_versions()
        items=[{'id':str(v),'label':str(v),'detail':'可用内核'} for v in versions]
        if key=='hub':source='厂商接口 / 本机已安装内核'
    elif section=='configs':
        values=browser.list_fingerprint_configs(refresh=refresh)
        for identity,v in values.items():
            if not isinstance(v,Mapping):continue
            items.append({'id':str(identity),'label':str(identity),'os':str(v.get('os') or ''),
                          'browser':str(v.get('browser') or ''),'detail':' · '.join(str(v.get(k) or '') for k in ('os','browser','screen','webgl')),
                          'useragent':str(v.get('useragent') or '')})
        note='当前账号接口实际返回的可用配置；厂商没有在此回包中区分购买与免费来源。'
    elif section=='fingerprints':
        if set(filters)-{'system','product','version'}:raise ValueError('指纹筛选字段无效')
        system=filters.get('system','Win/Win11');product=filters.get('product','chrome')
        os_family={'Win/Win10':'windows','Win/Win11':'windows','Mac/Intel':'macos','Android':'android'}.get(system)
        if not os_family or product not in ('chrome','edge'):raise ValueError('Kameleo 系统或产品筛选无效')
        version=str(filters.get('version') or '')
        if version and not version.isdigit():raise ValueError('内核主版本应为数字')
        query={'deviceType':'mobile' if system=='Android' else 'desktop','osFamily':os_family,'browserProduct':product}
        if version:query['browserVersion']=version
        values=browser.api.request('GET','/fingerprints?'+urlencode(query),operation='fingerprint_catalog')
        if not isinstance(values,list):raise ValueError('Kameleo 指纹目录返回格式不正确')
        partial=len(values)>=25
        for v in values:
            if not isinstance(v,dict) or not v.get('id'):continue
            os_value=v.get('os') or {};browser_value=v.get('browser') or {}
            device=v.get('device') or {};webgl=v.get('webglMeta') or {}
            os_name={'windows':'Windows','macos':'macOS','android':'Android'}.get(str(os_value.get('family','')).lower(),str(os_value.get('family') or '系统未提供'))
            product_name={'chrome':'Chrome','edge':'Edge'}.get(str(browser_value.get('product','')).lower(),str(browser_value.get('product') or '浏览器未提供'))
            model=str(device.get('name') or '').strip()
            os_version=str(os_value.get('version') or '').strip()
            os_display=os_name+('（内核 '+os_version+'）' if os_name=='Windows' and os_version else ' '+os_version)
            label=' · '.join(x for x in (model,os_display,
                                          product_name+' '+str(browser_value.get('version') or browser_value.get('major') or '').strip()) if x)
            detail=' · '.join(x for x in ('设备：'+model if model else '桌面设备',str(webgl.get('renderer') or '').strip(),
                                          '字体 '+str(v['fontsCount']) if v.get('fontsCount') is not None else '') if x)
            items.append({'id':str(v['id']),'label':label,'system':system,'os':os_value,'browser':browser_value,
                          'device':device,'detail':detail})
        note='厂商每次筛选最多返回 25 条；名称来自实际系统、浏览器和设备字段。Windows 返回内核版本，不能据 10.0 区分 Windows 10 与 11。此列表不代表完整指纹库。'
    elif section=='models':
        values=browser.list_device_models(refresh=refresh)
        items=[{'id':m.value,'label':m.value,'brand':m.brand,'versions':list(m.os_versions),
                'detail':m.brand+' · Android '+', '.join(m.os_versions)} for m in values]
        if key=='hub':source='内置型号表（非实时 API）';note='当前 Hub 适配器提供内置型号表，刷新不会冒充从厂商下载新机型。'
    return {'provider':key,'section':section,'source':source,'items':items,'total':len(items),'partial':partial,'note':note}
