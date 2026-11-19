"""独立管理软件只打开厂商官方控制页：无ADB、无DP、无元素动作。"""
from pathlib import Path
from .isolated_viewer import IsolatedViewer

class ManagementWindow:
    def __init__(self,viewer):
        import psutil
        self.viewer=viewer
        self.created=psutil.Process(viewer.pid).create_time()
    def describe(self):
        return {**self.viewer.call('describe'), 'created':self.created}
    def show(self):return self.viewer.call('show')
    def is_open(self):
        import psutil,win32gui,win32process
        try:
            return (win32gui.IsWindow(self.viewer.hwnd) and
                win32process.GetWindowThreadProcessId(self.viewer.hwnd)[1]==self.viewer.pid and
                abs(psutil.Process(self.viewer.pid).create_time()-self.created)<.01)
        except psutil.NoSuchProcess:return False
    def close(self):
        if not self.viewer.close():raise RuntimeError('控制窗口尚未完全退出，请重试关闭')

def open_management_window(provider,device,name,url=None):
    if provider.key=='duoplus':
        from .isolated_window import share_connection
        url=share_connection(provider.api,device,provider.config_root)
    else:
        from .providers.geelark.viewer import validate_url
        url=validate_url(url,device)
    root=Path(provider.config_root).parent/'文件缓存/云手机控制页'
    viewer=IsolatedViewer(root,url,device_identity=provider.key+':'+device,manual=True,visible=True)
    try:
        viewer.start()
        return ManagementWindow(viewer)
    except BaseException:
        viewer.close()
        raise
