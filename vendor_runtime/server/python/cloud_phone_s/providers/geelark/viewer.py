"""GeeLark官方单设备视频页适配；操作全部发送到其隔离Desktop。"""
import json,time
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from ...isolated_viewer import IsolatedViewer,ViewerError
from ...framework.touch_window import TouchWindow

def validate_url(url,device):
    p=urlsplit(url)
    if (p.scheme!='https' or p.hostname not in ('phone.geelark.cn','phone.geelark.com','speedup.geelark.com','speedup.geelark.cn')
        or p.username or p.password or p.port not in (None,443) or parse_qs(p.query).get('id')!=[device]):
        raise ViewerError('GeeLark没有返回匹配本设备的官方控制链接')
    return url

class GeeLarkWindow(TouchWindow):
    @classmethod
    def open(cls,provider,device,name,config_root,url):
        viewer=IsolatedViewer(Path(config_root).parent/'运行状态/云手机查看器',validate_url(url,device),device_identity='geelark:'+device)
        try:
            viewer.start();deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                value=viewer.call('query','return [...document.querySelectorAll("video")].some(e=>e.videoWidth>0&&e.readyState>=2)')
                if value:
                    access=provider.android(device)
                    if access._shell('getprop','ro.gl.serialno').strip()!=device:
                        raise ViewerError('GeeLark查询通道返回的环境ID与本任务不一致')
                    result=cls(viewer,device,name,access.resolution());result.provider=provider
                    result._geometry();return result
                time.sleep(.4)
            raise ViewerError('GeeLark控制页90秒内未取得手机画面')
        except BaseException:viewer.close();raise
    def _geometry(self):
        g=self.viewer.call('query','''
            const all=[...document.querySelectorAll('video')].filter(e=>e.videoWidth&&e.getBoundingClientRect().width);
            if(all.length!==1)return null;
            const e=all[0],r=e.getBoundingClientRect(),s=getComputedStyle(e);
            return {id:new URL(location.href).searchParams.get('id'),x:r.x,y:r.y,w:r.width,h:r.height,
                vw:innerWidth,vh:innerHeight,fit:s.objectFit};''')
        if (not g or g['id']!=self.device or g['x']<0 or g['y']<0 or g['x']+g['w']>g['vw']+1
            or g['y']+g['h']>g['vh']+1 or g['fit']!='fill'):
            raise ViewerError('GeeLark视频区域未就绪、被裁剪或缩放方式改变，未发送输入')
        return g
    def _clear_points(self,points):
        return self.viewer.call('query','''
            const v=[...document.querySelectorAll('video')].find(e=>e.videoWidth&&e.getBoundingClientRect().width);
            const p=JSON.parse(arguments[0]);for(let i=0;i<p.length;i+=2)if(document.elementFromPoint(p[i],p[i+1])!==v)return false;
            return true;''',json.dumps(points))
    def _navigate(self,operation):
        # 系统导航控件也走Win32触摸；不发送adb input keyevent。
        suffix='/home' if operation=='home' else '/back'
        nodes=[e for e in self.provider.elements(self.device) if e['id'].startswith('com.android.systemui:') and e['id'].endswith(suffix)]
        if len(nodes)==1:x,y=nodes[0]['x'],nodes[0]['y']
        else:
            access=self.provider.android(self.device)
            if access._shell('settings','get','secure','navigation_mode').strip()!='0':
                raise ViewerError('当前手机不是三键导航，请设置系统导航模式后重试')
            from ...framework.navigation import navigation_point
            from ...framework.contracts import CloudPhoneError
            deadline=time.monotonic()+3
            while True:
                try:x,y=navigation_point(self.capture_bytes(),operation);break
                except CloudPhoneError:
                    if time.monotonic()>=deadline:raise
                    time.sleep(.25)
        self.operate('tap',{'x':x,'y':y},self.phone_size)
