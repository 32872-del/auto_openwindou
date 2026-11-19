"""软件位置与用户数据位置分离；所有新数据均进入两个规定目录。"""
import json
import os
from pathlib import Path
import uuid


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default):
    from copy import deepcopy
    path = Path(path)
    if not path.exists():
        return deepcopy(default)
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('顶层应为对象')
        return value
    except (ValueError, OSError) as exc:
        raise ValueError(f'无法读取 {path}：{exc}；未覆盖原文件') from exc


class Layout:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.common = self.root / '通用配置'
        self.shared = self.common / '共用配置.json'
        self.private = self.root / '专属配置'
        self.local = self.private / '本机设置'
        self.state = self.private / '运行状态'
        self.logs = self.private / '日志'
        self.archives = self.private / '网页存档'
        self.cache = self.private / '文件缓存'
        for path in (self.common, self.local, self.state, self.logs, self.archives, self.cache):
            path.mkdir(parents=True, exist_ok=True)

    def local_file(self, virtual):
        """适配已有服务调用的虚拟路径，拒绝任意路径写出。"""
        path = Path(virtual)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('本机数据路径必须在专属配置目录内')
        text = path.as_posix()
        exact = {'状态/操作人.json': self.local / '操作人.json',
                 '状态/实例.json': self.local / '实例.json',
                 '配置/系统.json': self.local / '系统.json',
                 '配置/本机程序.json': self.local / '本机程序.json'}
        if text in exact:
            return exact[text]
        prefixes = {'状态': self.state, '日志': self.logs, '文件缓存': self.cache,
                    '检测存档': self.archives, '本机设置': self.local}
        if path.parts and path.parts[0] in prefixes:
            return prefixes[path.parts[0]].joinpath(*path.parts[1:])
        raise ValueError('未登记的数据保存位置：' + text)

    def public(self):
        return {key: './' + path.relative_to(self.root).as_posix() for key, path in {
            'config': self.shared, 'local': self.local, 'state': self.state,
            'logs': self.logs, 'cache': self.cache, 'archives': self.archives}.items()}
