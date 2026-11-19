"""设置 DuoPlus 仿真 Wi-Fi，并保留 Android 默认网络的独立读数。"""
import re
import time

from ...framework.contracts import CloudPhoneError


_MODE = {'WIFI': 'wifi', 'CELLULAR': 'mobile'}
_LABEL = {'wifi': 'Wi-Fi', 'mobile': '移动数据'}
# Keep current network records including wrapped capability fields. Drop network requests
# and historical logs, which can mention an entirely different transport for the same ID.
_DUMP_COMMAND = (
    "dumpsys connectivity | sed -n '/Active default network:/p; "
    "/^[[:space:]]*Current Networks:/,/^[[:space:]]*$/p'"
)


def _transports(record):
    match = re.search(r'\bTransports:\s*([A-Z0-9_]+(?:\s*[|&,]\s*[A-Z0-9_]+)*)\b', record)
    return set(re.split(r'\s*[|&,]\s*', match[1])) if match else set()


def _radio(types):
    return _MODE.get(next(iter(types))) if len(types) == 1 else None


def _network_state(dump):
    default = re.search(r'^\s*Active default network:[ \t]*(\d+|none)[ \t\r]*$', dump, re.MULTILINE)
    if not default:
        return None, '设备未返回默认网络编号'
    if default[1] == 'none':
        return None, 'Android 尚无默认网络'
    records = {}
    for entry in re.split(r'(?m)^\s*NetworkAgentInfo\b', dump)[1:]:
        # NetworkRequest entries describe requested transports, not the agent's capabilities.
        entry = re.split(r'(?m)^\s*(?:Requests:|NetworkRequest\b|Nat464Xlat:|Inactivity Timers:)', entry, maxsplit=1)[0]
        identity = re.search(r'\bnetwork\s*\{\s*(\d+)\s*\}', entry)
        if identity:
            records[identity[1]] = entry
    record = records.get(default[1], '')
    types = _transports(record)
    summary = '默认网络 ' + default[1] + '，传输类型 ' + ('|'.join(sorted(types)) or '未返回')
    info = re.search(r'\bni\{([^}]*)\}', record)
    if info and re.search(r'\b(?:DISCONNECTING|DISCONNECTED)\b', info[1]):
        return None, summary + '，连接已断开'
    if not types:
        # Some older images only include NetworkInfo for the active ID.
        if info and re.search(r'\bCONNECTED\b', info[1]):
            kind = re.match(r'\s*(?:\[type:\s*)?(WIFI|MOBILE|CELLULAR)\b', info[1])
            if kind:
                return ('wifi' if kind[1] == 'WIFI' else 'mobile'), summary
        return None, summary
    if 'VPN' not in types:
        return _radio(types), summary
    # Android reports VPN together with its current underlying transport(s).
    physical = types - {'VPN'}
    linked = re.search(r'\b(?:UnderlyingNetworks:\s*|underlying\{\s*)\[([^\]]*)\]', record)
    if linked:
        identity = linked[1].strip()
        if not identity.isdigit() or identity == default[1]:
            return None, summary + '，无唯一底层网络'
        backing_record = records.get(identity, '')
        if re.search(r'\bni\{[^}]*\b(?:DISCONNECTING|DISCONNECTED)\b', backing_record):
            return None, summary + '，底层网络 ' + identity + ' 已断开'
        backing = _transports(backing_record)
        summary += '，底层网络 ' + identity + '：' + ('|'.join(sorted(backing)) or '未返回')
        if len(backing) != 1 or (physical and physical != backing):
            return None, summary
        physical = backing
    return _radio(physical), summary


def active_mode(dump):
    return _network_state(dump)[0]


def configure_wifi(provider, device_id, check):
    """以厂商读回确认 Wi-Fi 开关；底层 Ethernet 不代表仿真开关失败。"""
    def read_status():
        detail = provider.api.request('/api/v1/cloudPhone/info', {'image_id': device_id})
        if detail.get('id') and str(detail['id']) != device_id:
            raise CloudPhoneError('厂商返回了其他设备的联网配置', provider='DuoPlus', device=device_id)
        wifi = detail.get('wifi') or {}
        value = wifi.get('status') if isinstance(wifi, dict) else None
        if type(value) not in (int, str) or str(value) not in ('1', '2'):
            raise CloudPhoneError('厂商未返回有效的 Wi-Fi 仿真开关，联网设置尚未核实',
                                  provider='DuoPlus', device=device_id)
        return int(value)

    def wait_status(wanted, checkpoint):
        for attempt in range(10):
            checkpoint()
            if read_status() == wanted:
                return
            if attempt < 9:
                time.sleep(1)
        raise CloudPhoneError('厂商未确认 Wi-Fi 仿真开关已' + ('开启' if wanted == 1 else '恢复'),
                              provider='DuoPlus', device=device_id)

    check()
    previous = read_status()
    changed = False
    try:
        if previous != 1:
            check()
            # 请求超时也可能已经生效，先登记恢复责任。
            changed = True
            provider.update(device_id, {'wifi': {'status': 1}})
            wait_status(1, check)
        check()
        # 仿真配置和 Android 实际传输类型分别记录，不能把 ETHERNET 改写成 WIFI。
        try:
            actual, summary = _network_state(provider.api.command(device_id, _DUMP_COMMAND))
        except Exception:
            actual, summary = None, 'Android 底层网络读数暂不可用'
        check()
    except Exception as exc:
        if changed:
            try:
                provider.update(device_id, {'wifi': {'status': previous}})
                wait_status(previous, lambda: None)
            except Exception:
                raise CloudPhoneError('Wi-Fi 仿真设置失败，原开关恢复未获确认',
                                      provider='DuoPlus', device=device_id) from exc
        raise

    return {'status': 'configured', 'requested': 'wifi', 'vendor_wifi_status': 1,
            'android_mode': actual or '', 'android_summary': summary}


def apply_mode(api, device_id, mode, check):
    label = _LABEL[mode]

    def command(value):
        return api.command(device_id, value).strip()

    def current_state():
        return _network_state(command(_DUMP_COMMAND))

    check()
    if current_state()[0] == mode:
        return
    # Wi-Fi 可用时 Android 自动优先使用它；选择 Wi-Fi 无需禁用 SIM 数据。
    changes = [('wifi', 'wifi_on', '1')] if mode == 'wifi' else [
        ('data', 'mobile_data', '1'), ('wifi', 'wifi_on', '0')]
    pending = []
    for service, setting, wanted in changes:
        previous = command('settings get global ' + setting)
        # 部分镜像将移动数据开关存到订阅配置。保留其现状，仍用实际默认网络确认结果。
        if service == 'data' and previous in ('', 'null'):
            continue
        if previous not in ('0', '1'):
            raise CloudPhoneError('无法读取 Android 网络开关，请在 DuoPlus 客户端选择' + label,
                                  provider='DuoPlus', device=device_id)
        if previous != wanted:
            pending.append((service, setting, wanted, previous))

    def switch(service, value):
        result = command('svc ' + service + (' enable' if value == '1' else ' disable'))
        if re.search(r'error|exception|denied|failure|not found|usage:', result, re.I):
            raise CloudPhoneError('设备拒绝更改 Android 网络开关', provider='DuoPlus', device=device_id)

    undo = []
    try:
        for service, setting, wanted, previous in pending:
            check()
            # 请求超时也可能已生效，先记录原开关用于恢复。
            undo.append((service, setting, previous))
            switch(service, wanted)
        # A running cloud image can still be bringing up Android connectivity.
        for attempt in range(20):
            check()
            actual, summary = current_state()
            if actual == mode:
                return
            if attempt < 19:
                time.sleep(1)
        result = 'Android 当前联网方式为' + _LABEL[actual] if actual else '无法核实 Android 当前联网方式'
        raise CloudPhoneError(f'已请求{label}，{result}（{summary}）。请在 DuoPlus 客户端核对该设备的仿真联网方式',
                              provider='DuoPlus', device=device_id)
    except Exception as exc:
        failures = []
        for service, setting, previous in reversed(undo):
            try:
                switch(service, previous)
                if command('settings get global ' + setting) != previous:
                    failures.append(service)
            except Exception:
                failures.append(service)
        if failures:
            raise CloudPhoneError('切换联网方式失败，原网络开关恢复失败：' + '、'.join(failures),
                                  provider='DuoPlus', device=device_id) from exc
        raise
