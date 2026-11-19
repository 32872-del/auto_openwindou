"""DuoPlus官方分享页到隔离Win32窗口的适配，不使用ADB输入。"""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .isolated_viewer import IsolatedViewer, ViewerError
from .framework.touch_window import TouchWindow


def connection_path(config_root, api_key, device):
    identity = hashlib.sha256((api_key + '\0' + device).encode()).hexdigest()[:24]
    return Path(config_root).parent / '运行状态' / '云手机隔离连接' / (identity + '.json')


def validate_connection(url, device):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('my.duoplus.net', 'my.duoplus.cn')
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.path != '/share' or parse_qs(parsed.query).get('id') != [device]
            or not parse_qs(parsed.fragment).get('password', [''])[0]):
        raise ViewerError('DuoPlus未返回本设备的有效官方带密码链接，未打开窗口')
    return url


def share_connection(client, device, config_root):
    path = connection_path(config_root, client.api_key, device)
    if path.exists():
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        if record.get('deviceId') != device:
            raise ViewerError('云手机隔离连接记录与设备不一致，请核对本机配置')
        return validate_connection(record.get('url', ''), device)
    # 厂商自动生成密码，避免文档8-20位约定与实际自定义校验不一致。
    # 不授予重置、恢复出厂、改参数、云盘、命令行权限。
    response = client.request('/api/v1/cloudPhone/share', {'share': [{
        'image_ids': [device], 'config': {'share_status': 1, 'share_phone_type': 1, 'share_auth': []}}]})
    url = validate_connection(response.get(device, ''), device)
    record = {'deviceId': device, 'url': url,
              'password': parse_qs(urlsplit(url).fragment)['password'][0], 'shareAuth': []}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return url


class DuoPlusIsolatedWindow(TouchWindow):
    @classmethod
    def open(cls, client, device, name, config_root):
        url = share_connection(client, device, config_root)
        root = Path(config_root).parent / '运行状态' / '云手机查看器'
        viewer = IsolatedViewer(root, url, device_identity=device)
        try:
            viewer.start()
            # 分享链接的密码由官方页面从URL片段读取，不向DOM写值。
            access_deadline = time.monotonic() + 8
            ready = None
            while ready is None and time.monotonic() < access_deadline:
                ready = viewer.call('query', '''
                if(location.pathname!='/share')return null;
                const buttons=[...document.querySelectorAll('button')].filter(e=>e.offsetWidth&&e.offsetHeight);
                const input=document.querySelector('input');
                if(buttons.length!==1||!input||!input.value)return null;
                const r=buttons[0].getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2];''')
                if ready is None:
                    time.sleep(.2)
            if ready is None:
                raise ViewerError('DuoPlus分享访问页未加载完成或访问密码缺失，请重新打开')
            viewer.call('tap', *ready)
            deadline = time.monotonic() + 50
            last = ''
            while time.monotonic() < deadline:
                state = viewer.call('query', '''
                    const c=document.querySelector('#redPhone');const r=c?.getBoundingClientRect();
                    return {path:location.pathname,id:new URL(location.href).searchParams.get('id'),
                      text:document.body.innerText,ready:!!(c&&c.width&&c.height&&r.width&&r.height&&
                        document.querySelector('button svg.lucide-home'))};''')
                last = state.get('text', '')
                if state.get('path') == '/control' and state.get('id') == device and state.get('ready'):
                    screen = client.screen(device)
                    result = cls(viewer, device, name, (screen['width'], screen['height']))
                    result._verify_picture(screen)
                    return result
                if any(text in last for text in ('The connection has been disconnected', '连接已断开', '密码错误', 'Invalid password')):
                    raise ViewerError('DuoPlus控制画面连接失败：' + last[:700])
                time.sleep(.4)
            raise ViewerError('DuoPlus控制画面50秒内未连接完成：' + last[:700])
        except BaseException:
            viewer.close()
            raise

    def _geometry(self):
        value = self.viewer.call('query', '''
            const c=document.querySelector('#redPhone'),r=c?.getBoundingClientRect();
            const id=new URL(location.href).searchParams.get('id');
            if(!c||!r||!r.width||!r.height)return null;
            return {id,path:location.pathname,x:r.x,y:r.y,w:r.width,h:r.height,
              vw:innerWidth,vh:innerHeight};''')
        if (not value or value['id'] != self.device or value['path'] != '/control'
                or value['x'] < 0 or value['y'] < 0
                or value['x']+value['w'] > value['vw']+1
                or value['y']+value['h'] > value['vh']+1):
            raise ViewerError('云手机画面未就绪、已换设备或被窗口裁剪，未发送输入')
        if abs(value['w']/value['h'] - self.phone_size[0]/self.phone_size[1]) > .015:
            raise ViewerError('云手机画面方向已改变，请重新打开隔离窗口')
        return value

    def _clear_points(self,points):
        return self.viewer.call('query', """
            const c=document.querySelector('#redPhone'),p=JSON.parse(arguments[0]);
            for(let i=0;i<p.length;i+=2)if(document.elementFromPoint(p[i],p[i+1])!==c)return false;
            return true;""",json.dumps(points))

    def _navigate(self,operation):
        selector='svg.lucide-home' if operation=='home' else 'svg.lucide-undo2'
        point=self.viewer.call('query', """
            const e=document.querySelector(arguments[0])?.closest('button');if(!e)return null;
            const r=e.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2];""",selector)
        if point is None:raise ViewerError('没有找到DuoPlus导航按钮')
        self.viewer.call('tap',*point)
