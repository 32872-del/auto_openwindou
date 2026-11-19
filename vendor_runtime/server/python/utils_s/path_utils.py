"""只指向新版根目录的路径接口。"""
import os
from pathlib import Path


class Paths:
    def __init__(self):
        self.root = Path(os.environ['WINDOW_STUDIO_DATA']).resolve()
        self.code_root = Path(os.environ['WINDOW_STUDIO_CODE']).resolve()
        self.state_root = self.root / '运行状态'
        self.runtime_cache_dir = self.root / '文件缓存/运行依赖'
        self.browser_cache_dir = self.root / '文件缓存/浏览器'
        self.config_dir = self.root / '本机设置'
        self.bundled_resource_root = self.code_root / 'vendor_runtime/server'
        for path in (self.runtime_cache_dir,self.browser_cache_dir):
            path.mkdir(parents=True,exist_ok=True)

    def resolve_resource(self, value):
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.root/path).resolve()


def get_server_paths():
    return Paths()
