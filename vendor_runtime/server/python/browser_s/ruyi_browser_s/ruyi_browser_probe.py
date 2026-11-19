"""执行端 ruyi 组件、指纹内核探针和自动安装。"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from server.python.utils_s.path_utils import get_server_paths

from .ruyi_browser_runtime_setting import RuyiBrowserRuntimeSetting


_install_lock = threading.Lock()

# ruyipage 1.2.43 / 151-ruyi 官方 GitHub Release 的固定发布摘要。
# 官方安装器默认不校验哈希，因此执行端必须先验证归档，才允许解压。
_TRUSTED_RUNTIME_ASSETS: dict[str, tuple[int, str]] = {
    "firefox-151.0a1.en-US.win64.zip": (
        128_276_441,
        "8a724622bef11d7d1049a5e9fb8d8fedd8ec987eaa65d2ec2a6ca8cac0a65f77",
    ),
    "firefox-151.0a1.en-US.linux-x86_64.tar.xz": (
        103_125_296,
        "34951094a30488a38cf54ea7b9f04a62ae127c467492cefae5231dcca5248bce",
    ),
}


@dataclass(frozen=True, slots=True)
class RuyiProbeResult:
    """Ruyi 探针结果。"""

    package_ready: bool
    browser_path: Path | None
    source: str
    message: str
    version: str = ""

    @property
    def ready(self) -> bool:
        return (
            self.package_ready
            and self.browser_path is not None
            and self.browser_path.is_file()
        )


def probe_ruyi(setting: RuyiBrowserRuntimeSetting) -> RuyiProbeResult:
    """检查执行端 Python 组件、指定内核和应用内托管内核。"""

    try:
        import ruyipage
    except ImportError:
        return RuyiProbeResult(
            package_ready=False,
            browser_path=None,
            source="组件缺失",
            message="执行端没有安装 ruyi 组件，请先安装 ruyiPage",
        )

    version = str(getattr(ruyipage, "__version__", "") or "")
    paths = get_server_paths()
    configured = str(setting.browser_path or "").strip()
    if configured:
        candidate = paths.resolve_resource(configured)
        if candidate.is_dir():
            candidate = candidate / "firefox.exe"
        if candidate.is_file():
            return RuyiProbeResult(
                package_ready=True,
                browser_path=candidate,
                source="执行端设置",
                message="已使用执行端设置的 ruyi 内核",
                version=version,
            )

    try:
        from ruyipage._runtime import get_executable_path
    except ImportError:
        return RuyiProbeResult(
            package_ready=True,
            browser_path=None,
            source="安装器缺失",
            message="执行端的 ruyi 组件缺少内核安装器，请更新 ruyiPage",
            version=version,
        )

    core_root = paths.resolve_resource(setting.core_root)
    managed = get_executable_path(root=str(core_root), strict=False)
    if managed:
        candidate = Path(managed)
        if candidate.is_file():
            return RuyiProbeResult(
                package_ready=True,
                browser_path=candidate,
                source="执行端托管内核",
                message="已找到执行端托管的 ruyi 内核",
                version=version,
            )

    if core_root.is_dir():
        executable_name = "firefox.exe"
        for candidate in sorted(core_root.rglob(executable_name)):
            if candidate.is_file():
                return RuyiProbeResult(
                    package_ready=True,
                    browser_path=candidate,
                    source="执行端自动发现",
                    message="已在执行端缓存目录找到 ruyi 内核",
                    version=version,
                )

    action = (
        "，启动浏览器时会自动下载"
        if setting.auto_download_core
        else "，请在执行端下载或选择 ruyi 内核"
    )
    return RuyiProbeResult(
        package_ready=True,
        browser_path=None,
        source="内核缺失",
        message=f"执行端没有找到 ruyi 内核{action}",
        version=version,
    )


def install_ruyi_core(setting: RuyiBrowserRuntimeSetting) -> Path:
    """使用 ruyiPage 自带安装器下载、校验、解压到执行端缓存目录。"""

    from ruyipage._runtime import get_executable_path, install
    from ruyipage._runtime.downloads import download
    from ruyipage._runtime.installer import runtime_info
    from ruyipage._runtime.manifest import runtime_url

    core_root = get_server_paths().resolve_resource(setting.core_root)
    core_root.mkdir(parents=True, exist_ok=True)
    with _install_lock:
        existing = get_executable_path(root=str(core_root), strict=False)
        if existing and Path(existing).is_file():
            return Path(existing)
        info = runtime_info()
        asset = str(info.get("asset") or "").strip()
        if asset not in _TRUSTED_RUNTIME_ASSETS:
            raise RuntimeError(
                "当前 Ruyi 内核版本没有可信校验值，请先更新执行端"
            )
        source_url = runtime_url(
            info,
            base_url=setting.download_base_url or None,
        )
        archive = Path(download(
            source_url,
            asset,
            root=str(core_root),
            quiet=True,
        ))
        try:
            _verify_ruyi_archive(archive, asset)
        except Exception:
            archive.unlink(missing_ok=True)
            raise
        values = {
            "root": str(core_root),
            "quiet": True,
            "from_file": str(archive),
        }
        result = install(**values)
        installed = Path(str(result.get("executable_path") or ""))
        if not installed.is_file():
            raise RuntimeError("ruyi 内核安装完成后仍未找到浏览器程序")
        return installed


def _verify_ruyi_archive(path: Path, asset: str) -> None:
    """校验 Ruyi 官方发布包大小和 SHA-256，禁止先解压后校验。"""

    try:
        expected_size, expected_digest = _TRUSTED_RUNTIME_ASSETS[asset]
    except KeyError as exc:
        raise RuntimeError("Ruyi 内核发布包没有可信校验值") from exc
    if not path.is_file() or path.stat().st_size != expected_size:
        raise RuntimeError("Ruyi 内核发布包大小校验失败")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest().lower() != expected_digest.lower():
        raise RuntimeError("Ruyi 内核发布包 SHA-256 校验失败")


__all__ = ["RuyiProbeResult", "install_ruyi_core", "probe_ruyi"]
