"""Native text clipboard for user-initiated desktop UI copy/paste."""
import time


class ClipboardBridge:
    def select_program(self):
        import webview
        if not webview.windows:return {'path':''}
        files=webview.windows[0].create_file_dialog(webview.FileDialog.OPEN,allow_multiple=False,file_types=('程序 (*.exe)',))
        return {'path':str(files[0]) if files else ''}

    def set_title(self, alias):
        import webview
        if isinstance(alias,str) and 1<=len(alias)<=40 and webview.windows:
            webview.windows[0].set_title(alias+' · 自动创建窗口')
            return {'ok':True}
        return {'ok':False}

    def read_clipboard(self):
        try:
            import win32clipboard as clip
            import win32con
            for attempt in range(5):
                try:
                    clip.OpenClipboard()
                    break
                except Exception:
                    if attempt == 4:raise
                    time.sleep(.05)
            try:
                return {'text': clip.GetClipboardData(win32con.CF_UNICODETEXT) if clip.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT) else ''}
            finally:
                clip.CloseClipboard()
        except Exception as exc:
            return {'error': f'读取剪贴板失败：{exc}'}

    def write_clipboard(self, text):
        try:
            import win32clipboard as clip
            import win32con
            if not isinstance(text, str):raise ValueError('只能复制文本')
            for attempt in range(5):
                try:
                    clip.OpenClipboard()
                    break
                except Exception:
                    if attempt == 4:raise
                    time.sleep(.05)
            try:
                clip.EmptyClipboard()
                clip.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                clip.CloseClipboard()
            return {'ok': True}
        except Exception as exc:
            return {'error': f'复制失败：{exc}'}
