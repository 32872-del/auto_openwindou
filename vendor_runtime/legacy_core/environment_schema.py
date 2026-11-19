"""指纹环境事件 schema v2、ID 与隐私边界。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from vendor_runtime.legacy_core.environment_diagnostic_schema import validate_canonical_fields
from vendor_runtime.legacy_core.environment_identity import LAUNCH_ID_RE


SCHEMA_VERSION = 2
APP_NAME = "undetectable-manager"
ENVIRONMENT_ID_RE = re.compile(r"^env_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
CAPTURE_REASONS = {"created", "rerolled", "restarted", "manual"}
CHECK_NAMES = ("browserscan", "browserleaks", "scamalytics", "ip2proxy", "leakdig", "creepjs", "tls")
CHECK_STATUSES = {"complete", "partial", "manual", "failed"}
# 检测页存档只允许 "<站点>.html" 这种纯文件名
PAGE_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}[.]html$")
# TLS 检测以独立 Excel 存档，文件名包含固定采集 ID。
TLS_PAGE_NAME_RE = re.compile(r"^tls_[0-9a-f]{32}[.]xlsx$")
ARCHIVE_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}[.](?:html|json|xlsx)$")
MAX_EVENT_BYTES = 256 * 1024
_ALLOWED_KEYS = {
    "event": {"schema_version", "event_id", "environment_id", "launch_id", "captured_at", "capture_reason", "source", "status", "profile", "page", "fingerprint", "fingerprint_hash", "ip", "latency", "checks", "privacy_policy_version", "error"},
    "source": {"client_id", "client_number", "client_name", "app"},
    "profile": {"id", "name", "browser", "os_label", "proxy_type"},
    "page": {"requested_url", "url", "title", "ready_state"},
    "ip": {"address", "city", "region", "zip", "query_failed", "dual_breakdown"},
    # 代理延迟：只存数字，不存目标 URL
    "latency": {"us_avg_ms", "ca_avg_ms", "us_reachable", "us_total", "ca_reachable", "ca_total", "rounds"},
    "check": {"status", "fields", "url", "error", "page"},
}
_SENSITIVE_KEY = re.compile(
    r"(^|_)(cookies?|password|passwd|credential|local_storage|session_storage|"
    r"form|form_data|html|page_body|proxy_url|proxy_user|proxy_username|proxy_pass|proxy_password|"
    r"authorization|auth_token|access_token|refresh_token|api_key|secret)($|_)",
    re.IGNORECASE,
)
_SENSITIVE_COMPACT_KEYS = {
    "cookie", "cookies", "password", "passwd", "credential", "credentials",
    "localstorage", "sessionstorage", "formdata", "html", "pagebody",
    "proxyurl", "proxyuser", "proxyusername", "proxypass", "proxypassword",
    "authorization", "authtoken", "accesstoken", "refreshtoken", "apikey", "secret",
}


class EnvironmentSchemaError(ValueError):
    """事件违反公开契约或隐私策略。"""


def new_environment_id() -> str:
    return f"env_{uuid.uuid4()}"


def new_event_id() -> str:
    return f"evt_{uuid.uuid4()}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def fingerprint_hash(fingerprint: dict) -> str:
    raw = json.dumps(fingerprint or {}, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest() if fingerprint else ""


def _assert_no_sensitive_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            raw_key = str(key)
            normalized_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw_key)
            normalized_key = re.sub(r"[^A-Za-z0-9]+", "_", normalized_key).strip("_").lower()
            compact_key = re.sub(r"[^a-z0-9]", "", normalized_key)
            sensitive = _SENSITIVE_KEY.search(normalized_key) or compact_key in _SENSITIVE_COMPACT_KEYS
            if normalized_key != "cookie_enabled" and sensitive:
                raise EnvironmentSchemaError(f"禁止保存敏感字段: {path}.{key}")
            _assert_no_sensitive_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_sensitive_fields(child, f"{path}[{index}]")


def _assert_only_keys(value: dict, allowed: set[str], path: str) -> None:
    extras = set(value) - allowed
    if extras:
        raise EnvironmentSchemaError(f"{path} 含未允许字段: {sorted(extras)[0]}")


def validate_event(event: dict) -> dict:
    if not isinstance(event, dict):
        raise EnvironmentSchemaError("事件必须是对象")
    _assert_only_keys(event, _ALLOWED_KEYS["event"], "event")
    if event.get("schema_version") != SCHEMA_VERSION:
        raise EnvironmentSchemaError("schema_version 必须为 2")
    if not EVENT_ID_RE.fullmatch(str(event.get("event_id", ""))):
        raise EnvironmentSchemaError("event_id 格式无效")
    if not ENVIRONMENT_ID_RE.fullmatch(str(event.get("environment_id", ""))):
        raise EnvironmentSchemaError("environment_id 格式无效")
    if not LAUNCH_ID_RE.fullmatch(str(event.get("launch_id", ""))):
        raise EnvironmentSchemaError("launch_id 格式无效（yyyy-mm-dd-XXXXXXX）")
    captured_at = str(event.get("captured_at", ""))
    try:
        parsed_at = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except Exception as exc:
        raise EnvironmentSchemaError("captured_at 必须为 UTC ISO-8601（Z）") from exc
    if not captured_at.endswith("Z") or parsed_at.utcoffset() != timezone.utc.utcoffset(parsed_at):
        raise EnvironmentSchemaError("captured_at 必须为 UTC ISO-8601（Z）")
    if event.get("capture_reason") not in CAPTURE_REASONS:
        raise EnvironmentSchemaError("capture_reason 无效")
    if event.get("status") not in {"complete", "partial"}:
        raise EnvironmentSchemaError("status 无效")
    source = event.get("source")
    if not isinstance(source, dict) or not str(source.get("client_id", "")).strip():
        raise EnvironmentSchemaError("source.client_id 不能为空")
    _assert_only_keys(source, _ALLOWED_KEYS["source"], "source")
    for key in ("profile", "page", "fingerprint", "ip", "checks"):
        if not isinstance(event.get(key), dict):
            raise EnvironmentSchemaError(f"{key} 必须是对象")
    _assert_only_keys(event["profile"], _ALLOWED_KEYS["profile"], "profile")
    _assert_only_keys(event["page"], _ALLOWED_KEYS["page"], "page")
    _assert_only_keys(event["ip"], _ALLOWED_KEYS["ip"], "ip")
    latency = event.get("latency")
    if latency is not None:
        if not isinstance(latency, dict):
            raise EnvironmentSchemaError("latency 必须是对象")
        _assert_only_keys(latency, _ALLOWED_KEYS["latency"], "latency")
        for key in ("us_avg_ms", "ca_avg_ms"):
            value = latency.get(key)
            if value is not None and not (isinstance(value, (int, float))
                                          and not isinstance(value, bool) and value >= 0):
                raise EnvironmentSchemaError(f"latency.{key} 必须是非负数或 null")
        for key in ("us_reachable", "us_total", "ca_reachable", "ca_total", "rounds"):
            value = latency.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise EnvironmentSchemaError(f"latency.{key} 必须是非负整数")
    _assert_only_keys(event["checks"], set(CHECK_NAMES), "checks")
    privacy_policy_version = event.get("privacy_policy_version")
    if privacy_policy_version not in {1, 2}:
        raise EnvironmentSchemaError("privacy_policy_version 必须为 1 或 2")
    for name in CHECK_NAMES:
        if name in ("creepjs", "tls") and name not in event["checks"]:
            continue  # 历史五站记录仍可读，不补造 CreepJS 存档。
        check = event["checks"].get(name)
        if not isinstance(check, dict) or check.get("status") not in CHECK_STATUSES:
            raise EnvironmentSchemaError(f"checks.{name} 无效")
        _assert_only_keys(check, _ALLOWED_KEYS["check"], f"checks.{name}")
        page_name = check.get("page", "")
        page_pattern = TLS_PAGE_NAME_RE if name == "tls" else PAGE_NAME_RE
        if page_name and not page_pattern.fullmatch(str(page_name)):
            raise EnvironmentSchemaError(f"checks.{name}.page 文件名无效")
        if not isinstance(check.get("fields"), dict):
            raise EnvironmentSchemaError(f"checks.{name}.fields 必须是对象")
        if privacy_policy_version == 2:
            problem = validate_canonical_fields(name, check["fields"])
            if problem:
                raise EnvironmentSchemaError(f"checks.{name}.{problem}")
    _assert_no_sensitive_fields(event)
    if len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_EVENT_BYTES:
        raise EnvironmentSchemaError("单条事件超过 256KB")
    return event


def normalize_latency(latency: dict | None) -> dict:
    """把延迟结果压成事件里的定长数字结构；缺失时各项为 None/0。"""
    source: dict = latency if isinstance(latency, dict) else {}

    def number(value: Any) -> float | None:
        try:
            result = round(float(value), 1)
        except (TypeError, ValueError):
            return None
        return result if 0 <= result <= 600_000 else None

    def count(value: Any) -> int:
        try:
            return max(0, min(int(value), 1000))
        except (TypeError, ValueError):
            return 0

    return {
        "us_avg_ms": number(source.get("us_avg_ms")),
        "ca_avg_ms": number(source.get("ca_avg_ms")),
        "us_reachable": count(source.get("us_reachable")),
        "us_total": count(source.get("us_total")),
        "ca_reachable": count(source.get("ca_reachable")),
        "ca_total": count(source.get("ca_total")),
        "rounds": count(source.get("rounds")),
    }


def build_event(*, environment_id: str, launch_id: str, capture_reason: str, source: dict,
                profile: dict, page_snapshot: dict | None, ip_check: dict | None,
                checks: dict | None, error: str = "", latency: dict | None = None) -> dict:
    snapshot = page_snapshot or {}
    raw_page = snapshot.get("page")
    page: dict = raw_page if isinstance(raw_page, dict) else {}
    raw_fingerprint = snapshot.get("fingerprint")
    fingerprint: dict = raw_fingerprint if isinstance(raw_fingerprint, dict) else {}
    ip_check = ip_check or {}
    normalized_checks = {}
    supplied: dict = checks if isinstance(checks, dict) else {}
    for name in CHECK_NAMES:
        if name in ("creepjs", "tls") and name not in supplied:
            continue
        raw_item = supplied.get(name)
        item: dict = raw_item if isinstance(raw_item, dict) else {}
        page_pattern = TLS_PAGE_NAME_RE if name == "tls" else PAGE_NAME_RE
        normalized_checks[name] = {
            "status": item.get("status") if item.get("status") in CHECK_STATUSES else "manual",
            "fields": item.get("fields") if isinstance(item.get("fields"), dict) else {},
            "url": str(item.get("url", ""))[:2000],
            "error": str(item.get("error", ""))[:1000],
            # 只存文件名；实际目录由 environment_id/event_id 推出，避免路径穿越
            "page": (str(item.get("page", "") or "")
                     if page_pattern.fullmatch(str(item.get("page", "") or "")) else ""),
        }
    event_error = str(error or "")[:2000]
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": new_event_id(),
        "environment_id": environment_id,
        "launch_id": str(launch_id or ""),
        "captured_at": now_iso(),
        "capture_reason": capture_reason,
        "source": {
            "client_id": str(source.get("client_id", "")),
            "client_number": str(source.get("client_number", ""))[:16].upper(),
            "client_name": str(source.get("client_name", ""))[:200],
            "app": APP_NAME,
        },
        "status": "complete" if fingerprint and ip_check.get("actual_ip") and not event_error else "partial",
        "profile": {
            "id": str(profile.get("id", "")),
            "name": str(profile.get("name", ""))[:500],
            "browser": str(profile.get("browser", ""))[:50],
            "os_label": str(profile.get("os_label", ""))[:200],
            "proxy_type": str(profile.get("proxy_type", ""))[:100],
        },
        "page": {
            "requested_url": str(page.get("requested_url", ""))[:2000],
            "url": str(page.get("url", ""))[:2000],
            "title": str(page.get("title", ""))[:500],
            "ready_state": str(page.get("ready_state", ""))[:100],
        },
        "fingerprint": fingerprint,
        "fingerprint_hash": fingerprint_hash(fingerprint),
        "ip": {
            "address": str(ip_check.get("actual_ip", ""))[:100],
            "city": str(ip_check.get("actual_city", ""))[:200],
            "region": str(ip_check.get("actual_region", ""))[:200],
            "zip": str(ip_check.get("actual_zip", ""))[:100],
            "query_failed": bool(ip_check.get("query_failed")),
            "dual_breakdown": str(ip_check.get("dual_breakdown", ""))[:1000],
        },
        "latency": normalize_latency(latency),
        "checks": normalized_checks,
        "privacy_policy_version": 2,
        "error": event_error,
    }
    return validate_event(event)
