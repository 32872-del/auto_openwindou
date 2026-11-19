"""如意管理模式：持久环境记录、原生进程启动，不保留 FirefoxPage 查询连接。"""
from dataclasses import asdict
from pathlib import Path
import psutil
from .paths import read_json,atomic_json
from .cleanup import process_state,close_owned_viewer
from server.python.browser_s.ruyi_browser_s.ruyi_browser import RuyiBrowser
from server.python.browser_s.ruyi_browser_s.ruyi_browser_fingerprint import prepare_ruyi_fingerprint
from server.python.browser_s.ruyi_browser_s.desktop_process import launch_on_desktop
from server.python.browser_s.browser_module_s.browser_cache import prepare_local_browser_proxy
from server.python.browser_s.browser_module_s.browser_error import BrowserError
from server.python.browser_s.browser_info import StartedBrowserInfo
from server.python.browser_s.browser_task_setting import ProxySetting

class ManagedRuyi(RuyiBrowser):
    def __init__(self,setting):
        super().__init__(setting)
        root=self._profile_root().resolve()
        for marker in root.glob('*/launcher_ruyi.json'):
            if not marker.resolve().is_relative_to(root):continue
            data=read_json(marker,{})
            identity=data.get('id','')
            if not identity.startswith('ruyi-') or len(identity)!=37:continue
            record=data['record'];record['profile_dir']=marker.parent
            record['proxy']=ProxySetting(**record['proxy']) if record.get('proxy') else None
            self._records[identity]=record

    def _save(self,identity):
        record=self._records[identity];directory=Path(record['profile_dir']).resolve()
        root=self._profile_root().resolve()
        if directory==root or not directory.is_relative_to(root):raise ValueError('如意环境目录越界')
        atomic_json(directory/'launcher_ruyi.json',{'id':identity,'record':{
            **{k:v for k,v in record.items() if k not in ('profile_dir','proxy')},
            'proxy':asdict(record['proxy']) if record.get('proxy') else None}})

    def create_browser(self,setting):
        result=super().create_browser(setting)
        self._save(result.browser_id)
        return result

    def _record(self,identity):
        try:return super()._record(identity)
        except BrowserError as exc:
            if exc.code=='browser_not_found':exc.code='not_found'
            raise

    def start_browser(self,setting):
        from ruyipage import FirefoxOptions
        from server.python.driver_s.win32_dp_module_s.win32_native import current_desktop_name
        record=self._record(setting.browser_id)
        if process_state(record.get('_pid'),record.get('_created'))!='exited':raise RuntimeError('此如意环境已在运行')
        options=FirefoxOptions().set_auto_port().set_browser_path(str(self._ensure_browser_path()))
        proxy=prepare_local_browser_proxy(record.get('proxy'),metadata=record.get('metadata'),provider=self.key,display_name=self.display_name)
        prepare_ruyi_fingerprint(options,profile_dir=Path(record['profile_dir']),proxy=proxy,
                                 geo_timeout=self.setting.geo_timeout,geo_retries=self.setting.geo_retries,library_directory=self._fingerprint_root())
        options.set_profile(str(record['profile_dir']))
        target=next((f for f in setting.flags if f.startswith(('http://','https://','about:'))),'about:blank')
        options.set_argument('-url',target)
        owned=launch_on_desktop(options,current_desktop_name(),setting.timeout_seconds)
        self._desktop_processes[setting.browser_id]=owned
        try:
            record.update(_pid=owned.pid,_created=psutil.Process(owned.pid).create_time())
            self._save(setting.browser_id)
        except BaseException:
            owned.close(timeout=0,terminate=True);raise
        return StartedBrowserInfo(provider=self.key,browser_id=setting.browser_id,debug_port=options.port,cdp_url='',
                                  process_name=self.process_name,raw={'pid':owned.pid},driver_type='ruyi')

    def close_browser(self,identity,*,force=False):
        record=self._record(identity)
        close_owned_viewer({'pid':record.get('_pid',0),'created':record.get('_created',0),'_viewer_owned':True})
        owned=self._desktop_processes.pop(identity,None)
        if owned:owned.close(timeout=0)
        record.update(_pid=0,_created=0);self._save(identity)

    def delete_browser(self,identity,*,permanent=True):
        # 父类只删除本记录中、处于本机环境根目录下的独立目录。
        if identity not in self._records:return
        return super().delete_browser(identity,permanent=permanent)

    def update_browser(self,identity,values):
        result=super().update_browser(identity,values);self._save(identity);return result

    def update_proxy(self,identity,proxy):
        with self._lock:
            record=self._record(identity)
            if process_state(record.get('_pid'),record.get('_created'))!='exited':raise RuntimeError('请先关闭原环境再更新代理')
            record['proxy']=proxy
            self._save(identity)
