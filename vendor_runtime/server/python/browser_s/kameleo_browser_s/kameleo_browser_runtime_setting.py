from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class KameleoBrowserRuntimeSetting:
    local_api_url: str = "http://127.0.0.1:5050"
    request_timeout: float = 8
    start_timeout: float = 120

    def __post_init__(self):
        url = self.local_api_url.strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Kameleo 隔离运行须使用执行端本机 Engine 地址，例如 http://127.0.0.1:5050")
        if min(self.request_timeout, self.start_timeout) <= 0:
            raise ValueError("Kameleo 等待时间必须大于 0")
        object.__setattr__(self, "local_api_url", url)
