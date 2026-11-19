"""一次性导航查询；不用于运行窗口存活判断。"""
import json
from urllib.parse import quote
import requests
import websocket


def navigate(port,url,*,timeout=8):
    if not port:raise ValueError('此厂商未提供网页导航通道，请在打开画面后手动输入地址')
    with requests.Session() as session:
        session.trust_env=False
        response=session.put(f'http://127.0.0.1:{port}/json/new?'+quote(url,safe=''),timeout=timeout)
        response.raise_for_status()
        return response.json()


def close_page(port,target_id,*,timeout=3):
    """只关闭明确的标签页 ID，不关闭浏览器或其它标签页。"""
    if not port or not isinstance(target_id,str) or not target_id:
        raise ValueError('缺少待关闭标签页的查询通道或 ID')
    with requests.Session() as session:
        session.trust_env=False
        response=session.get(f'http://127.0.0.1:{int(port)}/json/close/'+quote(target_id,safe=''),timeout=timeout)
        response.raise_for_status()
