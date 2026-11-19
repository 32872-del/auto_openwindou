"""先租约后修改设备；只领取关机空闲设备，等待可取消，无新建/购买。"""
from __future__ import annotations
import ctypes, hashlib, os, threading, time, uuid, random
from .contracts import CloudPhoneError

_lock=threading.RLock()
_leases={}

class DeviceLease:
    def __init__(self,provider,device,owner):
        self.key=provider+':'+device;self.owner=owner;self.token=uuid.uuid4().hex;self.handle=None
    def acquire(self):
        with _lock:
            if self.key in _leases:return False
            if os.name=='nt':
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
                kernel.CreateMutexW.restype=ctypes.c_void_p
                kernel.CloseHandle.argtypes=[ctypes.c_void_p]
                # 只持有命名对象句柄，不取得线程所有权，清理可由任务停止线程执行。
                handle=kernel.CreateMutexW(None,False,'Global\\AutoDrssion_CloudLease_'+hashlib.sha256(self.key.encode()).hexdigest())
                error=ctypes.get_last_error()
                if not handle:raise CloudPhoneError(f'建立设备租约失败：Windows {error}')
                if error==183:kernel.CloseHandle(handle);return False
                self.handle=handle
            _leases[self.key]=self
            return True
    def release(self):
        with _lock:
            if _leases.get(self.key) is not self:return
            if self.handle:
                kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.CloseHandle.argtypes=[ctypes.c_void_p]
                kernel.CloseHandle(self.handle);self.handle=None
            _leases.pop(self.key,None)
    @staticmethod
    def owners(provider):
        with _lock:return {k.split(':',1)[1]:v.owner for k,v in _leases.items() if k.startswith(provider+':')}

class DevicePool:
    def __init__(self,provider,allowed=(),*,filters=None):
        self.provider,self.allowed,self.filters=provider,set(allowed),filters or {}
    def _matches(self,device):
        if not any(self.filters.values()):return True
        from .device_catalog import detail,matches
        if self.provider.key=='duoplus' and (self.filters.get('device_brands') or self.filters.get('device_models')):
            device=detail(self.provider,device.id,device=device)
        return matches(device,self.filters,provider_key=self.provider.key)
    def scan(self):
        owners=DeviceLease.owners(self.provider.key)
        return [{**d.public(),'selected':not self.allowed or d.id in self.allowed,
                 'owner':owners.get(d.id,''),'available':d.status=='idle' and d.id not in owners and (not self.allowed or d.id in self.allowed)}
                for d in self.provider.list_devices()]
    def acquire(self,owner,stop_requested,wait_seconds):
        deadline=time.monotonic()+wait_seconds
        while True:
            if stop_requested():raise CloudPhoneError('任务已停止，已取消等待空闲设备')
            devices=self.provider.list_devices()
            if any(self.filters.values()):random.shuffle(devices)
            for device in devices:
                if stop_requested():raise CloudPhoneError('任务已停止，已取消等待空闲设备')
                if device.status!='idle' or (self.allowed and device.id not in self.allowed):continue
                if not self._matches(device):continue
                lease=DeviceLease(self.provider.key,device.id,owner)
                if not lease.acquire():continue
                try:
                    latest=self.provider.device(device.id)
                    if latest.status=='idle' and self._matches(latest):return latest,lease
                except BaseException:
                    lease.release();raise
                lease.release()
            if time.monotonic()>=deadline:
                raise CloudPhoneError(f'{wait_seconds}秒内没有空闲云手机。已有设备可能运行中、被占用、过期或不在选定范围',provider=self.provider.label,stage='等待设备')
            for _ in range(20):
                if stop_requested():raise CloudPhoneError('任务已停止，已取消等待空闲设备')
                time.sleep(.25)
