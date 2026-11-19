"""本机最后使用的界面选项；不保存粘贴资料或运行任务。"""
from copy import deepcopy

FIELDS={
    'create':{'kind','provider','name','count','target','country','state','city','zip','proxy_mode','proxy_sources',
              'desktop_proxy_mode','desktop_proxy_sources','phone_proxy_mode','phone_proxy_sources',
              'ip_family','network_mode','website_test','tls_test','cleanup','system_family','windows_mode','win10_weight','win11_weight'},
    'workspace':{'page','kind','key'},
    'paste':{'delimiter','orientation','layout'},
    'panels':set(),
}


def read(store):
    return store.read('本机设置/界面偏好.json',{'revision':0,'create':{},'workspace':{},'paste':{},'panels':{}})


def save(store,body):
    if not isinstance(body,dict) or set(body)-set(FIELDS):raise ValueError('界面偏好字段不支持')
    for section,values in body.items():
        if not isinstance(values,dict) or len(values)>200:raise ValueError('界面偏好格式不正确')
        if section!='panels' and set(values)-FIELDS[section]:raise ValueError('界面偏好不保存表格资料或任务内容')
        for k,v in values.items():
            if not isinstance(k,str) or len(k)>100:raise ValueError('界面偏好名称过长')
            if section=='panels':
                if not isinstance(v,bool):raise ValueError('折叠状态应为开关')
            elif k in ('proxy_sources','desktop_proxy_sources','phone_proxy_sources'):
                if not isinstance(v,list) or len(v)>500 or any(not isinstance(x,str) or len(x)>100 for x in v):raise ValueError('代理范围不正确')
            elif k in ('desktop_proxy_mode','phone_proxy_mode'):
                if v not in ('inherit','selected','direct'):raise ValueError('代理使用方式无效')
            elif k in ('website_test','tls_test','cleanup'):
                if not isinstance(v,bool):raise ValueError('偏好开关格式不正确')
            elif k=='count':
                if isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=100:raise ValueError('创建数量应为 1～100')
            elif k in ('win10_weight','win11_weight'):
                if isinstance(v,bool) or not isinstance(v,int) or not 0<=v<=1000:raise ValueError('系统比例应为 0～1000 的整数')
            elif k=='system_family':
                if v not in ('win','mac','android'):raise ValueError('系统选择应为 win / mac / android')
            elif k=='windows_mode':
                if v not in ('random','weighted'):raise ValueError('系统分配应为随机或比例模式')
            elif k=='network_mode':
                if v not in ('inherit','keep','wifi','mobile'):raise ValueError('云手机联网方式无效')
            elif not isinstance(v,str) or len(v)>2000:raise ValueError('偏好文本过长或格式不正确')
    with store.lock:
        value=read(store)
        for section,changes in body.items():value[section]={**value.get(section,{}),**deepcopy(changes)}
        return store.write('本机设置/界面偏好.json',{k:v for k,v in value.items() if k!='revision'},value['revision'])
