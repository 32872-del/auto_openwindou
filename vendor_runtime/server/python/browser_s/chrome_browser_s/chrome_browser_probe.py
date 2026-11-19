"""执行端本地 Chrome 探针与设置文件准备。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from server.python.utils_s.path_utils import get_server_paths


@dataclass(frozen=True, slots=True)
class ChromeProbeResult:
    """Chrome 探针返回的最小、可测试结果。"""

    browser_path: Path | None
    source: str
    message: str

    @property
    def ready(self) -> bool:
        return self.browser_path is not None and self.browser_path.is_file()


def probe_chrome(
    *,
    browser_path: str = "",
    ini_path: str = "",
) -> ChromeProbeResult:
    """先检查执行端设置，再调用 DrissionPage 的系统探针。"""

    paths = get_server_paths()
    configured = str(browser_path or "").strip()
    if configured:
        candidate = paths.resolve_resource(configured)
        if candidate.is_dir():
            candidate = candidate / "chrome.exe"
        if candidate.is_file():
            return ChromeProbeResult(
                browser_path=candidate,
                source="执行端设置",
                message="已使用执行端设置的 Chrome",
            )
        return ChromeProbeResult(
            browser_path=None,
            source="执行端设置无效",
            message="执行端没有找到设置的 Chrome，请重新选择 chrome.exe",
        )

    from DrissionPage._functions.browser import get_chrome_path

    ini_file = str(ini_path or "").strip()
    resolved_ini = (
        paths.resolve_resource(ini_file)
        if ini_file
        else None
    )
    detected = get_chrome_path(
        str(resolved_ini) if resolved_ini and resolved_ini.is_file() else None
    )
    candidate = Path(str(detected or ""))
    if candidate.is_file():
        return ChromeProbeResult(
            browser_path=candidate,
            source="系统自动发现",
            message="已在执行端自动找到 Chrome",
        )
    return ChromeProbeResult(
        browser_path=None,
        source="未找到",
        message="执行端没有找到 Chrome，请先在执行端安装或选择 chrome.exe",
    )


def ensure_chrome_ini(ini_path: str) -> Path | None:
    """在执行端创建 Chrome 设置文件；空值表示使用 DrissionPage 默认设置。"""

    value = str(ini_path or "").strip()
    if not value:
        return None
    path = get_server_paths().resolve_resource(value)
    if path.is_file():
        return path

    from DrissionPage import ChromiumOptions

    path.parent.mkdir(parents=True, exist_ok=True)
    ChromiumOptions().save(str(path))
    return path


__all__ = ["ChromeProbeResult", "ensure_chrome_ini", "probe_chrome"]
