"""公司内部 EXE 首次启动配置；已使用的目录不会被内置快照覆盖。"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import uuid

from .paths import atomic_json

SHARED = '通用配置/共用配置.json'
LOCAL = '专属配置/本机设置/'
ALLOWED = {SHARED, *(LOCAL + name for name in (
    '系统.json', '界面偏好.json', '本机程序.json', '方案附件.json', '操作人.json'))}
MARKER = LOCAL + '公司预置初始化.json'
FORMAT = 'window-studio-company-preset'
_lock = threading.RLock()


def bundled_preset():
    if not getattr(sys, 'frozen', False):
        return None
    path = Path(sys._MEIPASS) / '_company_preset/config.json'
    return path if path.is_file() else None


def load_preset(path):
    content = Path(path).read_bytes()
    if len(content) > 20_000_000:
        raise ValueError('公司预置配置超过 20 MB')
    try:
        value = json.loads(content.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('公司预置配置文件损坏') from exc
    if not isinstance(value, dict) or value.get('format') != FORMAT or value.get('version') != 1:
        raise ValueError('公司预置配置版本不受支持')
    files = value.get('files')
    if not isinstance(files, dict) or set(files) - ALLOWED or SHARED not in files:
        raise ValueError('公司预置配置包含未允许的文件')
    if any(not isinstance(data, dict) for data in files.values()):
        raise ValueError('公司预置配置应为 JSON 对象')
    shared = files[SHARED]
    if shared.get('format') != 'window-studio-shared' or shared.get('version') != 1:
        raise ValueError('公司预置通用配置格式不正确')
    for key in ('providers', 'plans', 'proxies', 'weights', 'services'):
        if not isinstance(shared.get(key), dict):
            raise ValueError('公司预置通用配置缺少有效的 ' + key)
    return value, hashlib.sha256(content).hexdigest()


def _target(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError('配置目录不能通过链接指向软件目录之外')
    return path


def _write_missing(path, value):
    """临时文件完整落盘后再发布；文件已存在时绝不覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.preset-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == 'nt':
                # Windows rename 不覆盖已存在的目标，也适用于没有硬链接的移动磁盘。
                temporary.rename(path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _localize(files, root):
    files = deepcopy(files)
    # 操作人可以随后在系统设置修改；首次打开无需员工填写账号密钥或操作人。
    computer = (os.environ.get('COMPUTERNAME') or socket.gethostname() or '新电脑').strip()
    files[LOCAL + '操作人.json'] = {'alias': ('员工-' + computer)[:40], 'revision': 0}
    local = files.get(LOCAL + '本机程序.json', {})
    for fields in local.get('providers', {}).values():
        if not isinstance(fields, dict):
            raise ValueError('公司预置本机程序配置格式不正确')
        for key in ('manager_path', 'browser_path'):
            value = fields.get(key)
            if value and not (Path(value) if Path(value).is_absolute() else root / value).is_file():
                fields[key] = ''  # 由原有程序探测流程查找新电脑的安装位置。
        for key in ('core_root', 'core_directory', 'fingerprint_directory'):
            value = fields.get(key)
            if value and Path(value).is_absolute() and not Path(value).is_dir():
                fields.pop(key)  # 缺失的旧绝对目录回到该厂商默认路径。
    for fields in files.get(LOCAL + '方案附件.json', {}).get('plans', {}).values():
        value = fields.get('apk_path', '')
        if value and not (Path(value) if Path(value).is_absolute() else root / value).is_file():
            fields['apk_path'] = ''
    return files


def initialize_preset(root, *, preset_path=None):
    path = preset_path or bundled_preset()
    if path is None:
        return {'status': 'no_preset'}
    root = Path(root).resolve()
    with _lock:
        marker = _target(root, MARKER)
        state = None
        if marker.exists():
            try:
                state = json.loads(marker.read_text(encoding='utf-8'))
            except (ValueError, OSError) as exc:
                raise ValueError('公司预置初始化记录损坏，未覆盖现有配置') from exc
            if not isinstance(state, dict) or state.get('format') != FORMAT or state.get('status') not in ('pending', 'complete'):
                raise ValueError('公司预置初始化记录无效，未覆盖现有配置')
            if state['status'] == 'complete':
                return {'status': 'already_initialized'}
        elif any((root / name).exists() for name in (
            SHARED, '配置', LOCAL + '实例.json', '专属配置/运行状态/窗口.sqlite')):
            return {'status': 'existing_installation'}
        preset, digest = load_preset(path)
        if state and state.get('preset_sha256') != digest:
            raise ValueError('上次公司预置尚未完成，请先用原内部版启动完成初始化')
        files = _localize(preset['files'], root)
        targets = {name: _target(root, name) for name in files}
        # 所有路径检查和配置处理均在落盘前完成。
        if state is None:
            state = {'format': FORMAT, 'version': 1, 'status': 'pending', 'preset_sha256': digest}
            atomic_json(marker, state)
        written = []
        for name, value in files.items():
            if _write_missing(targets[name], value):
                written.append(name)
        atomic_json(marker, {**state, 'status': 'complete'})
        return {'status': 'initialized', 'files_written': written}
