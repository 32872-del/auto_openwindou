"""通用准备与收尾流程；厂商差异通过Provider实现，输入由APP/网页驱动负责。"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import re,time
from .contracts import CloudPhoneError
from .device_pool import DevicePool

class DeviceRun:
    def __init__(self,provider,policy,allowed=(),*,stop_requested=lambda:False,on_stage=None,on_ownership=None,
                 on_proxy_checked=None,on_candidate_checked=None,on_guest_check=None,on_network_checked=None):
        self.provider,self.policy,self.stop_requested=provider,policy,stop_requested
        filters={snake:policy.get(camel,[]) for snake,camel in (('device_brands','deviceBrands'),('device_models','deviceModels'),('device_versions','deviceVersions'))}
        self.pool=DevicePool(provider,allowed,filters=filters);self.on_stage=on_stage
        self.device=None;self.lease=None;self.window=None;self.started=False;self.stage='等待设备'
        self.failed_stage=''
        self.on_ownership=on_ownership
        self.on_proxy_checked=on_proxy_checked
        self.on_candidate_checked=on_candidate_checked
        self.on_guest_check=on_guest_check
        self.on_network_checked=on_network_checked

    def _state(self):
        return self.provider.runtime_state(self.device.id)

    def _progress(self,stage):
        # 收尾时不能调用会检查取消标志的 step。
        self.stage=stage
        if self.on_stage:self.on_stage(stage)

    def check(self):
        if self.stop_requested():raise CloudPhoneError('任务已停止',provider=self.provider.label,stage=self.stage)
    def step(self,stage):
        self.stage=stage;self.check()
        if self.on_stage:self.on_stage(stage)
        from server.python.log_s import log
        log.info(f'{self.provider.label} {self.device.id if self.device else ""}：{stage}',module='云手机')

    def _wait_configured(self,wait_seconds=75):
        latest=self._state()
        if latest.reason!='配置中':return latest
        self.step('等待云手机配置完成')
        deadline=time.monotonic()+wait_seconds
        while latest.reason=='配置中':
            self.check()
            remaining=deadline-time.monotonic()
            if remaining<=0:raise CloudPhoneError('设备持续配置中，等待后仍不可启动；请稍后重试',provider=self.provider.label,device=self.device.id,stage=self.stage)
            time.sleep(min(2,remaining))
            latest=self._state()
        return latest

    def prepare(self,*,owner='',project='',record='',proxy=None):
        self.failed_stage=''
        try:
            self.step('等待空闲设备')
            self.device,self.lease=self.pool.acquire(owner,self.stop_requested,self.policy['idleWaitSeconds'])
            if self.on_ownership:self.on_ownership(self.device,False)
            raw = None
            if self.policy['useTaskProxy']:
                if proxy is None:raise CloudPhoneError('已启用任务代理，但本条没有取得代理；请在客户端配置代理网络')
                raw=proxy.to_runtime_dict() if hasattr(proxy,'to_runtime_dict') else dict(proxy)
                raw['username']=raw.get('username',raw.get('user',''))
                if self.on_candidate_checked:
                    self.step('挂载前核验厂商侧候选代理')
                    self.on_candidate_checked(self.provider.check_candidate_proxy(self.device.id,raw))
            mode = self.policy.get('networkMode', 'keep')
            if mode != 'keep':
                self.step('检查云手机联网方式')
                self.provider.validate_network_mode(self.device, mode)
                if not self.provider.network_mode_after_boot:
                    self.step('设置云手机联网方式')
                    report=self.provider.set_network_mode(self.device, mode, self.check)
                    if self.on_network_checked and isinstance(report,dict):
                        self.on_network_checked(report)
            if self.policy['ipEnvironment'] and 'ipEnvironment' not in self.provider.capabilities:
                raise CloudPhoneError('此厂商尚不支持保留数据的按IP环境更新；请关闭该项，设备未重置')
            binding = None
            if raw is not None:
                self.step('挂载任务代理')
                binding = self.provider.set_proxy(self.device.id,raw)
            if self.policy['ipEnvironment']:
                self.step('按代理IP更新地区环境');self.provider.set_ip_environment(self.device.id)
            if self.policy['renamePattern']:
                self.step('重命名')
                name=self.policy['renamePattern']
                values={'project':project,'record':record,'timestamp':datetime.now().strftime('%Y%m%d-%H%M%S'),
                        'provider':self.provider.label,'device':self.device.id}
                for k,v in values.items():name=name.replace('${'+k+'}',str(v))
                if '${' in name:raise CloudPhoneError('名称模板含未知变量；支持project、record、timestamp、provider、device')
                name=name.strip()
                if not name or len(name)>100:raise CloudPhoneError('云手机名称必须为1至100字符')
                self.provider.rename(self.device.id,name)
                self.device=self.provider.device(self.device.id)
            # 状态再确认，厂商启动接口仍需处理跨电脑占用冲突。
            latest=self._wait_configured()
            if latest.status!='idle':raise CloudPhoneError('设备已被其它操作启动，未接管')
            if not self.pool._matches(self.provider.device(self.device.id)):raise CloudPhoneError('设备的品牌、型号或系统版本已变化，不符合本次排队设置，未启动；请刷新设备列表后重新创建')
            if self.on_proxy_checked:
                self.step('核验厂商侧代理出口地区')
                self.on_proxy_checked(self.provider.check_bound_proxy(self.device.id, expected=raw, binding=binding))
            self.step('启动设备')
            # 发出请求前登记收尾责任；接口超时可能已在远端开机。
            self.started=True
            if self.on_ownership:self.on_ownership(self.device,True)
            self.provider.start(self.device.id)
            start_wait=240 if self.provider.key=='duoplus' else 120
            started_at=time.monotonic();deadline=started_at+start_wait;last_progress=None
            while True:
                self.check()
                latest=self._state()
                if latest.status=='running':break
                if latest.status=='unavailable':raise CloudPhoneError('云手机无法开机：'+(latest.reason or '厂商返回未知状态'),provider=self.provider.label,device=self.device.id,stage='等待开机')
                elapsed=int(time.monotonic()-started_at)
                progress=(latest.status,latest.reason,elapsed//10)
                if progress!=last_progress:
                    self.step(f'等待云手机开机：{latest.reason or ("开机中" if latest.status=="starting" else "等待厂商启动")}（{elapsed}/{start_wait}秒）');last_progress=progress
                if time.monotonic()>=deadline:raise CloudPhoneError(f'{start_wait}秒内未启动完成；最后状态：{latest.reason or latest.status}')
                for _ in range(8):
                    self.check();time.sleep(.25)
            if mode != 'keep' and self.provider.network_mode_after_boot:
                self.step('设置并核对云手机联网方式')
                report=self.provider.set_network_mode(self.device, mode, self.check)
                if self.on_network_checked and isinstance(report,dict):
                    self.on_network_checked(report)
            if self.on_guest_check:
                switch = None
                if raw is not None and 'proxySwitch' in self.provider.capabilities:
                    self.step('启用云手机任务代理')
                    switch = self.provider.enable_task_proxy(self.device.id)
                self.step('核验设备内各线路出口')
                self.on_guest_check(self.device.id, self.check, switch)
            self.step('准备指定软件')
            apps=self.policy.get('apps',[])
            if apps:
                installed=self.provider.installed_apps(self.device.id)
                for app in apps:
                    self.check();package=app['package']
                    if package not in installed:
                        path=Path(app.get('apkPath',''))
                        if not path.is_absolute():path=self.provider.config_root.parent/path
                        if not app.get('apkPath'):raise CloudPhoneError('设备未安装 '+package+'，请填写APK文件路径')
                        self.provider.install_app(self.device.id,package,path)
            if self.policy.get('openWindow',True):
                self.step('打开云手机操作窗口')
                self.window=self.provider.open_window(self.device.id,self.device.name)
            else:
                self.step('设备已启动')
            self.step('执行流程')
            return self.window
        except BaseException as exc:
            # close 会发布关机进度；先保存失败现场，避免清理覆盖根因。
            self.failed_stage=getattr(exc,'stage','') or self.stage
            try:self.close(force_stop=True)
            except Exception as cleanup:exc.add_note('收尾未完成：'+str(cleanup))
            if isinstance(exc,CloudPhoneError):raise
            raise CloudPhoneError(f'{type(exc).__name__}: {exc}',provider=self.provider.label,
                device=self.device.id if self.device else '',stage=self.failed_stage) from exc

    def launch_apps(self,driver):
        for app in self.policy.get('apps',[]):
            if app.get('launchSelector'):
                driver.send('home')
                element=driver.find(app['launchSelector'],timeout=5)
                if element is None:raise CloudPhoneError('未找到指定软件桌面入口：'+app['launchSelector'])
                driver.human_click(element)

    def close(self,force_stop=False):
        errors=[]
        if self.device:
            try:self.provider.close_queries(self.device.id)
            except Exception as exc:errors.append(str(exc))
        if self.window:
            try:self.window.close();self.window=None
            except Exception as exc:errors.append(str(exc))
        if self.started and (force_stop or self.policy['closeAfterRun']):
            try:
                wait_seconds=120 if self.provider.key=='duoplus' else 60
                deadline=time.monotonic()+wait_seconds
                last_stop=None;last_error='';requests=0;idle_since=None;last_progress=None
                while True:
                    try:latest=self._state()
                    except Exception as exc:
                        latest=None;last_error=str(exc)
                    now=time.monotonic()
                    if latest and latest.status=='idle':
                        # 开机/关机异步交错时，单次读到关机不能立即释放设备。
                        if idle_since is None:idle_since=now
                        if now-idle_since>=2:break
                    else:
                        idle_since=None
                        # 取消发生在开机中时，首次关机可能被稍后完成的开机覆盖。
                        # 仅确认后来仍在运行时补发，绝不重置、删除设备。
                        if last_stop is None or (latest and latest.status=='running' and now-last_stop>=10 and requests<3 and not last_error):
                            last_stop=now;requests+=1
                            try:self.provider.stop(self.device.id);last_error=''
                            except Exception as exc:
                                last_error=str(exc)
                                # 响应丢失且已关机可继续确认；明确仍未关闭则保留责任供重试。
                                if self._state().status!='idle':raise
                    progress=(latest.status if latest else 'unknown',int((wait_seconds-(deadline-now))//10))
                    if progress!=last_progress:
                        self._progress('确认云手机关机：'+(latest.reason or latest.status if latest else '状态查询暂时失败'));last_progress=progress
                    if now>=deadline:
                        raise CloudPhoneError(f'已请求关机，{wait_seconds}秒内未确认关闭；最后状态：{latest.reason or latest.status if latest else "查询失败"}；租约保留供清理重试'+('；'+last_error if last_error else ''))
                    time.sleep(2)
                self.started=False
            except Exception as exc:errors.append(str(exc))
        if not errors and self.lease:self.lease.release();self.lease=None
        if errors:raise CloudPhoneError('；'.join(errors),provider=self.provider.label,stage='结束清理')
