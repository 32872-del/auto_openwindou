"""浏览器公共实现：从统一配置构建各家指纹浏览器。"""

from __future__ import annotations

from ..adspower_browser_s.adspower_browser import AdsPowerBrowser
from ..adspower_browser_s.adspower_browser_runtime_setting import AdsPowerBrowserRuntimeSetting
from ..bit_browser_s.bit_browser import BitBrowser
from ..bit_browser_s.bit_browser_runtime_setting import BitBrowserRuntimeSetting
from ..chrome_browser_s.chrome_browser import ChromeBrowser
from ..chrome_browser_s.chrome_browser_runtime_setting import ChromeBrowserRuntimeSetting
from .browser_base import BrowserBase
from .browser_catalog import BrowserCatalog, BrowserRegistration
from ..browser_task_setting import BrowserSettings
from ..hidemium_browser_s.hidemium_browser import HidemiumBrowser
from ..hidemium_browser_s.hidemium_browser_runtime_setting import HidemiumBrowserRuntimeSetting
from ..helix_browser_s.helix_browser import HelixBrowser
from ..helix_browser_s.helix_manager import create_helix_backend
from ..helix_browser_s.helix_browser_runtime_setting import HelixBrowserRuntimeSetting
from ..hub_browser_s.hub_browser import HubBrowser
from ..hub_browser_s.hub_browser_runtime_setting import HubBrowserRuntimeSetting
from ..morelogin_browser_s.morelogin_browser import MoreLoginBrowser
from ..kameleo_browser_s.kameleo_browser import KameleoBrowser
from ..kameleo_browser_s.kameleo_browser_runtime_setting import KameleoBrowserRuntimeSetting
from ..morelogin_browser_s.morelogin_browser_runtime_setting import MoreLoginBrowserRuntimeSetting
from ..o_browser_s.o_browser import OBrowser
from ..o_browser_s.o_browser_runtime_setting import OBrowserRuntimeSetting
from ..ruyi_browser_s.ruyi_browser import RuyiBrowser
from ..ruyi_browser_s.ruyi_browser_runtime_setting import RuyiBrowserRuntimeSetting
from ..u_browser_s.u_browser import UBrowser
from ..u_browser_s.u_browser_runtime_setting import UBrowserRuntimeSetting
from ..vmlogin_browser_s.vmlogin_browser import VMLoginBrowser
from ..vmlogin_browser_s.vmlogin_browser_runtime_setting import VMLoginBrowserRuntimeSetting


class BrowserFactory:
    """根据配置创建指定浏览器，业务层不接触厂商构造参数。"""

    IMPLEMENTED = frozenset({
        "octo", "hub", "undetectable", "morelogin", "bitbrowser",
        "adspower", "vmlogin", "hidemium", "chrome", "helix", "ruyi", "kameleo",
    })

    @classmethod
    def build(cls, settings: BrowserSettings, key: str) -> BrowserBase:
        normalized = key.strip().lower()
        try:
            provider = settings.providers[normalized]
        except KeyError as exc:
            raise KeyError(f"浏览器配置不存在: {key}") from exc

        if normalized == "kameleo":
            return KameleoBrowser(KameleoBrowserRuntimeSetting(
                local_api_url=provider.local_api_url,
                request_timeout=float(provider.options.get("request_timeout_seconds") or 8),
                start_timeout=float(provider.options.get("start_timeout_seconds") or 120),
            ))
        if normalized == "octo":
            token = provider.credentials.api_token.get_secret_value()
            return OBrowser(OBrowserRuntimeSetting(
                local_api_url=provider.local_api_url,
                api_token=token,
                remote_api_url=(
                    provider.remote_api_url
                    or "https://app.octobrowser.net/api/v2/automation"
                ),
            ))
        if normalized == "hub":
            authorization = (
                provider.credentials.authorization.get_secret_value() or "NULL"
            )
            return HubBrowser(HubBrowserRuntimeSetting(
                local_api_url=provider.local_api_url,
                authorization=authorization,
                app_id=provider.credentials.app_id.get_secret_value(),
                app_secret=(
                    provider.credentials.app_secret.get_secret_value()
                ),
                group_code=(
                    provider.credentials.group_code.get_secret_value()
                ),
                core_directory=str(provider.options.get("core_directory") or ""),
            ))
        if normalized == "undetectable":
            return UBrowser(UBrowserRuntimeSetting(
                local_api_url=provider.local_api_url,
            ))
        if normalized == "morelogin":
            return MoreLoginBrowser(MoreLoginBrowserRuntimeSetting(
                local_api_url=provider.local_api_url,
            ))
        if normalized == "bitbrowser":
            return BitBrowser(BitBrowserRuntimeSetting(
                local_api_url=provider.local_api_url,
            ))
        if normalized == "adspower":
            return AdsPowerBrowser(AdsPowerBrowserRuntimeSetting(
                local_api_url=provider.local_api_url,
                api_key=provider.credentials.api_token.get_secret_value(),
            ))
        if normalized == "vmlogin":
            return VMLoginBrowser(VMLoginBrowserRuntimeSetting(
                local_api_url=provider.local_api_url,
                remote_api_url=(
                    provider.remote_api_url or "https://api.vmlogin.com/v1"
                ),
                api_token=provider.credentials.api_token.get_secret_value(),
            ))
        if normalized == "hidemium":
            return HidemiumBrowser(HidemiumBrowserRuntimeSetting(
                local_api_url=provider.local_api_url,
                is_local=bool(provider.options.get("is_local", True)),
            ))
        if normalized == "chrome":
            defaults = ChromeBrowserRuntimeSetting()
            return ChromeBrowser(ChromeBrowserRuntimeSetting(
                browser_path=str(provider.options.get("browser_path") or ""),
                profile_root=str(
                    provider.options.get("profile_root")
                    or defaults.profile_root
                ),
                ini_path=str(
                    provider.options.get("ini_path")
                    or defaults.ini_path
                ),
            ))
        if normalized == "helix":
            return create_helix_backend(HelixBrowserRuntimeSetting(
                local_api_url=(
                    provider.local_api_url
                    or "http://127.0.0.1:58888"
                ),
                core_family=str(
                    provider.options.get("core_family") or "chromium"
                ),
                core_version=str(
                    provider.options.get("core_version")
                    or "149.0.7827.156"
                ),
                auto_download_core=bool(
                    provider.options.get("auto_download_core", False)
                ),
                humanize=bool(provider.options.get("humanize", True)),
                request_timeout=float(
                    provider.options.get("request_timeout") or 120
                ),
            ))
        if normalized == "ruyi":
            defaults = RuyiBrowserRuntimeSetting()
            return RuyiBrowser(RuyiBrowserRuntimeSetting(
                browser_path=str(
                    provider.options.get("browser_path") or ""
                ),
                profile_root=str(
                    provider.options.get("profile_root")
                    or defaults.profile_root
                ),
                core_root=str(
                    provider.options.get("core_root")
                    or defaults.core_root
                ),
                fingerprint_directory=str(
                    provider.options.get("fingerprint_directory")
                    or defaults.fingerprint_directory
                ),
                auto_download_core=bool(
                    provider.options.get(
                        "auto_download_core",
                        defaults.auto_download_core,
                    )
                ),
                download_base_url=str(
                    provider.options.get("download_base_url") or ""
                ),
                geo_timeout=float(
                    provider.options.get("geo_timeout") or 8
                ),
                geo_retries=int(
                    provider.options.get("geo_retries")
                    if provider.options.get("geo_retries") is not None
                    else 1
                ),
            ))
        raise NotImplementedError(f"暂不支持该浏览器: {key}")

    @classmethod
    def create_catalog(cls, settings: BrowserSettings) -> BrowserCatalog:
        registrations = []
        for key, provider_settings in settings.providers.items():
            if key not in cls.IMPLEMENTED:
                if provider_settings.enabled and provider_settings.weight > 0:
                    raise NotImplementedError(f"已启用的浏览器尚未迁移: {key}")
                continue
            provider = cls.build(settings, key)
            registrations.append(BrowserRegistration(
                browser=provider,
                enabled=provider_settings.enabled,
                weight=provider_settings.weight,
            ))
        return BrowserCatalog(registrations)


__all__ = ["BrowserFactory"]
