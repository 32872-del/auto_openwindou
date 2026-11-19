"""窗口资料与下次启动配置的受控编辑；不热切换、不购买/重置云手机。"""
from copy import deepcopy
import math
import time

from . import providers
from .table_import import validate_item, FIELDS
from .storage import Conflict
from .resource_state import confirmed_absent, unresolved_creation

EDIT_FIELDS = {'name', 'address', 'note', 'input_data', 'planned_device', 'network_mode',
               'allow_city_fallback', 'proxy_mode', 'proxy_sources', 'ip_family',
               'website_test', 'tls_test'}


def edit_reason(engine, row):
    identity = row['id']
    if engine.stop.is_set():
        return '软件正在退出，请重新打开后再编辑'
    if row.get('_owner') != engine.owner:
        return '环境归属无法确认，不能修改此记录'
    if row.get('trashed'):
        return '请先从回收站恢复，再编辑'
    if row.get('status') not in ('stopped', 'closed', 'failed', 'cancelled'):
        return '请先关闭窗口，等待清理完成后再编辑；保存不会自动关闭或重启设备'
    if any(identity in getattr(engine, field, ()) for field in ('sessions', 'closing', '_starting', '_viewer_opening')):
        return '窗口任务尚未结束，请稍后再编辑'
    if unresolved_creation(row) or not (row.get('_cleanup_complete') or confirmed_absent(row)):
        return '环境清理结果尚未确认，请先处理清理错误'
    if row.get('pid'):
        from .cleanup import process_state
        if process_state(row['pid'], row.get('created')) != 'exited':
            return '原窗口进程仍在运行或无法确认退出，暂不能编辑'
    return ''


def _devices(engine, row):
    from server.python.cloud_phone_s.framework.device_pool import DeviceLease
    provider = providers.build_cloud(row['provider'], row['_config'], engine.store.root)
    try:
        reserved = engine.store.reserved_cloud_devices(row['provider'], exclude=row['id']) | set(DeviceLease.owners(row['provider']))
        current = row.get('profile_id') or row.get('planned_device')
        result = []
        for device in provider.list_devices():
            if device.id in reserved or (device.id != current and device.status != 'idle'):
                continue
            public = device.public()
            result.append({key: public.get(key, '') for key in ('id', 'name', 'status', 'brand', 'model', 'system')})
        return result
    finally:
        provider.close()


def edit_options(engine, identity):
    from .api import public_row
    from .engine import _allow_state_fallback
    with engine.lock:
        row = deepcopy(engine.store.get_run(identity))
        reason = edit_reason(engine, row)
        values = {key: row.get(key, '') for key in ('name', 'address', 'note', 'planned_device')}
        values.update(input_data=deepcopy(row.get('input_data') or {}),
                      network_mode=row.get('_config', {}).get('network_mode', 'keep'),
                      allow_city_fallback=_allow_state_fallback(row),
                      ip_family=row.get('_ip_family', 'any'),
                      website_test=bool(row.get('_website_test')), tls_test=bool(row.get('_tls_test')))
        values['input_data'].setdefault('email', row.get('account_email', ''))
        values['planned_device'] = row.get('profile_id') or row.get('planned_device', '')
        request = row.get('_plan', {}).get('proxy_request', {})
        values.update(proxy_mode=request.get('proxy_mode', 'selected' if row.get('_plan', {}).get('proxy_sources') else 'direct'),
                      proxy_sources=list(row.get('_plan', {}).get('proxy_sources', [])))
        result = dict(row=public_row(row), values=values, expected_updated=row['updated'],
                      editable=not reason, reason=reason, devices=[], devices_error='',
                      editable_fields=sorted(EDIT_FIELDS),
                      message='保存资料只影响本地记录及下次启动，不迁移、重置或删除原设备数据。更换设备后原设备仍保留。')
    if not reason and row['kind'] in ('phone', 'app'):
        try:
            result['devices'] = _devices(engine, row)
        except Exception:
            # 厂商错误可能带请求参数；不要向前端泄漏配置或密钥。
            result['devices_error'] = '读取厂商设备失败；资料仍可编辑，更换设备需要成功刷新并在保存时重新校验'
    return result


def edit_run(engine, identity, body):
    from .engine import _allow_state_fallback
    if not isinstance(body, dict) or set(body) - EDIT_FIELDS - {'expected_updated'}:
        raise ValueError('编辑内容包含不允许修改的字段')
    expected = body.get('expected_updated')
    if isinstance(expected, bool) or not isinstance(expected, (int, float)) or not math.isfinite(expected):
        raise ValueError('请提供有效的窗口版本，重新打开编辑后保存')
    if not (set(body) & EDIT_FIELDS):
        raise ValueError('没有需要保存的修改')
    with engine._record_action([identity]), engine.lock:
        row = deepcopy(engine.store.get_run(identity))
        if row.get('updated') != expected:
            raise Conflict('窗口记录已变化，请重新打开编辑后保存')
        reason = edit_reason(engine, row)
        if reason:
            raise ValueError(reason)
        supplied = body.get('input_data', {})
        if not isinstance(supplied, dict) or set(supplied) - set(FIELDS) - {'source_row'}:
            raise ValueError('资料包含未知字段')
        from .email_accounts import row_email
        base = dict(name=row.get('name', ''), address=row.get('address', ''), note=row.get('note', ''),
                    email=row_email(row), url=row.get('url', ''), source_row=row.get('source_row') or 1,
                    **(row.get('_location') or row.get('location') or {}))
        raw = {**base, **(row.get('input_data') or {}), **supplied}
        for key in ('address', 'note'):
            if key in body:
                raw[key] = body[key]
        item = validate_item(raw)
        old_email = row.get('account_email') or (row.get('input_data') or {}).get('email') or ''
        if old_email and not item['email']:
            raise ValueError('已绑定邮箱不能为空')
        row['input_data'] = item
        row['address'], row['note'] = item['address'], item['note']
        location = {key: item[key] for key in ('country', 'state', 'city', 'zip')}
        location_changed = location != row.get('_location', row.get('location', {}))
        row.update(location=location, _location=deepcopy(location))
        if item['email']:
            row['account_email'] = item['email']
            if old_email != item['email'] and 'name' not in body:
                row['name'] = item['email']
        if 'name' in body:
            name = body['name']
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
                raise ValueError('窗口名称应为 1～80 字，不能含控制字符')
            row['name'] = name.strip()
        row['_plan'] = deepcopy(row['_plan'])
        row['_config'] = deepcopy(row['_config'])
        if 'url' in supplied:
            row['url'] = item['url'] or 'about:blank'
            row['_plan']['url'] = row['url']
        phone = row['kind'] in ('phone', 'app')
        if not phone and any(key in body for key in ('planned_device', 'network_mode', 'allow_city_fallback')):
            raise ValueError('设备、联网和城市匹配选项仅适用于云手机')
        proxy_changed = location_changed
        if 'allow_city_fallback' in body:
            if not isinstance(body['allow_city_fallback'], bool):
                raise ValueError('城市匹配选项需要开关值')
            proxy_changed |= body['allow_city_fallback'] != _allow_state_fallback(row)
            row['_allow_city_fallback'] = body['allow_city_fallback']
            row['allow_city_fallback'] = body['allow_city_fallback']
        if 'network_mode' in body:
            if body['network_mode'] not in ('keep', 'wifi', 'mobile'):
                raise ValueError('请选择有效的云手机联网方式')
            row['_config']['network_mode'] = body['network_mode']
            row['network_mode'] = body['network_mode']
        if 'ip_family' in body:
            if body['ip_family'] not in ('any', 'ipv4', 'ipv6', 'dual'):
                raise ValueError('IP 类型无效')
            proxy_changed |= body['ip_family'] != row.get('_ip_family', 'any')
            row['_ip_family'] = row['requested_ip_family'] = body['ip_family']
        for flag in ('website_test', 'tls_test'):
            if flag in body:
                if not isinstance(body[flag], bool):
                    raise ValueError('检测选项需要开关值')
                if body[flag] and (phone or row['provider'] == 'ruyi'):
                    raise ValueError('自动网页检测仅适用于 Chromium 电脑浏览器')
                row['_' + flag] = body[flag]
        if 'proxy_mode' in body or 'proxy_sources' in body:
            from . import proxy
            request = dict(row['_plan'].get('proxy_request', {}))
            request.update({key: body[key] for key in ('proxy_mode', 'proxy_sources') if key in body})
            document = engine.store.proxies()
            sources, origin = proxy.resolve_selection(document, request, row['_plan'])
            row['_plan'].update(proxy_request=request, proxy_sources=sources, proxy_origin=origin)
            row.update(proxy_origin=origin, _proxy_document=deepcopy(document))
            proxy_changed = True
        if 'planned_device' in body:
            device = body['planned_device']
            if not isinstance(device, str) or not device.strip() or len(device) > 300:
                raise ValueError('请选择有效的云手机编号')
            device = device.strip()
            if device != (row.get('profile_id') or row.get('planned_device')):
                choices = {choice['id']: choice for choice in _devices(engine, row)}
                if device not in choices or choices[device]['status'] != 'idle':
                    raise ValueError('目标设备不是可分配的空闲设备，或已绑定其他邮箱，请刷新后重选')
                row.setdefault('device_binding_history', []).append(dict(device=row.get('profile_id') or row.get('planned_device'), at=time.time()))
                row['planned_device'] = device
                row['_config'].update(device_selection='selected', device_ids=[device], device_brands=[], device_models=[], device_versions=[])
                row.update(profile_id='', _cloud_owned=False, _cloud_started=False, _cloud_resume_unstarted=True,
                           _deleted=False, creation_state='not_requested', status='stopped',
                           stage='设备绑定已修改，等待下次打开', pid=0, created=0, hwnd=0)
                for key in ('device_brand', 'device_model', 'device_system', 'device_name', 'os'):
                    row.pop(key, None)
                proxy_changed = True
        if proxy_changed:
            row.update(_proxy_update_pending=True, _proxy=None, ip='', preflight_ip='', geo_verified=False,
                       proxy_fallback=False, proxy_city_unavailable=False, proxy_city_hint='',
                       chrome_egress={}, device_egress={}, vendor_proxy={}, network_report={})
        row['edited_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        row['edit_revision'] = int(row.get('edit_revision', 0)) + 1
        # 版本校验、邮箱重新绑定与窗口写入在同一数据库事务中完成，冲突时整体回滚。
        saved = engine.store.put_run(row, expected_updated=expected, rebind=True)
        return saved
