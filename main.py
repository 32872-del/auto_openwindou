"""单软件入口：本机 API + 独立 WebView 窗口。"""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
import uuid
import traceback
import uvicorn
from launcher.api import create_app


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--server-only',action='store_true')
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--root',default=str(Path(sys.executable if getattr(sys,'frozen',False) else __file__).resolve().parent))
    args=parser.parse_args()
    root=Path(args.root).resolve()
    root_mutex=None
    if os.name=='nt':
        import win32api,win32event,winerror
        root_key=hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
        root_mutex=win32event.CreateMutex(None,False,'Local\\WindowStudioPath_'+root_key)
        if win32api.GetLastError()==winerror.ERROR_ALREADY_EXISTS:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'此目录的软件已经打开，请使用原窗口。','自动创建窗口',0)
            return
    from launcher.company_preset import initialize_preset
    initialize_preset(root)
    from launcher.storage import Store
    store=Store(root)
    identity_file=store.paths.local/'实例.json'
    if identity_file.exists():identity=json.loads(identity_file.read_text(encoding='utf-8'))['uuid']
    else:
        identity=uuid.uuid4().hex
        identity_file.write_text(json.dumps({'uuid':identity},indent=2),encoding='utf-8')
    mutex=None
    if os.name=='nt':
        import win32api,win32event,winerror
        mutex=win32event.CreateMutex(None,False,'Local\\WindowStudio_'+identity)
        if win32api.GetLastError()==winerror.ERROR_ALREADY_EXISTS:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'此软件已经打开。请使用原窗口。\n如需独立副本，只复制通用配置；或关闭后移除新副本的“专属配置/本机设置/实例.json”再启动。','自动创建窗口',0)
            return
    app=create_app(root)
    from server.python.runtime_s.desktop_scope import configure_desktop_scope
    configure_desktop_scope(identity)
    logging.basicConfig(filename=store.paths.logs/'适配器.log',level=logging.INFO,encoding='utf-8',format='%(asctime)s %(levelname)s %(message)s')
    sock=socket.socket()
    port=args.port
    while True:
        try:sock.bind(('127.0.0.1',port));break
        except OSError:
            if not port or port>=65535:raise
            port+=1
    sock.listen(128);port=sock.getsockname()[1]
    # pythonw 无 stdout/stderr；沿用文件日志，避免默认彩色日志调用 isatty 导致闪退。
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning',log_config=None,access_log=False))
    if args.server_only:
        print(f'http://127.0.0.1:{port}',flush=True)
        server.run(sockets=[sock]);return
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True,name='本机界面服务')
    thread.start()
    deadline=time.monotonic()+10
    while not server.started:
        if not thread.is_alive() or time.monotonic()>deadline:raise RuntimeError('本机界面服务未启动，请查看日志')
        time.sleep(.05)
    import webview
    from launcher.clipboard import ClipboardBridge
    alias=app.state.store.system()['alias']
    window=webview.create_window((alias+' · ' if alias else '')+'自动创建窗口',f'http://127.0.0.1:{port}',width=1440,height=920,min_size=(850,620),js_api=ClipboardBridge())
    try:webview.start(storage_path=str(store.paths.cache/'界面'),private_mode=False)
    finally:
        server.should_exit=True;thread.join(timeout=10)
        sock.close()
        if mutex:win32api.CloseHandle(mutex)
        if root_mutex:win32api.CloseHandle(root_mutex)


if __name__=='__main__':
    try:
        if '--v2-worker' in sys.argv:
            from launcher.v2_worker import main as worker_main
            raise SystemExit(worker_main())
        if '--diagnostic-worker' in sys.argv:
            from vendor_runtime.legacy_core.environment_dp_worker import main as worker_main
            raise SystemExit(worker_main())
        main()
    except Exception as exc:
        # pythonw 没有控制台；启动失败必须留下可读原因，不能无声退出。
        base=Path(sys.executable if getattr(sys,'frozen',False) else __file__).resolve().parent
        if '--root' in sys.argv:
            base=Path(sys.argv[sys.argv.index('--root')+1]).resolve()
        path=base/'专属配置/日志/启动错误.log'
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a',encoding='utf-8') as stream:
            stream.write(time.strftime('%Y-%m-%d %H:%M:%S')+'\n'+traceback.format_exc()+'\n')
        if os.name=='nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,f'启动失败：{type(exc).__name__}: {exc}\n\n完整原因已保存到：{path}','自动创建窗口 · 启动失败',0x10)
        else:
            raise
