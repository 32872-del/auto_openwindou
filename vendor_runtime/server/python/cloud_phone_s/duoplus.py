"""DuoPlus 官方 OpenAPI；只向本地界面公开明确的设备操作。"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET

import requests


class DuoPlusError(RuntimeError):
    pass


class DuoPlus:
    BASE = 'https://openapi.duoplus.net'
    _READ_PATHS = frozenset({
        '/api/v1/cloudPhone/list', '/api/v1/cloudPhone/info',
        '/api/v1/cloudPhone/status', '/api/v1/app/installedList',
    })

    def __init__(self, api_key: str, *, session=None):
        self.api_key = api_key.strip()
        self.session = session or requests.Session()
        self._lock = threading.Lock()
        self._last = {}

    def request(self, path, body):
        if not self.api_key:
            raise DuoPlusError('请先填写并保存 DuoPlus API key')
        # 厂商管理接口沿用本机网络。仅重试白名单里的查询，不能重发开关机、
        # 更新配置、设备命令等可能已经被厂商执行的操作。
        attempts = 3 if path in self._READ_PATHS else 1
        for attempt in range(1, attempts + 1):
            # 每一次尝试都遵守同接口每秒一次的限制。
            with self._lock:
                delay = self._last.get(path, 0) + 1.05 - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._last[path] = time.monotonic()
            try:
                response = self.session.post(self.BASE + path, json=body,
                    headers={'DuoPlus-API-Key': self.api_key, 'Lang': 'zh'}, timeout=(8, 25))
                response.raise_for_status()
                payload = response.json()
                break
            except (requests.RequestException, ValueError) as exc:
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                transient = (
                    isinstance(exc, (requests.ConnectionError, requests.Timeout))
                    and not isinstance(exc, requests.exceptions.SSLError)
                ) or isinstance(exc, requests.HTTPError) and status in (502, 503, 504)
                if attempt < attempts and transient:
                    continue
                count = f'（厂商管理接口，已尝试 {attempt} 次）' if path in self._READ_PATHS else ''
                raise DuoPlusError(f'DuoPlus {path} 请求失败{count}：{type(exc).__name__}: {exc}') from exc
        if not isinstance(payload, dict):
            raise DuoPlusError(f'DuoPlus {path} 响应不是JSON对象')
        if payload.get('code') != 200:
            raise DuoPlusError(f'DuoPlus {path} 返回 {payload.get("code")}：{payload.get("message") or payload}')
        data = payload.get('data')
        if not isinstance(data, dict):
            raise DuoPlusError(f'DuoPlus {path} 响应缺少有效 data 对象：{data}')
        return data

    def devices(self, device_id):
        data = self.request('/api/v1/cloudPhone/list', {'image_id': [device_id], 'page': 1, 'pagesize': 100})
        return [item for item in data.get('list', []) if item.get('id') == device_id]

    def status(self, device_id):
        data = self.request('/api/v1/cloudPhone/status', {'image_ids': [device_id]})
        rows = data.get('list')
        if not isinstance(rows, list):
            raise DuoPlusError(f'DuoPlus 状态响应缺少设备列表：{data}')
        for item in rows:
            if isinstance(item, dict) and item.get('id') == device_id:
                return item
        raise DuoPlusError(f'DuoPlus 没有返回设备 {device_id}，请检查设备ID和账号权限')

    def power(self, device_id, on):
        path = '/api/v1/cloudPhone/' + ('powerOn' if on else 'powerOff')
        data = self.request(path, {'image_ids': [device_id]})
        if device_id not in data.get('success', []):
            raise DuoPlusError(f'DuoPlus 设备 {device_id} 开关机未受理：{data}')
        return {'accepted': True, 'message': '开机请求已受理，请刷新状态' if on else '关机请求已受理，请刷新状态'}

    def command(self, device_id, command):
        data = self.request('/api/v1/cloudPhone/command', {'image_id': device_id, 'command': command})
        if not isinstance(data, dict) or data.get('success') is not True:
            raise DuoPlusError(f'DuoPlus 设备命令失败：{data}')
        return str(data.get('content') or '')

    def screen(self, device_id):
        value = self.command(device_id, 'screencap -p | base64')
        try:
            content = base64.b64decode(''.join(value.split()), validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise DuoPlusError('DuoPlus 未返回有效截图，请确认设备已开机') from exc
        if not content.startswith(b'\x89PNG\r\n\x1a\n') or not 24 <= len(content) <= 12_000_000:
            raise DuoPlusError('DuoPlus 返回的截图格式或大小不符合要求')
        import struct
        width, height = struct.unpack('>II', content[16:24])
        if not 1 <= width <= 10000 or not 1 <= height <= 10000:
            raise DuoPlusError('DuoPlus 截图尺寸无效')
        return {'image': 'data:image/png;base64,' + base64.b64encode(content).decode(),
            'width': width, 'height': height}

    def elements(self, device_id, *, mark_web_content=False):
        # A failed or concurrent dump must never return another page's old visible IP.
        path = '/sdcard/autodrssion-ui-' + uuid.uuid4().hex + '.xml'
        failure = None
        try:
            self.command(device_id, 'DuoPlusDumpUI ' + path)
            raw = self.command(device_id, 'cat ' + path)
        except Exception as exc:
            failure = exc
            raise
        finally:
            try:
                self.command(device_id, 'rm -f ' + path)
            except DuoPlusError:
                if failure is None:
                    raise
                failure.add_note('临时控件树文件清理未获确认')
        from .framework.android import parse_ui
        return parse_ui(raw, mark_web_content=mark_web_content)

    def operate(self, device_id, operation, values):
        if operation == 'powerOn': return self.power(device_id, True)
        if operation == 'powerOff': return self.power(device_id, False)
        if operation == 'screen': return self.screen(device_id)
        if operation == 'elements': return self.elements(device_id)
        raise DuoPlusError('云手机点击、滑动和键盘操作需要先绑定 DuoPlus 电脑端窗口，使用 Win32 输入')


class DuoPlusService:
    def __init__(self, config_root: Path):
        self.path = Path(config_root) / '配置文件' / 'DuoPlus云手机.json'
        self._lock = threading.RLock()
        self._client = None
        self._desktop = None
        self._windows = {}
        self._closing = threading.Event()

    def close(self):
        self._closing.set()
        windows = [entry['window'] for entry in list(self._windows.values())]
        self._windows.clear()
        if self._desktop is not None and self._desktop not in windows:
            windows.append(self._desktop)
        self._desktop = None
        for window in windows:
            try:
                window.close()
            except Exception:
                import logging
                logging.getLogger(__name__).exception('关闭云手机隔离窗口失败，继续回收其它资源')

    def _close_window(self, window=None):
        window = window or self._desktop
        if self._desktop is window:
            self._desktop = None
        for key, entry in list(self._windows.items()):
            if entry['window'] is window:
                self._windows.pop(key, None)
        if window is not None:
            window.close()

    def windows(self):
        with self._lock:
            return [dict(entry['summary']) for entry in self._windows.values()]

    def settings(self):
        with self._lock:
            raw = self.path.read_bytes() if self.path.exists() else b''
            value = json.loads(raw.decode('utf-8-sig')) if raw else {'apiKey':'', 'deviceId':'', 'deviceName':''}
            return {**value, 'revision': hashlib.sha256(raw).hexdigest()}

    def save(self, value):
        with self._lock:
            if value['revision'] != self.settings()['revision']:
                raise DuoPlusError('DuoPlus配置已在别处修改，请重新读取后再保存')
            data = {key: str(value[key]).strip() for key in ('apiKey','deviceId','deviceName')}
            devices = list(dict.fromkeys(str(item).strip() for item in value.get('deviceIds', []) if str(item).strip()))
            if len(devices)>32 or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', item) for item in devices):
                raise DuoPlusError('执行设备ID每行一个，只允许字母、数字、下划线或短横线，最多32台')
            if devices: data['deviceIds'] = devices
            if not data['apiKey'] or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', data['deviceId']):
                raise DuoPlusError('请填写API key和有效的设备ID')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
            try:
                temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                temp.replace(self.path)
            finally:
                temp.unlink(missing_ok=True)
            # 修改当前连接不关闭其它设备已经打开的隔离窗口。
            self._desktop = None
            self._client = None
            return self.settings()

    def client(self):
        with self._lock:
            settings = self.settings()
            if not settings.get('deviceId'):
                raise DuoPlusError('请先填写并保存设备ID')
            if self._client is None or self._client.api_key != settings['apiKey']:
                self._client = DuoPlus(settings['apiKey'])
            return self._client, settings['deviceId']

    def operate(self, revision, operation, values, window_id=''):
        # 保存和执行不能交叉切换设备；多开界面的旧画面不能操作新设备。
        with self._lock:
            if self._closing.is_set():
                raise DuoPlusError('执行端正在关闭，不能继续操作云手机')
            if revision != self.settings()['revision']:
                raise DuoPlusError('云手机连接已改变，请重新读取配置和画面后再操作')
            client, device = self.client()
            window = self._desktop
            if window_id:
                entry = self._windows.get(window_id)
                if entry is None:
                    raise DuoPlusError('所选隔离窗口已经关闭，请重新选择；未操作其它设备')
                client, device, window = entry['client'], entry['device'], entry['window']
            elif len(self._windows) > 1 and operation in ('tap','swipe','text','home','back','enter','closeWindow'):
                raise DuoPlusError('已打开多个云手机，请先选择本次操作的隔离窗口')
            from .isolated_window import DuoPlusIsolatedWindow
            from .isolated_viewer import ViewerError
            if operation == 'closeWindow':
                self._close_window(window)
                return {'message': '本机隔离窗口已关闭；云手机是否自动关机由DuoPlus设置决定'}
            if operation == 'screen' and window is not None:
                try:
                    return window.screen()
                except ViewerError as exc:
                    self._close_window(window)
                    raise DuoPlusError(str(exc)) from exc
            if operation in ('powerOn', 'powerOff', 'screen', 'elements'):
                if operation.startswith('power'):
                    for entry in list(self._windows.values()):
                        if entry['device'] == device and entry['client'].api_key == client.api_key:
                            self._close_window(entry['window'])
                result = client.operate(device, operation, values)
                return result
            try:
                if operation == 'bindWindow':
                    window = None
                    identity = hashlib.sha256((client.api_key+'\0'+device).encode()).hexdigest()
                    for entry in self._windows.values():
                        if entry['identity'] == identity:
                            window = self._desktop = entry['window']
                            window.describe()
                            return dict(entry['summary'])
                    state = client.status(device)
                    if state.get('status') != 1:
                        raise DuoPlusError('设备尚未开机完成，请刷新状态后再操作')
                    name = str(state.get('name') or '').strip()
                    if not name:
                        raise DuoPlusError('DuoPlus 未返回设备名称，不能确认隔离窗口')
                    window = DuoPlusIsolatedWindow.open(client, device, name, self.path.parent.parent)
                    if self._closing.is_set():
                        window.close()
                        raise DuoPlusError('执行端正在关闭，已结束新建云手机窗口')
                    self._desktop = window
                    window_id = uuid.uuid4().hex
                    summary = {**window.describe(), 'windowId': window_id, 'deviceId': device}
                    self._windows[window_id] = {'window': window, 'client': client, 'device': device,
                                                'identity': identity, 'summary': summary}
                    return summary
                if window is None:
                    raise DuoPlusError('请先点击“打开隔离窗口”，再执行云手机动作')
                if operation == 'text':
                    # 查询已取得焦点再输入，避免远程画面刚点开输入框时丢首字。
                    if not any(e['focused'] and ('EditText' in e['class'] or 'AutoCompleteTextView' in e['class'])
                               for e in client.elements(device)):
                        raise DuoPlusError('云手机输入框尚未取得焦点，请先点选输入框后再输入')
                result = window.operate(operation, values, window.phone_size)
                if operation == 'text':
                    actual = [e['text'] for e in client.elements(device) if e['focused']]
                    if not any(values.get('text', '') in text for text in actual):
                        raise DuoPlusError(f'Win32 已发送文字，但云手机回读不一致：{actual}；没有重复输入，请检查画面')
                return result
            except ViewerError as exc:
                if window is not None:
                    self._close_window(window)
                raise DuoPlusError(str(exc)) from exc
