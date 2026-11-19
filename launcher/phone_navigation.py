"""手机启动页：只读控件树找图标/地址栏，操作走隔离查看器 Win32。"""
import time


def prepare(provider,device,window,plan,kind):
    package=plan['package'];label=plan.get('launch_label','')
    def tap(node):
        if not node.get('enabled',True):raise ValueError('目标控件暂不可用')
        return window.operate('tap',{'x':node['x'],'y':node['y']},window.phone_size)
    def find(predicate,seconds=8):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            matches=[n for n in provider.elements(device) if predicate(n) and n.get('enabled',True) and n['bounds'][2]>n['bounds'][0] and n['bounds'][3]>n['bounds'][1]]
            if matches:return matches[0]
            time.sleep(.5)
        return None
    nodes=provider.elements(device)
    if not any(n.get('package')==package for n in nodes):
        if not label:raise ValueError('请填写 APP 在手机桌面显示的名称，用于 Win32 点击启动')
        window.operate('home',{},window.phone_size)
        node=find(lambda n:label in (n.get('text'),n.get('description')))
        if not node:raise ValueError(f'手机桌面未找到“{label}”；请将入口放到首页，或修正启动名称')
        tap(node)
        if not find(lambda n:n.get('package')==package):raise ValueError('点击图标后未确认指定软件打开，请查看隔离画面')
    if kind=='phone' and plan['url']!='about:blank':
        node=find(lambda n:n.get('id') in (package+':id/url_bar',package+':id/search_box_text'))
        if not node:raise ValueError('未找到手机浏览器地址栏，可能停在首次使用提示；请打开画面处理')
        tap(node)
        window.operate('clearText',{},window.phone_size)
        window.operate('text',{'text':plan['url']},window.phone_size)
        window.operate('enter',{},window.phone_size)
