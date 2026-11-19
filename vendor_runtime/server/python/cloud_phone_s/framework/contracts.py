"""新增厂商只实现这里的设备接口；APP/网页驱动不判断厂商名。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
import threading


class CloudPhoneError(RuntimeError):
    def __init__(self, message, *, provider='', device='', stage='', code=''):
        self.provider, self.device, self.stage, self.code = provider, device, stage, code
        prefix = ' / '.join(str(x) for x in (provider, device, stage) if x)
        super().__init__((prefix + '：' if prefix else '') + str(message))


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    status: Literal['idle', 'running', 'starting', 'unavailable']
    reason: str = ''
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    def public(self):
        equipment=self.raw.get('equipmentInfo') or {}
        device=self.raw.get('device') or {}
        model=str(equipment.get('deviceModel') or device.get('model') or '')
        brand=str(equipment.get('deviceBrand') or device.get('brand') or '')
        return {'id':self.id,'name':self.name,'status':self.status,'reason':self.reason,
                'system':str(equipment.get('osVersion') or self.raw.get('os') or ''),
                'model':model,'brand':brand,'model_available':bool(model)}


@dataclass(frozen=True)
class AdbEndpoint:
    address: str
    password: str = field(default='', repr=False)
    restore: bool = False


class CloudProvider(ABC):
    key: str
    label: str
    capabilities: frozenset[str] = frozenset()
    network_mode_after_boot = False

    def __init__(self, config, config_root):
        self.config, self.config_root = dict(config), Path(config_root)
        self.api_key = str(config.get('apiKey','')).strip()
        self._android = {}
        self._android_lock = threading.RLock()

    @abstractmethod
    def list_devices(self) -> list[Device]: ...

    def device(self, device_id) -> Device:
        for item in self.list_devices():
            if item.id == device_id:return item
        raise CloudPhoneError('没有找到设备，请重新扫描',provider=self.label,device=device_id)

    def runtime_state(self, device_id) -> Device:
        """开关机轮询；有实时状态接口的厂商应覆盖此方法。"""
        return self.device(device_id)

    @abstractmethod
    def start(self, device_id): ...

    @abstractmethod
    def stop(self, device_id): ...

    @abstractmethod
    def open_window(self, device_id, name): ...

    @abstractmethod
    def open_adb(self, device_id) -> AdbEndpoint: ...

    @abstractmethod
    def restore_adb(self, device_id, endpoint): ...

    @abstractmethod
    def rename(self, device_id, name): ...

    @abstractmethod
    def set_proxy(self, device_id, proxy): ...

    def validate_network_mode(self, device, mode):
        if mode not in ('wifi', 'mobile') or 'networkMode' not in self.capabilities:
            raise CloudPhoneError('此设备不支持所选联网方式', provider=self.label, device=device.id)

    def set_network_mode(self, device, mode, check):
        raise CloudPhoneError('厂商未实现联网方式设置', provider=self.label, device=device.id)

    def check_bound_proxy(self, device_id, *, expected=None, binding=None):
        raise CloudPhoneError('厂商不支持核验设备现有代理出口',provider=self.label,device=device_id)

    def check_candidate_proxy(self, device_id, proxy):
        raise CloudPhoneError('厂商不支持在挂载前核验任务代理出口',provider=self.label,device=device_id)

    def proxy_measurement(self, device_id, success, ip, country, region, city):
        # 检测响应可能含代理密码，只返回显示和地区比对所需字段。
        if success is not True:
            raise CloudPhoneError('厂商无法通过设备绑定的代理连通外网',provider=self.label,device=device_id)
        try:
            address = str(ip_address(str(ip)))
        except ValueError:
            raise CloudPhoneError('厂商未返回有效的代理出口 IP',provider=self.label,device=device_id) from None
        country = str(country or '').strip().upper()
        if len(country) != 2 or not country.isascii() or not country.isalpha():
            raise CloudPhoneError('厂商未返回可核验的代理出口国家',provider=self.label,device=device_id)
        return {'ip': address, 'geo': {'countryCode': country, 'region': str(region or ''), 'city': str(city or '')}}

    def set_ip_environment(self, device_id):
        raise CloudPhoneError('此厂商尚未提供可保留原数据的按IP环境更新；未重置手机',provider=self.label,device=device_id)

    def android(self, device_id):
        from .android import AndroidAccess
        with self._android_lock:
            access=self._android.get(device_id)
            if access is None:
                access=AndroidAccess(self,device_id)
                access.start()
                self._android[device_id]=access
            return access

    def elements(self, device_id):return self.android(device_id).elements()
    def screen(self, device_id):return self.android(device_id).screen()
    def upload_file(self, device_id, path):return self.android(device_id).push_file(Path(path))
    def installed_apps(self, device_id):return self.android(device_id).installed_apps()
    def install_app(self, device_id, package, path):return self.android(device_id).install_app(package,Path(path))

    def close_queries(self, device_id):
        with self._android_lock:
            access=self._android.get(device_id)
            if access:
                access.close()
                self._android.pop(device_id,None)

    def close(self):
        errors=[]
        for device in list(self._android):
            try:self.close_queries(device)
            except Exception as exc:errors.append(str(exc))
        if errors:raise CloudPhoneError('；'.join(errors),provider=self.label,stage='关闭查询')
