"""执行端浏览器模块的稳定数据模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BrowserRuntimeMode(StrEnum):
    """浏览器程序在执行端的运行形态。"""

    PERSISTENT_SERVICE = "persistent_service"
    ON_DEMAND_KERNEL = "on_demand_kernel"


class BrowserHealthState(StrEnum):
    """跨厂商统一、可直接用于自动决策的健康状态。"""

    DISABLED = "DISABLED"
    NOT_INSTALLED = "NOT_INSTALLED"
    NOT_RUNNING = "NOT_RUNNING"
    STARTING = "STARTING"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    READY = "READY"
    QUOTA_LOW = "QUOTA_LOW"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    CAPACITY_REQUIRED = "CAPACITY_REQUIRED"
    UPDATE_REQUIRED = "UPDATE_REQUIRED"
    KERNEL_MISSING = "KERNEL_MISSING"
    NETWORK_ERROR = "NETWORK_ERROR"
    PROXY_ERROR = "PROXY_ERROR"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class BrowserValidationLevel(StrEnum):
    """浏览器实现实际通过的验证层级。"""

    NOT_CONFIGURED = "B0_PENDING"
    CONTRACT = "B1_CONTRACT"
    DISCOVERED = "B2_DISCOVERED"
    LIFECYCLE = "B3_LIFECYCLE"
    OFFICIAL_API = "B4_OFFICIAL_API"
    PROXY = "B5_PROXY"
    DRIVER = "B6_DRIVER"
    REAL_TASK = "B7_REAL_TASK"
    PACKAGED = "B8_PACKAGED"
    FAILED = "FAILED"


class BrowserModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_assignment=True,
    )


class BrowserCapabilities(BrowserModel):
    create: bool = False
    start: bool = False
    stop: bool = False
    update: bool = False
    delete: bool = False
    groups: bool = False
    templates: bool = False
    quota: bool = False
    auto_start_program: bool = False
    authenticated_proxy: bool = True
    remote_screen: bool = True
    independent_protocol: bool = Field(default=False, alias="independentProtocol")


class BrowserLimits(BrowserModel):
    available: bool = False
    maximum_environments: int | None = Field(default=None, alias="maximumEnvironments")
    current_environments: int | None = Field(default=None, alias="currentEnvironments")
    maximum_open: int | None = Field(default=None, alias="maximumOpen")
    current_open: int | None = Field(default=None, alias="currentOpen")
    maximum_creates_per_hour: int | None = Field(
        default=None,
        alias="maximumCreatesPerHour",
    )
    remaining_creates_this_hour: int | None = Field(
        default=None,
        alias="remainingCreatesThisHour",
    )
    maximum_creates_per_day: int | None = Field(
        default=None,
        alias="maximumCreatesPerDay",
    )
    created_last_hour: int | None = Field(default=None, alias="createdLastHour")
    created_last_day: int | None = Field(default=None, alias="createdLastDay")
    maximum_requests_per_minute: int | None = Field(
        default=None,
        alias="maximumRequestsPerMinute",
    )
    maximum_requests_per_hour: int | None = Field(
        default=None,
        alias="maximumRequestsPerHour",
    )
    remaining_requests_this_minute: int | None = Field(
        default=None,
        alias="remainingRequestsThisMinute",
    )
    remaining_requests_this_hour: int | None = Field(
        default=None,
        alias="remainingRequestsThisHour",
    )
    create_request_cost: int | None = Field(default=None, alias="createRequestCost")
    start_request_cost: int | None = Field(default=None, alias="startRequestCost")
    stop_request_cost: int | None = Field(default=None, alias="stopRequestCost")
    one_time_request_cost: int | None = Field(
        default=None,
        alias="oneTimeRequestCost",
    )
    reset_at: datetime | None = Field(default=None, alias="resetAt")
    limit_source: str = Field(default="unavailable", alias="limitSource")
    usage_source: str = Field(default="unavailable", alias="usageSource")
    limit_note: str = Field(default="", alias="limitNote", max_length=2_000)
    usage_note: str = Field(default="", alias="usageNote", max_length=2_000)
    local_maximum_creates_per_hour: int | None = Field(
        default=None,
        alias="localMaximumCreatesPerHour",
    )
    message: str = "厂商未提供使用上限"


class BrowserProbeResult(BrowserModel):
    provider: str = Field(min_length=1, max_length=64)
    display_name: str = Field(alias="displayName", min_length=1, max_length=128)
    runtime_mode: BrowserRuntimeMode = Field(alias="runtimeMode")
    state: BrowserHealthState
    message: str = Field(min_length=1, max_length=2_000)
    suggestion: str = Field(default="", max_length=2_000)
    checked_at: datetime = Field(default_factory=utc_now, alias="checkedAt")
    version: str = Field(default="", max_length=128)
    source: str = Field(default="", max_length=256)
    required_by_task: bool = Field(default=False, alias="requiredByTask")
    validation_level: BrowserValidationLevel = Field(
        default=BrowserValidationLevel.CONTRACT,
        alias="validationLevel",
    )
    safe_details: dict[str, Any] = Field(
        default_factory=dict,
        alias="safeDetails",
    )

    @property
    def usable(self) -> bool:
        return self.state in {
            BrowserHealthState.READY,
            BrowserHealthState.QUOTA_LOW,
        } and self.validation_level in {
            BrowserValidationLevel.REAL_TASK,
            BrowserValidationLevel.PACKAGED,
        }


class BrowserReadyRequest(BrowserModel):
    allow_safe_start: bool = Field(default=False, alias="allowSafeStart")
    task_id: UUID | None = Field(default=None, alias="taskId")


class BrowserReadyResult(BrowserModel):
    ready: bool
    probe: BrowserProbeResult
    action_taken: str = Field(default="", alias="actionTaken", max_length=256)


class BrowserProgramCandidate(BrowserModel):
    """执行端本机探针发现的只读程序候选。"""

    candidate_id: str = Field(alias="candidateId", min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=4_096)
    product_name: str = Field(default="", alias="productName", max_length=256)
    version: str = Field(default="", max_length=128)
    publisher: str = Field(default="", max_length=1_024)
    signature_status: str = Field(
        default="Unknown",
        alias="signatureStatus",
        max_length=64,
    )
    sha256: str = Field(min_length=64, max_length=64)
    source: str = Field(min_length=1, max_length=128)
    trusted: bool = False
    reason: str = Field(default="", max_length=1_024)


class BrowserProgramStatus(BrowserModel):
    """执行端本机程序探测与确认状态；不得通过远程协议返回。"""

    provider: str = Field(min_length=1, max_length=64)
    supported: bool
    candidates: tuple[BrowserProgramCandidate, ...] = ()
    confirmed_candidate_id: str = Field(
        default="",
        alias="confirmedCandidateId",
        max_length=128,
    )
    confirmed_valid: bool = Field(default=False, alias="confirmedValid")
    confirmed_candidate: BrowserProgramCandidate | None = Field(
        default=None,
        alias="confirmedCandidate",
    )
    message: str = Field(min_length=1, max_length=1_024)


class BrowserProgramStartResult(BrowserModel):
    """一次本机程序启动命令的安全回执。"""

    provider: str = Field(min_length=1, max_length=64)
    started: bool
    candidate_id: str = Field(default="", alias="candidateId", max_length=128)
    message: str = Field(min_length=1, max_length=1_024)


class BrowserCreateRequest(BrowserModel):
    name: str = Field(min_length=1, max_length=128)
    group: str = Field(default="", max_length=128)
    template: str = Field(default="", max_length=128)
    safe_parameters: dict[str, Any] = Field(
        default_factory=dict,
        alias="safeParameters",
    )


class BrowserUpdateRequest(BrowserModel):
    environment_id: str = Field(alias="environmentId", min_length=1, max_length=256)
    safe_parameters: dict[str, Any] = Field(
        default_factory=dict,
        alias="safeParameters",
    )


class BrowserEnvironment(BrowserModel):
    environment_id: str = Field(alias="environmentId", min_length=1, max_length=256)
    provider: str = Field(min_length=1, max_length=64)
    provider_environment_id: str = Field(
        alias="providerEnvironmentId",
        min_length=1,
        max_length=256,
    )
    owner_client_id: str = Field(alias="ownerClientId", min_length=1, max_length=128)
    owner_task_id: UUID | None = Field(default=None, alias="ownerTaskId")
    created_by: str = Field(default="client", alias="createdBy", min_length=1, max_length=64)
    state: str = Field(default="created", max_length=64)
    safe_parameters: dict[str, Any] = Field(
        default_factory=dict,
        alias="safeParameters",
    )
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")
    updated_at: datetime = Field(default_factory=utc_now, alias="updatedAt")
    last_used_at: datetime | None = Field(default=None, alias="lastUsedAt")


class BrowserSessionDescriptor(BrowserModel):
    browser_session_id: UUID = Field(
        default_factory=uuid4,
        alias="browserSessionId",
    )
    provider: str = Field(min_length=1, max_length=64)
    environment_id: str = Field(alias="environmentId", min_length=1, max_length=256)
    driver_type: str = Field(alias="driverType", min_length=1, max_length=64)
    debug_port: int | None = Field(default=None, alias="debugPort", ge=1, le=65_535)
    connection_url: str = Field(default="", alias="connectionUrl", max_length=2_048)
    process_id: int | None = Field(default=None, alias="processId", ge=1)
    started_at: datetime = Field(default_factory=utc_now, alias="startedAt")


class BrowserProviderSummary(BrowserModel):
    key: str = Field(min_length=1, max_length=64)
    display_name: str = Field(alias="displayName", min_length=1, max_length=128)
    runtime_mode: BrowserRuntimeMode = Field(alias="runtimeMode")
    enabled: bool
    monitored: bool
    required_by_task: bool = Field(default=False, alias="requiredByTask")
    configured: bool
    revision: int = Field(ge=0)
    capabilities: BrowserCapabilities
    health: BrowserProbeResult | None = None
    secret_configured: dict[str, bool] = Field(
        default_factory=dict,
        alias="secretConfigured",
    )


class BrowserSettingSnapshot(BrowserModel):
    provider: str
    revision: int = Field(ge=0)
    settings: dict[str, Any]
    secret_configured: dict[str, bool] = Field(
        default_factory=dict,
        alias="secretConfigured",
    )


__all__ = [
    "BrowserCapabilities",
    "BrowserCreateRequest",
    "BrowserEnvironment",
    "BrowserHealthState",
    "BrowserLimits",
    "BrowserProviderSummary",
    "BrowserProbeResult",
    "BrowserProgramCandidate",
    "BrowserProgramStartResult",
    "BrowserProgramStatus",
    "BrowserReadyRequest",
    "BrowserReadyResult",
    "BrowserRuntimeMode",
    "BrowserSessionDescriptor",
    "BrowserSettingSnapshot",
    "BrowserUpdateRequest",
    "BrowserValidationLevel",
]
