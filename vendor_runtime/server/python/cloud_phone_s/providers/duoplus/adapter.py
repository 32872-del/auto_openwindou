"""DuoPlus官方字段翻译；不承担调度、动作编排或界面逻辑。"""
import hashlib
import re
import time
from ...duoplus import DuoPlus, DuoPlusError
from ...framework.contracts import AdbEndpoint, CloudPhoneError, CloudProvider, Device

class DuoPlusProvider(CloudProvider):
    key='duoplus'
    label='DuoPlus'
    capabilities=frozenset({'app','browser','upload','install','proxy','ipEnvironment','rename','isolatedView','networkMode','proxySwitch'})
    network_mode_after_boot = True

    def __init__(self,config,config_root,*,api=None):
        super().__init__(config,config_root)
        self.api=api or DuoPlus(self.api_key)

    @staticmethod
    def normalize(row):
        code=row.get('status')
        try:code=int(code)
        except (TypeError,ValueError):pass
        state={1:'running',2:'idle',10:'starting',11:'starting'}.get(code,'unavailable')
        reason={0:'尚未配置',3:'已过期',4:'需要续费',11:'配置中',12:'配置失败'}.get(code,'')
        return Device(str(row['id']),str(row.get('name')or row['id']),state,reason,row)

    def list_devices(self):
        rows=[];seen=set()
        for page in range(1,101):
            data=self.api.request('/api/v1/cloudPhone/list',{'page':page,'pagesize':100})
            batch=data.get('list',[])
            for raw in batch:
                item=self.normalize(raw)
                if item.id in seen:raise CloudPhoneError('设备接口重复返回同一页，未使用不完整列表',provider=self.label)
                seen.add(item.id);rows.append(item)
            total=data.get('total')
            if (len(batch)<100 if total is None else len(rows)>=int(total)):return rows
            if not batch:raise CloudPhoneError('厂商设备总数与分页不一致，请重新扫描',provider=self.label)
        raise CloudPhoneError('设备列表分页超过上限，未使用不完整清单',provider=self.label)

    def device(self,device_id):
        rows=self.api.devices(device_id)
        if len(rows)!=1:raise CloudPhoneError('设备不存在',provider=self.label,device=device_id)
        return self.normalize(rows[0])

    def runtime_state(self,device_id):
        return self.normalize(self.api.status(device_id))

    def start(self,device_id):return self.api.power(device_id,True)
    def stop(self,device_id):return self.api.power(device_id,False)
    def elements(self,device_id):return self.api.elements(device_id)
    def screen(self,device_id):return self.api.screen(device_id)

    def open_window(self,device_id,name):
        if self.config.get('managementOnly'):
            from ...management_window import open_management_window
            return open_management_window(self,device_id,name)
        from ...isolated_window import DuoPlusIsolatedWindow
        return DuoPlusIsolatedWindow.open(self.api,device_id,name,self.config_root)

    def validate_network_mode(self, device, mode):
        super().validate_network_mode(device, mode)
        version = device.public()['system'].lower().replace('andriod', 'android')
        wifi_only = re.search(r'(?<!\d)(?:10|11)(?!\d)|12[\s(（-]*(?:区域|region)?\s*b\b', version)
        if mode == 'mobile' and wifi_only:
            raise CloudPhoneError('DuoPlus Android 10、11 和 12 区域 B 仅支持 Wi-Fi',
                                  provider=self.label, device=device.id)

    def set_network_mode(self, device, mode, check):
        from .network import apply_mode, configure_wifi
        if mode == 'wifi':
            return configure_wifi(self, device.id, check)
        return apply_mode(self.api, device.id, mode, check)

    def open_adb(self,device_id):
        row=self.device(device_id).raw
        restore=row.get('adb_status')!=1
        if restore:
            data=self.api.request('/api/v1/cloudPhone/openAdb',{'image_ids':[device_id]})
            if device_id not in data.get('success',[]):raise CloudPhoneError('开启ADB失败：'+str(data),provider=self.label,device=device_id)
            row=self.device(device_id).raw
        return AdbEndpoint(str(row.get('adb','')).strip(),restore=restore)

    def restore_adb(self,device_id,endpoint):
        if endpoint.restore:
            data=self.api.request('/api/v1/cloudPhone/closeAdb',{'image_ids':[device_id]})
            if device_id not in data.get('success',[]):raise CloudPhoneError('恢复ADB关闭失败：'+str(data))

    def update(self,device_id,values,*,wait_seconds=75):
        deadline=time.monotonic()+wait_seconds
        while True:
            data=self.api.request('/api/v1/cloudPhone/update',{'images':[{'image_id':device_id,**values}]})
            if device_id in [str(value) for value in data.get('success',[])]:return data
            reason=data.get('failreason',data.get('fail_reason',data.get('failReason',{})))
            failed={str(value) for value in data.get('fail',[])}
            configuring='配置中' in str(reason) and (not failed or device_id in failed)
            if not configuring:raise CloudPhoneError('修改未得到确认：'+str(data),provider=self.label,device=device_id)
            remaining=deadline-time.monotonic()
            if remaining<=0:raise CloudPhoneError('云手机持续配置中，等待后仍不可操作；本次未继续启动，请稍后重试',provider=self.label,device=device_id,code='configuring')
            time.sleep(min(3,remaining))

    def rename(self,device_id,name):
        # DuoPlus 的 images[].name 限制为 2～20 个字符；模板展开后再约束长度。
        name=str(name).strip()
        if not name:raise CloudPhoneError('云手机名称不能为空',provider=self.label,device=device_id)
        if len(name)>20:name=name[:13]+'-'+hashlib.sha256(name.encode('utf-8')).hexdigest()[:6]
        elif len(name)<2:name=name+'-'+hashlib.sha256(device_id.encode('utf-8')).hexdigest()[:6]
        return self.update(device_id,{'name':name})

    def check_bound_proxy(self, device_id, *, expected=None, binding=None):
        try:
            detail = self.api.request('/api/v1/cloudPhone/info', {'image_id': device_id})
            bound = detail.get('proxy') or {}
            proxy_id = bound.get('id')
        except (DuoPlusError, AttributeError):
            raise CloudPhoneError('读取设备当前代理失败，请核对厂商连接与权限',provider=self.label,device=device_id) from None
        if not proxy_id:
            raise CloudPhoneError('设备未返回可检测的现有代理 ID，请配置任务代理或在 DuoPlus 绑定可检测的代理',provider=self.label,device=device_id)
        if expected is not None and (binding or {}).get('bound_proxy_id') != proxy_id:
            raise CloudPhoneError('设备当前绑定的代理 ID 与本次挂载不一致，请重试',provider=self.label,device=device_id)
        return self._check_proxy(device_id, {'proxy_id': proxy_id}, expected=expected)

    def check_candidate_proxy(self, device_id, proxy):
        return self._check_proxy(device_id, {
            'protocol': proxy['type'], 'host': proxy['host'], 'port': proxy['port'],
            'user': proxy.get('username', ''), 'password': proxy.get('password', '')})

    def _check_proxy(self, device_id, payload, *, expected=None):
        try:
            result = self.api.request('/api/v1/proxy/check', {**payload, 'ip_scan_channel': 'ip2location'})
        except DuoPlusError:
            raise CloudPhoneError('调用厂商代理检测接口失败，请核对厂商连接与权限',provider=self.label,device=device_id) from None
        measured = self.proxy_measurement(device_id, result.get('success'), result.get('ip'), result.get('country'),
                                          result.get('region'), result.get('city'))
        if expected is not None:
            # 厂商可隐藏认证字段。仅当检测接口回显明文时核对同一个专用代理 ID 的更新。
            for field, key in (('user', 'username'), ('password', 'password')):
                value = result.get(field)
                if expected.get(key) and value and '***' not in value and value != expected[key]:
                    raise CloudPhoneError('设备当前代理的认证信息与本次挂载不一致，请重试',provider=self.label,device=device_id)
        return measured

    def set_proxy(self,device_id,proxy):
        from .owned_proxy import bind_proxy
        return bind_proxy(self,device_id,proxy)

    def enable_task_proxy(self, device_id):
        try:
            data = self.api.request('/api/v1/cloudPhone/proxySwitch', {'image_ids': [device_id], 'status': 1})
        except DuoPlusError:
            return 'unconfirmed'
        if device_id in (data.get('fail') or []):
            return 'rejected'
        if device_id in (data.get('success') or []):
            return 'confirmed'
        return 'unconfirmed'

    def set_ip_environment(self,device_id):
        return self.update(device_id,{'gps':{'type':1},'locale':{'type':1}})

    def installed_apps(self,device_id):
        return set(self.api.request('/api/v1/app/installedList',{'image_id':device_id}).get('list',[]))
