"""本机 Chrome 从当前桌面启动，存活判断只查进程。"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import uuid
import psutil
from types import SimpleNamespace


def find_chrome(configured=''):
    if configured:
        path=Path(configured).expanduser().resolve()
        if not path.is_file():raise ValueError('Chrome 程序不存在：'+str(path))
        return path
    for base in (os.environ.get('PROGRAMFILES',''),os.environ.get('PROGRAMFILES(X86)',''),os.environ.get('LOCALAPPDATA','')):
        path=Path(base)/'Google/Chrome/Application/chrome.exe'
        if path.is_file():return path
    raise ValueError('没有找到本地 Chrome；请填写程序路径')


class LocalChrome:
    key='chrome'
    def __init__(self, config):
        self.config=config
        self.root=Path(os.environ['WINDOW_STUDIO_DATA'])/'文件缓存/浏览器/chrome'
        self.root.mkdir(parents=True,exist_ok=True)
        self.processes={}

    def check_health(self):
        return SimpleNamespace(state='healthy',message='已找到 '+str(find_chrome(self.config['browser_path'])),version='')

    def path(self, identity):
        if not identity or any(c not in '0123456789abcdef' for c in identity):raise ValueError('Chrome 环境编号无效')
        path=(self.root/identity).resolve()
        if path.parent != self.root.resolve():raise ValueError('环境目录越界')
        return path

    def create_browser(self, setting):
        identity=uuid.uuid4().hex
        path=self.path(identity);path.mkdir()
        (path/'launcher.json').write_text(json.dumps({'name':setting.name,'proxy':None if setting.proxy is None else {
            k:getattr(setting.proxy,k) for k in ('protocol','host','port','username','password')}},ensure_ascii=False),encoding='utf-8')
        return SimpleNamespace(browser_id=identity)

    def get_browser(self, identity):
        from server.python.browser_s.browser_module_s.browser_error import BrowserError
        path = self.path(identity)
        if not (path / 'launcher.json').is_file():
            raise BrowserError(provider='chrome', operation='detail', code='not_found', message='本地环境不存在')
        return SimpleNamespace(browser_id=identity)

    def update_proxy(self, identity, proxy):
        from .paths import atomic_json
        self.get_browser(identity)
        path=self.path(identity)/'launcher.json'
        data=json.loads(path.read_text(encoding='utf-8'))
        data['proxy']={k:getattr(proxy,k) for k in ('protocol','host','port','username','password')}
        atomic_json(path,data)

    def start_browser(self, setting):
        import win32api,win32con,win32process,win32service
        path=self.path(setting.browser_id)
        if not path.is_dir():raise ValueError('Chrome 环境不存在')
        data=json.loads((path/'launcher.json').read_text(encoding='utf-8'))
        proxy=data.get('proxy')
        if proxy and (proxy['username'] or proxy['password']):
            raise ValueError('本地 Chrome 的带账号代理转发尚未迁移；请使用指纹浏览器，或本机无认证代理')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        args=[str(find_chrome(self.config['browser_path'])),f'--user-data-dir={path}',f'--remote-debugging-port={port}',
              '--no-first-run','--no-default-browser-check','--disable-backgrounding-occluded-windows',
              '--disable-features=PasswordLeakDetection,AutofillServerCommunication','--disable-save-password-bubble',*setting.flags]
        if proxy:args.append(f"--proxy-server={proxy['protocol']}://{proxy['host']}:{proxy['port']}")
        if not any(x.startswith(('https://','http://','about:')) for x in args):args.append('about:blank')
        startup=win32process.STARTUPINFO()
        desktop=win32service.GetThreadDesktop(win32api.GetCurrentThreadId())
        startup.lpDesktop=win32service.GetUserObjectInformation(desktop,win32con.UOI_NAME)
        process,thread,pid,_=win32process.CreateProcess(str(find_chrome(self.config['browser_path'])),subprocess.list2cmdline(args),None,None,False,0,None,str(path),startup)
        win32api.CloseHandle(thread);win32api.CloseHandle(process)
        self.processes[setting.browser_id]=(pid,psutil.Process(pid).create_time())
        return SimpleNamespace(debug_port=port,raw={'pid':pid},driver_type='dp')

    def close_browser(self, identity, **kwargs):
        path=self.path(identity)
        owned=[]
        for process in psutil.process_iter(['pid','cmdline']):
            try:
                if f'--user-data-dir={path}' in (process.info['cmdline'] or []):owned.append(process)
            except (psutil.NoSuchProcess,psutil.AccessDenied):pass
        for process in owned:
            try:
                children=process.children(recursive=True)
                process.terminate()
                for child in children:
                    try:child.terminate()
                    except psutil.NoSuchProcess:pass
            except psutil.NoSuchProcess:pass
        _,alive=psutil.wait_procs(owned,timeout=5)
        if alive:raise RuntimeError('Chrome 进程仍在运行，保留环境等待再次清理')

    def delete_browser(self, identity, **kwargs):
        path=self.path(identity)
        if not path.exists():return
        for process in psutil.process_iter(['cmdline']):
            if f'--user-data-dir={path}' in (process.info['cmdline'] or []):raise RuntimeError('Chrome 环境仍有进程占用')
        # 仅删本软件生成、携带所有权标记的单个环境。
        if not (path/'launcher.json').is_file():raise RuntimeError('缺少本软件环境标记，未删除')
        shutil.rmtree(path)
