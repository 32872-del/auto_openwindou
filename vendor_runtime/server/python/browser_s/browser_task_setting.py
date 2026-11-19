"""创建和启动指纹浏览器所需的统一参数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


@dataclass(frozen=True, slots=True)
class ProxySetting:
    """传给指纹浏览器的代理参数。

    密码不参与 ``repr``，防止调试日志把代理凭据直接打印出来。
    """

    protocol: str
    host: str
    port: int
    username: str = ""
    password: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        protocol = self.protocol.strip().lower()
        if protocol not in {"http", "https", "socks5", "ssh"}:
            raise ValueError(f"不支持的代理协议: {self.protocol}")
        if not self.host.strip():
            raise ValueError("代理 host 不能为空")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError(f"代理端口无效: {self.port}")
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "host", self.host.strip())
        object.__setattr__(self, "port", int(self.port))


@dataclass(frozen=True, slots=True)
class FingerprintSetting:
    """跨厂商通用的浏览器指纹参数。"""

    os: str
    os_version: str = ""
    architecture: str = ""
    device_type: str = ""
    device_model: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        os_name = self.os.strip().lower()
        if os_name not in {"win", "mac", "android", "linux"}:
            raise ValueError(f"不支持的指纹 OS: {self.os}")
        architecture = self.architecture.strip().lower()
        if architecture and architecture not in {"x86", "arm"}:
            raise ValueError(f"不支持的架构: {self.architecture}")
        object.__setattr__(self, "os", os_name)
        object.__setattr__(self, "architecture", architecture)
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True, slots=True)
class CreateBrowserSetting:
    """创建浏览器时由业务层传入的固定参数。"""

    name: str
    fingerprint: FingerprintSetting
    proxy: ProxySetting | None = None
    group: str = ""
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("浏览器名称不能为空")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(
            self,
            "tags",
            tuple(tag.strip() for tag in self.tags if tag.strip()),
        )
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(self.metadata)),
        )


@dataclass(frozen=True, slots=True)
class StartBrowserSetting:
    """启动一个已经创建的指纹浏览器。"""

    browser_id: str
    headless: bool = False
    timeout_seconds: int = 120
    flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.browser_id.strip():
            raise ValueError("browser_id 不能为空")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        object.__setattr__(self, "browser_id", self.browser_id.strip())
        object.__setattr__(self, "flags", tuple(self.flags))


class ProviderCredentials(BaseModel):
    """单个指纹浏览器的凭据；保存时由本类统一解包。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_token: SecretStr = SecretStr("")
    authorization: SecretStr = SecretStr("")
    app_id: SecretStr = SecretStr("")
    app_secret: SecretStr = SecretStr("")
    group_code: SecretStr = SecretStr("")
    secret_ref: str = ""

    def storage_dict(self) -> dict[str, str]:
        """生成配置文件需要的普通字典。"""

        return {
            "api_token": self.api_token.get_secret_value(),
            "authorization": self.authorization.get_secret_value(),
            "app_id": self.app_id.get_secret_value(),
            "app_secret": self.app_secret.get_secret_value(),
            "group_code": self.group_code.get_secret_value(),
            "secret_ref": self.secret_ref,
        }


class FingerprintSettings(BaseModel):
    """UI 和配置文件共同使用的指纹随机参数。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    os_mode: Literal["sequential", "weighted"] = "weighted"
    os_weights: dict[str, int] = Field(default_factory=dict)
    android_brand_weights: dict[str, int] = Field(default_factory=dict)
    chrome_versions: list[str] = Field(default_factory=list)
    cpu_cores: list[int] = Field(default_factory=list)
    memory_gb: list[int] = Field(default_factory=list)
    resolutions: list[str] = Field(default_factory=list)
    noise: bool = True

    @field_validator("os_weights", "android_brand_weights")
    @classmethod
    def weights_are_non_negative(cls, value: dict[str, int]) -> dict[str, int]:
        normalized = {str(key): int(weight) for key, weight in value.items()}
        if any(weight < 0 for weight in normalized.values()):
            raise ValueError("指纹权重不能为负数")
        return normalized


class BrowserProviderSettings(BaseModel):
    """一家指纹浏览器的公共配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    weight: int = Field(default=0, ge=0)
    local_api_url: str
    remote_api_url: str = ""
    credentials: ProviderCredentials = Field(default_factory=ProviderCredentials)
    fingerprint: FingerprintSettings = Field(default_factory=FingerprintSettings)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("local_api_url", "remote_api_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    def storage_dict(self) -> dict[str, Any]:
        """生成配置文件需要的普通字典。"""

        data = self.model_dump(mode="json", exclude={"credentials"})
        data["credentials"] = self.credentials.storage_dict()
        return data


class BrowserSettings(BaseModel):
    """浏览器配置唯一模型；UI 和业务代码只读写这一套结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    selection_mode: Literal["fixed", "weighted"] = "fixed"
    selected_provider: str = "octo"
    automation_driver: Literal["dp", "win32_dp"] = "win32_dp"
    group_name: str = "auto"
    threads: int = Field(default=1, ge=1, le=32)
    window_width: int = Field(default=1920, ge=320)
    window_height: int = Field(default=1080, ge=240)
    auto_sort: bool = True
    delete_on_close: bool = True
    retry_count: int = Field(default=1, ge=0)
    retry_run_count: int = Field(default=3, ge=0)
    retry_pause: int = Field(default=2, ge=0)
    browser_recreate_count: int = Field(default=1, ge=0, le=5)
    browser_start_retry_count: int = Field(default=2, ge=0, le=5)
    browser_create_hourly_limit: int = Field(default=60, ge=1, le=10_000)
    browser_retry_wait_max_seconds: int = Field(default=120, ge=0, le=3_600)
    # 一天内的第几分钟（0~1439），-1 表示不限时间窗。
    # 以前按小时存，09:30 会被静默截成 09:00，配的分钟根本不起作用。
    timer_start: int = Field(default=-1, ge=-1, le=1_439)
    timer_end: int = Field(default=-1, ge=-1, le=1_439)
    timer_timezone: str = "America/New_York"
    max_task_minutes: int = Field(default=30, ge=-1)
    record_packets: bool = True
    # 浏览器起来后跑 BrowserScan / CreepJS 检测页采集并存档。默认关：
    # 只有明确要看代理/指纹质量时才值得每个浏览器都付这个时间。
    detection_enabled: bool = False
    detection_relay: str = ""
    detection_alias: str = ""
    reuse_browser: bool = False
    providers: dict[str, BrowserProviderSettings] = Field(default_factory=dict)

    @field_validator("automation_driver", mode="before")
    @classmethod
    def migrate_automation_driver(cls, value: Any) -> Any:
        normalized = str(value or "").strip().casefold()
        if normalized in {"", "drission", "drissionpage", "chrome", "selenium"}:
            return "dp"
        return normalized

    @model_validator(mode="after")
    def selected_provider_exists(self) -> "BrowserSettings":
        if self.selection_mode == "fixed":
            provider = self.providers.get(self.selected_provider)
            if provider is None:
                raise ValueError(f"固定浏览器未配置: {self.selected_provider}")
            if not provider.enabled:
                raise ValueError(f"固定浏览器未启用: {self.selected_provider}")
        elif not any(
            item.enabled and item.weight > 0
            for item in self.providers.values()
        ):
            raise ValueError("加权模式至少启用一个权重大于 0 的浏览器")
        return self

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "BrowserSettings":
        """从任务下发的运行设置中读取 ``browser`` 分组。"""

        raw = document.get("browser")
        if not isinstance(raw, Mapping):
            raise ValueError("任务浏览器设置必须包含 browser 对象")
        return cls.model_validate(dict(raw))

    def storage_dict(self) -> dict[str, Any]:
        """生成配置文件需要的 browser 分组。"""

        data = self.model_dump(mode="json", exclude={"providers"})
        data["providers"] = {
            key: provider.storage_dict()
            for key, provider in self.providers.items()
        }
        return data


__all__ = [
    "BrowserProviderSettings",
    "BrowserSettings",
    "CreateBrowserSetting",
    "FingerprintSetting",
    "FingerprintSettings",
    "ProviderCredentials",
    "ProxySetting",
    "StartBrowserSetting",
]
