"""版本化发布的原生库与运行副本；旧进程可以继续持有旧版本。"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def valid_library_name(name: str, logical_name: str) -> bool:
    stem = Path(logical_name).stem
    return name.casefold() == logical_name.casefold() or bool(
        re.fullmatch(re.escape(stem) + r'\.[a-f0-9]{64}\.dll', name.casefold())
    )


def published_library(candidate: Path) -> Path:
    """清单存在时必须按清单加载；禁止失败后静默退回被覆盖遗留的旧DLL。"""
    manifest = candidate.parent / 'native-manifest.json'
    if not manifest.is_file():
        return candidate
    content = json.loads(manifest.read_text(encoding='utf-8'))
    entry = content.get('libraries', {}).get(candidate.name)
    if not isinstance(entry, dict):
        raise RuntimeError(f'原生组件清单缺少 {candidate.name}：{manifest}')
    name, digest = entry.get('file', ''), entry.get('sha256', '')
    if not valid_library_name(name, candidate.name) or Path(name).name != name:
        raise RuntimeError(f'原生组件清单文件名无效：{manifest}')
    selected = candidate.parent / name
    if not selected.is_file() or sha256(selected) != digest:
        raise RuntimeError(f'原生组件缺失或校验失败，请重新覆盖完整依赖：{selected}')
    return selected


def runtime_library(source: Path, *, cache_root: Path | None = None) -> Path:
    if cache_root is None:
        from server.python.utils_s.path_utils import get_server_paths
        cache_root = get_server_paths().runtime_cache_dir / 'native'
    digest = sha256(source)
    target = Path(cache_root) / digest / source.name
    if target.is_file():
        if sha256(target) != digest:
            raise RuntimeError(f'原生组件缓存校验失败：{target}')
        return target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f'.{source.name}.{uuid.uuid4().hex}.tmp')
    try:
        shutil.copy2(source, temporary)
        if sha256(temporary) != digest:
            raise RuntimeError('原生组件复制时内容变化，请完成更新后再启动')
        try:
            temporary.replace(target)
        except OSError:
            if not target.is_file() or sha256(target) != digest:
                raise
        return target.resolve()
    finally:
        temporary.unlink(missing_ok=True)
