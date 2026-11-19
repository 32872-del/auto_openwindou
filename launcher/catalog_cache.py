"""按厂商连接与系统版本保存型号目录；不缓存设备状态或修改操作。"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import threading
from .paths import atomic_json

_guard = threading.Lock()
_locks = {}


def load(root, provider, section, config, fetch, *, version='', refresh=False):
    if not isinstance(refresh, bool):
        raise ValueError('刷新目录标记应为开关值')
    if not re.fullmatch(r'[a-z0-9_-]+', provider + '-' + section):
        raise ValueError('型号目录名称无效')
    # 账号及 API 地址变更时独立建缓存；型号选择/权重变化不影响目录。
    connection = {k: config.get(k, '') for k in (
        'api_url', 'local_api_url', 'remote_api_url', 'api_key', 'api_token', 'app_id', 'auth_mode')}
    identity = hashlib.sha256(json.dumps([connection, str(version)], sort_keys=True).encode()).hexdigest()[:24]
    path = Path(root).resolve() / '专属配置/文件缓存/型号目录' / provider / f'{section}-{identity}.json'
    with _guard:
        lock = _locks.setdefault(str(path), threading.Lock())
    with lock:
        cached = None
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
            if entry.get('schema') == 1 and isinstance(entry.get('data'), dict) and isinstance(entry.get('saved_at'), str):
                key = 'models' if section == 'android' else 'items'
                if isinstance(entry['data'].get(key), list):
                    cached = entry
        except (OSError, ValueError, TypeError, AttributeError):
            pass

        def result(entry, hit, error=''):
            data = deepcopy(entry['data'])
            data['cache'] = {'hit': hit, 'saved_at': entry['saved_at'], 'refresh_error': error}
            return data

        if cached and not refresh:
            return result(cached, True)
        try:
            data = fetch()
            key = 'models' if section == 'android' else 'items'
            if not isinstance(data, dict) or not isinstance(data.get(key), list):
                raise ValueError('厂商型号目录格式不正确，未覆盖缓存')
        except Exception as exc:
            if cached:
                return result(cached, True, f'{type(exc).__name__}: {exc}')
            raise
        entry = {'schema': 1, 'saved_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'data': data}
        try:
            atomic_json(path, entry)
        except OSError as exc:
            data = deepcopy(data)
            data['cache'] = {'hit': False, 'saved_at': '', 'refresh_error': f'列表已取得，但本地缓存写入失败：{exc}'}
            return data
        return result(entry, False)
