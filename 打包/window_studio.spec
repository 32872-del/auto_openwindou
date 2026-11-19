# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
datas = [(str(ROOT / 'ui'), 'ui'),
         (str(ROOT / 'launcher/resources'), 'launcher/resources'),
         (str(ROOT / '功能清单.md'), '.')]
company_preset = os.environ.get('WINDOW_STUDIO_COMPANY_PRESET', '')
if company_preset:
    from launcher.company_preset import load_preset
    load_preset(company_preset)
    datas.append((company_preset, '_company_preset'))
binaries = []
hidden = ['main', 'package_check', 'win32timezone', 'socks']
# 厂商适配器按名称动态导入，必须完整收集；不收集配置、测试和浏览器资料。
for path in (ROOT / 'vendor_runtime').rglob('*.py'):
    relative = path.relative_to(ROOT / 'vendor_runtime') if path.parts[len(ROOT.parts) + 1] == 'server' else path.relative_to(ROOT)
    parts = list(relative.with_suffix('').parts)
    if parts[-1] == '__init__':
        parts.pop()
    hidden.append('.'.join(parts))
for path in (ROOT / 'vendor_runtime').rglob('*.dll'):
    binaries.append((str(path), str(path.parent.relative_to(ROOT))))
    binaries.append((str(path), str(path.parent.relative_to(ROOT / 'vendor_runtime'))))
for package in ('DrissionPage', 'webview', 'ruyipage', 'helixwright'):
    package_data, package_bins, package_imports = collect_all(package)
    datas += package_data
    binaries += package_bins
    hidden += package_imports
hidden += collect_submodules('uvicorn')
for package in ('pywebview', 'pythonnet', 'DrissionPage', 'ruyipage', 'helixwright'):
    datas += copy_metadata(package)

a = Analysis([str(ROOT / '打包/entry.py')],
    pathex=[str(ROOT), str(ROOT / '打包'), str(ROOT / 'vendor_runtime')],
    binaries=binaries, datas=datas, hiddenimports=sorted(set(hidden)),
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'cefpython3', 'gtk', 'gi', 'matplotlib', 'pytest'],
    noarchive=False)
pyz = PYZ(a.pure)
# 控制台在引导阶段隐藏，同时保留 stdin/stdout 管道供自动检测 worker 使用。
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
    name='自动创建窗口_公司内部版' if company_preset else '自动创建窗口', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True, hide_console='hide-early',
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None)
