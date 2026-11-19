"""本机 API 专用的 HTTP 调用：绝不走任何代理。

为什么要单独一层：`requests` 的 `proxies={"http": None, "https": None}` **挡不住
`ALL_PROXY`**——merge_setting 会把值为 None 的键直接删掉，环境里的 `ALL_PROXY`
照样生效，于是连 `127.0.0.1` 的本地接口都被甩去代理，返回 502。
唯一可靠的做法是 `Session.trust_env = False`：整个会话不读环境代理变量。

每次新建 Session（而不是共用一个）是有意为之：这些调用分散在多个后台线程里，
requests.Session 并不保证线程安全，本地请求本来就便宜，省这点开销不值当。
"""

from __future__ import annotations

import requests

DEFAULT_TIMEOUT = 10


def request(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    kwargs.pop("proxies", None)          # 交给 trust_env=False，别再自己拼 proxies
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, url, **kwargs)


def get(url: str, **kwargs) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    return request("POST", url, **kwargs)
