"""GeeLark适配；与DuoPlus无耦合。状态按官方Markdown而非过期OpenAPI枚举。"""
import re
import time
from ...framework.contracts import AdbEndpoint, CloudPhoneError, CloudProvider, Device
from .api import GeeLarkAPI

class GeeLarkProvider(CloudProvider):
    key='geelark';label='GeeLark'
    capabilities=frozenset({'app','browser','upload','install','proxy','rename','isolatedView','networkMode'})
    def __init__(self,config,config_root,*,api=None):
        super().__init__(config,config_root);self.api=api or GeeLarkAPI(config);self._urls={}
    @staticmethod
    def normalize(row):
        # 官方Get_all_cloud_phones.md：0已启动、1启动中、2已关闭。
        state={0:'running',1:'starting',2:'idle'}.get(row.get('status'),'unavailable')
        if row.get('rpaStatus')==1:state='running'
        return Device(str(row['id']),str(row.get('serialName')or row['id']),state,'厂商RPA运行中' if row.get('rpaStatus')==1 else '',row)
    def list_devices(self):
        rows=[];seen=set()
        for page in range(1,101):
            data=self.api.request('/phone/list',{'page':page,'pageSize':100});batch=data.get('items',[])
            for raw in batch:
                item=self.normalize(raw)
                if item.id in seen:raise CloudPhoneError('设备接口重复返回同一页，未使用不完整列表',provider=self.label)
                seen.add(item.id);rows.append(item)
            total=data.get('total')
            if (len(batch)<100 if total is None else len(rows)>=int(total)):return rows
            if not batch:raise CloudPhoneError('厂商设备总数与分页不一致，请重新扫描',provider=self.label)
        raise CloudPhoneError('设备分页超限，未使用不完整设备列表',provider=self.label)
    def device(self,device_id):
        data=self.api.request('/phone/list',{'ids':[device_id]})
        rows=[r for r in data.get('items',[]) if str(r.get('id'))==device_id]
        if len(rows)!=1:raise CloudPhoneError('设备不存在',provider=self.label,device=device_id)
        return self.normalize(rows[0])
    def start(self,device_id):
        data=self.api.request('/phone/start',{'ids':[device_id],'energySavingMode':0},partial=True)
        rows=[r for r in data.get('successDetails',[]) if str(r.get('id'))==device_id]
        if len(rows)!=1 or not rows[0].get('url'):
            raise CloudPhoneError('启动未成功：'+str(data),provider=self.label,device=device_id,stage='启动')
        self._urls[device_id]=rows[0]['url'];return {'accepted':True}
    def stop(self,device_id):
        data=self.api.request('/phone/stop',{'ids':[device_id]},partial=True)
        if data.get('failAmount',0) or data.get('failDetails'):
            raise CloudPhoneError('关机失败：'+str(data),provider=self.label,device=device_id)
        self._urls.pop(device_id,None);return {'accepted':True}
    def open_window(self,device_id,name):
        from .viewer import GeeLarkWindow
        url=self._urls.get(device_id)
        if not url:raise CloudPhoneError('没有本任务启动取得的独立访问链接；未接管他人窗口',provider=self.label)
        if self.config.get('managementOnly'):
            from ...management_window import open_management_window
            return open_management_window(self,device_id,name,url)
        return GeeLarkWindow.open(self,device_id,name,self.config_root,url)
    def rename(self,device_id,name):return self.api.request('/phone/detail/update',{'id':device_id,'name':name})
    def check_bound_proxy(self, device_id, *, expected=None, binding=None):
        try:
            bound = self.device(device_id).raw.get('proxy') or {}
            protocol = str(bound.get('type') or '').lower()
            port = int(bound.get('port') or 0)
        except (TypeError, ValueError, AttributeError, CloudPhoneError):
            raise CloudPhoneError('读取设备当前代理失败，请核对厂商连接与权限',provider=self.label,device=device_id) from None
        if protocol not in ('socks5', 'http', 'https') or not bound.get('server') or not 1 <= port <= 65535:
            raise CloudPhoneError('设备未返回可检测的现有代理，请配置任务代理或在 GeeLark 绑定可检测的代理',provider=self.label,device=device_id)
        actual = {'type': protocol, 'host': str(bound['server']).strip().lower(), 'port': port,
                  'username': bound.get('username') or '', 'password': bound.get('password') or ''}
        if expected is not None:
            wanted = {'type': expected['type'], 'host': expected['host'].strip().lower(), 'port': int(expected['port']),
                      'username': expected.get('username') or '', 'password': expected.get('password') or ''}
            if actual != wanted:
                raise CloudPhoneError('设备当前代理与本次挂载的网关或认证信息不一致，请重试',provider=self.label,device=device_id)
        return self.check_candidate_proxy(device_id, actual)

    def check_candidate_proxy(self, device_id, proxy):
        payload = {'proxyQueryChannel': 'IP2Location', 'proxyType': proxy['type'], 'server': proxy['host'],
                   'port': proxy['port'], 'username': proxy.get('username') or '', 'password': proxy.get('password') or ''}
        try:
            result = self.api.request('/proxy/check', payload)
        except CloudPhoneError:
            raise CloudPhoneError('调用厂商代理检测接口失败，请核对厂商连接与权限',provider=self.label,device=device_id) from None
        return self.proxy_measurement(device_id, result.get('detectStatus'), result.get('outboundIP'),
                                      result.get('countryCode'), result.get('subdivision'), result.get('city'))

    def set_proxy(self,device_id,proxy):
        types={'socks5':1,'http':2,'https':3}
        if proxy['type'] not in types:raise CloudPhoneError('GeeLark不支持此代理协议')
        return self.api.request('/phone/detail/update',{'id':device_id,'proxyConfig':{
            'typeId':types[proxy['type']],'server':proxy['host'],'port':proxy['port'],
            'username':proxy.get('username',''),'password':proxy.get('password','')}})
    def validate_network_mode(self, device, mode):
        super().validate_network_mode(device, mode)
        version = re.search(r'\d+', device.public()['system'])
        if version and int(version.group()) not in (12, 13, 15):
            raise CloudPhoneError('GeeLark 自定义联网方式仅支持 Android 12、13、15',
                                  provider=self.label, device=device.id)

    def set_network_mode(self, device, mode, check):
        wanted = {'wifi': 0, 'mobile': 1}[mode]
        def confirmed(row):
            actual = (row.raw.get('equipmentInfo') or {}).get('netType')
            return str(actual) == str(wanted)
        if confirmed(device):
            return
        self.api.request('/phone/net/set', {'id': device.id, 'netType': wanted})
        for attempt in range(5):
            check()
            if confirmed(self.device(device.id)):
                return
            if attempt < 4:
                time.sleep(1)
        raise CloudPhoneError('厂商尚未确认所选联网方式，请刷新设备后重试',
                              provider=self.label, device=device.id)

    def open_adb(self,device_id):
        def read():
            data=self.api.request('/adb/getData',{'ids':[device_id]},partial=True)
            rows=[r for r in data.get('items',[]) if str(r.get('id'))==device_id]
            if len(rows)!=1:raise CloudPhoneError('未返回设备ADB信息',provider=self.label,device=device_id)
            return rows[0]
        row=read();restore=row.get('code')==49001
        if restore:
            self.api.request('/adb/setStatus',{'ids':[device_id],'open':True});row=read()
        if row.get('code')!=0:raise CloudPhoneError('设备ADB不可用：'+str(row.get('code')),provider=self.label,device=device_id)
        return AdbEndpoint(str(row['ip'])+':'+str(row['port']),str(row.get('pwd','')),restore)
    def restore_adb(self,device_id,endpoint):
        if endpoint.restore:self.api.request('/adb/setStatus',{'ids':[device_id],'open':False})
