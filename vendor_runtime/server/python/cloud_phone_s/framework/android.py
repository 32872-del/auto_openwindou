"""共用私有ADB连接：控件/截图查询、文件与应用管理，不提供触摸/键盘输入。"""
from __future__ import annotations
import base64, hashlib, os, re, shlex, socket, struct, subprocess, threading, time
import xml.etree.ElementTree as ET
from pathlib import Path
from .contracts import CloudPhoneError

def parse_ui(raw, *, mark_web_content=False):
    try:root=ET.fromstring(raw[raw.index('<'):])
    except (ValueError,ET.ParseError) as exc:raise CloudPhoneError('手机未返回有效控件树') from exc
    web_nodes = set()
    hidden_nodes = set()
    if mark_web_content:
        for view in root.iter('node'):
            if view.get('visible-to-user') == 'false':
                hidden_nodes.update(view.iter('node'))
            if view.get('class') == 'android.webkit.WebView' and view.get('package') == 'com.android.chrome':
                web_nodes.update(view.iter('node'))
    result=[]
    for e in root.iter('node'):
        m=re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]',e.get('bounds',''))
        if not m:continue
        l,t,r,b=map(int,m.groups())
        if r<l or b<t:continue
        if mark_web_content and (e in hidden_nodes or r == l or b == t):
            continue
        result.append({'id':e.get('resource-id',''),'text':e.get('text',''),'package':e.get('package',''),
            'description':e.get('content-desc',''),'class':e.get('class',''),
            **{k:e.get(k)=='true' for k in ('clickable','checkable','checked','selected','focused','focusable','password','scrollable','long-clickable')},
            'enabled':e.get('enabled')!='false','bounds':[l,t,r,b],'x':(l+r)//2,'y':(t+b)//2})
        if mark_web_content:
            result[-1]['web_content'] = e in web_nodes
    return result

class AndroidAccess:
    def __init__(self,provider,device):
        from server.python.utils_s.path_utils import get_server_paths
        self.provider,self.device=provider,device
        paths=get_server_paths()
        self.adb=next((p for p in (paths.bundled_resource_root/'tools/platform-tools/adb.exe',paths.code_root/'tools/platform-tools/adb.exe') if p.is_file()),None)
        if self.adb is None:raise CloudPhoneError('缺少tools/platform-tools/adb.exe，请完整更新执行端依赖')
        identity=hashlib.sha256((provider.key+'\0'+device).encode()).hexdigest()[:24]
        state=paths.state_root/'云手机查询'/identity;state.mkdir(parents=True,exist_ok=True)
        self.env=dict(os.environ,ANDROID_USER_HOME=str(state),ADB_VENDOR_KEYS=str(state/'adbkey'))
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.endpoint=None;self.address='';self.started=False;self.lock=threading.RLock()

    def call(self,*args,binary=False,timeout=25):
        try:
            result=subprocess.run([str(self.adb),'-P',str(self.port),*map(str,args)],env=self.env,
                capture_output=True,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired as exc:
            raise CloudPhoneError(f'设备管理/查询超过{timeout}秒，未重复发送操作',
                provider=self.provider.label,device=self.device,stage='ADB管理/查询') from None
        if result.returncode:
            detail=(result.stderr or result.stdout).decode('utf-8','replace').strip()
            raise CloudPhoneError(detail,provider=self.provider.label,device=self.device,stage='ADB管理/查询')
        return result.stdout if binary else result.stdout.decode('utf-8','replace').strip()

    def _shell(self,*args,timeout=25):
        result=self.call('-s',self.address,'shell',shlex.join(map(str,args)),timeout=timeout)
        if args and args[0]!='glogin' and 'you should run glogin to login first' in result.lower():
            # GeeLark的ADB短暂重连后会要求重新鉴权，拒绝信息代表命令尚未执行。
            with self.lock:
                endpoint=self.provider.open_adb(self.device)
                if endpoint.address!=self.address or not endpoint.password:
                    raise CloudPhoneError('云手机ADB地址已变更，需要重新打开任务窗口',provider=self.provider.label,device=self.device)
                response=self.call('-s',self.address,'shell',shlex.join(['glogin',endpoint.password]),timeout=10)
                if 'error' in response.lower() or 'fail' in response.lower():
                    raise CloudPhoneError('云手机ADB重新鉴权失败',provider=self.provider.label,device=self.device)
                result=self.call('-s',self.address,'shell',shlex.join(map(str,args)),timeout=timeout)
                if 'you should run glogin to login first' in result.lower():
                    raise CloudPhoneError('云手机ADB重新鉴权后仍拒绝访问',provider=self.provider.label,device=self.device)
        return result

    def start(self):
        try:
            self.endpoint=self.provider.open_adb(self.device)
            self.address=self.endpoint.address
            if not re.fullmatch(r'[A-Za-z0-9.-]+:\d{1,5}',self.address):raise CloudPhoneError('厂商未返回有效ADB地址')
            self.call('start-server');self.started=True
            result=self.call('connect',self.address)
            if 'connected to' not in result:raise CloudPhoneError('无法连接设备ADB：'+result+'；请核对厂商ADB白名单')
            if self.endpoint.password:
                result=self._shell('glogin',self.endpoint.password)
                if 'fail' in result.lower() or 'error' in result.lower():raise CloudPhoneError('GeeLark设备ADB认证失败')
            self._shell('getprop','ro.build.version.sdk')
            return self
        except BaseException as exc:
            try:self.close()
            except Exception as cleanup:exc.add_note('连接清理：'+str(cleanup))
            raise

    def elements(self):
        with self.lock:
            path='/sdcard/autodrssion-ui.xml'
            for attempt in range(2):
                try:
                    response=self._shell('uiautomator','dump',path,timeout=20)
                    if path not in response or 'error' in response.lower():
                        raise CloudPhoneError('本次控件树未生成，未读取上次缓存：'+response,provider=self.provider.label,device=self.device)
                    return parse_ui(self._shell('cat',path))
                except CloudPhoneError as exc:
                    # 只重查没有副作用的控件树；输入、安装、上传不在此处重发。
                    if attempt or 'device offline' not in str(exc).lower():raise
                    time.sleep(.5)

    def screen(self):
        content=self.call('-s',self.address,'exec-out','screencap','-p',binary=True,timeout=90)
        if not content.startswith(b'\x89PNG\r\n\x1a\n') or not 24<=len(content)<=12_000_000:
            raise CloudPhoneError('手机截图格式或大小无效')
        w,h=struct.unpack('>II',content[16:24])
        if not 1<=w<=10000 or not 1<=h<=10000:raise CloudPhoneError('手机截图尺寸无效')
        return {'image':'data:image/png;base64,'+base64.b64encode(content).decode(),'width':w,'height':h}

    def resolution(self):
        matches=re.findall(r'(?:Physical|Override) size:\s*(\d+)x(\d+)',self._shell('wm','size'))
        if not matches:raise CloudPhoneError('未取得手机实际分辨率')
        return tuple(map(int,matches[-1]))

    def push_file(self,path):
        path=Path(path).resolve(strict=True)
        if not path.is_file():raise CloudPhoneError('上传源不是文件：'+str(path))
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        name=re.sub(r'[^\w. -]','_',path.name)
        remote='/sdcard/Download/AF_'+digest[:12]+'_'+name
        with self.lock:
            self._shell('mkdir','-p','/sdcard/Download')
            self.call('-s',self.address,'push',str(path),remote,timeout=max(60,min(600,60+path.stat().st_size//100000)))
            actual=self._shell('sha256sum',remote).split()
            if not actual or actual[0].lower()!=digest:raise CloudPhoneError('传到云手机的文件SHA-256不一致，未进行选文件')
            # Android媒体数据库更新属于文件管理，不模拟点击或输入。
            from urllib.parse import quote
            self._shell('am','broadcast','-a','android.intent.action.MEDIA_SCANNER_SCAN_FILE','-d','file://'+quote(remote,safe='/'))
        return {'path':remote,'name':remote.rsplit('/',1)[1],'sha256':digest,'size':path.stat().st_size}

    def installed_apps(self):
        return {line.removeprefix('package:').strip() for line in self._shell('pm','list','packages').splitlines() if line.startswith('package:')}

    def install_app(self,package,path):
        if package in self.installed_apps():return {'installed':True,'changed':False}
        if path.suffix.lower()!='.apk' or not path.is_file():raise CloudPhoneError('安装需要执行端可读取的APK文件：'+str(path))
        result=self.call('-s',self.address,'install',str(path.resolve()),timeout=240)
        if 'Success' not in result or package not in self.installed_apps():
            raise CloudPhoneError('APK安装后未找到指定包名 '+package+'：'+result)
        return {'installed':True,'changed':True}

    def forward_chrome(self):
        deadline=time.monotonic()+10
        while True:
            sockets=self._shell('cat','/proc/net/unix',timeout=5)
            if '@chrome_devtools_remote' in sockets:break
            if time.monotonic()>=deadline:raise CloudPhoneError('Chrome启动后10秒仍未提供网页查询通道，请确认手机Chrome已完成首次使用设置')
            time.sleep(.25)
        port=self.call('-s',self.address,'forward','tcp:0','localabstract:chrome_devtools_remote')
        if not port.isdigit():raise CloudPhoneError('未取得Chrome查询端口')
        return int(port)

    def close(self):
        errors=[]
        if self.started:
            try:self.call('kill-server');self.started=False
            except Exception as exc:errors.append(str(exc))
        if self.endpoint:
            try:self.provider.restore_adb(self.device,self.endpoint);self.endpoint=None
            except Exception as exc:errors.append(str(exc))
        if errors:raise CloudPhoneError('；'.join(errors),provider=self.provider.label,device=self.device,stage='恢复查询连接')
