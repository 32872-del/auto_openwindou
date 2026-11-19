"""浏览器本机程序的可信发现、确认与有限启动。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from server.python.utils_s.process_utils import no_window_kwargs
from threading import RLock
import time
from typing import Callable, Iterable

from pydantic import BaseModel

from ..browser_model import (
    BrowserProgramCandidate,
    BrowserProgramStartResult,
    BrowserProgramStatus,
)

try:  # pragma: no cover - 非 Windows 环境只会返回没有候选
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]


class BrowserProgramError(RuntimeError):
    """可信程序检查或启动被安全边界拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True, slots=True)
class BrowserProgramSpec:
    provider: str
    display_name: str
    executable_names: tuple[str, ...]
    product_names: tuple[str, ...]
    registry_display_names: tuple[str, ...]
    publisher_fragments: tuple[str, ...]
    default_relative_paths: tuple[str, ...]
    allow_auto_start: bool = True
    start_cooldown_seconds: float = 60.0
    allow_unsigned_registry_install: bool = False


@dataclass(frozen=True, slots=True)
class _ProgramMetadata:
    product_name: str
    version: str
    publisher: str
    signature_status: str


_PROGRAM_SPECS = {
    "kameleo": BrowserProgramSpec(
        "kameleo", "Kameleo", ("Kameleo.exe",), ("Kameleo",),
        ("Kameleo",), ("Outis Nemo Ltd.",),
        ("Programs/Kameleo/Kameleo.exe", "Kameleo/Kameleo.exe"),
    ),
    "octo": BrowserProgramSpec(
        provider="octo",
        display_name="O 浏览器",
        executable_names=("Octo Browser.exe",),
        product_names=("Octo Browser",),
        registry_display_names=("Octo Browser",),
        publisher_fragments=("BLACKBIRD SECURE BROWSING LTD",),
        default_relative_paths=("Octo Browser/Octo Browser.exe",),
    ),
    "hubstudio": BrowserProgramSpec(
        provider="hubstudio",
        display_name="Hub 浏览器",
        executable_names=("Hubstudio.exe",),
        product_names=("Hubstudio",),
        registry_display_names=("Hubstudio",),
        publisher_fragments=("福州科鑫创科技有限公司", "Fuzhou Kexinchuang Technology Co., Ltd."),
        default_relative_paths=("Hubstudio/Hubstudio.exe",),
    ),
    "bitbrowser": BrowserProgramSpec(
        provider="bitbrowser",
        display_name="BitBrowser",
        executable_names=("比特浏览器.exe", "BitBrowser.exe"),
        product_names=("比特浏览器", "BitBrowser"),
        registry_display_names=("比特浏览器", "BitBrowser"),
        # 官方 about/download 页面公布的开发者，与当前 Windows 有效签名一致。
        publisher_fragments=("BitBrowser", "北京元宇蓝图科技有限公司"),
        default_relative_paths=(
            "bitbrowser/比特浏览器.exe",
            "BitBrowser/BitBrowser.exe",
        ),
        # 比特 7.x 官方安装包的管理器主程序当前没有 Authenticode 签名。
        # 仅允许 Windows 卸载注册表发现、位于 Program Files、且文件名和
        # ProductName 都精确匹配的安装；随后仍由 SHA-256 锁定并要求本机确认。
        allow_unsigned_registry_install=True,
    ),
    "undetectable": BrowserProgramSpec(
        provider="undetectable",
        display_name="U 浏览器",
        executable_names=("Undetectable.exe",),
        product_names=("Undetectable",),
        registry_display_names=("Undetectable",),
        publisher_fragments=("MOBICO SERVICES LTD",),
        default_relative_paths=("Undetectable/Undetectable.exe",),
    ),
    "morelogin": BrowserProgramSpec(
        "morelogin", "MoreLogin", ("MoreLogin.exe",), ("MoreLogin",),
        ("MoreLogin",), ("EASYANT PTE. LTD.",),
        ("MoreLogin/MoreLogin.exe", "Programs/MoreLogin/MoreLogin.exe"),
    ),
    "adspower": BrowserProgramSpec(
        "adspower", "AdsPower", ("AdsPower Global.exe", "AdsPower.exe"),
        ("AdsPower Global", "AdsPower"), ("AdsPower Global", "AdsPower"),
        ("SUNFLOWER TECH PTE. LTD.",),
        ("AdsPower Global/AdsPower Global.exe", "AdsPower/AdsPower.exe"),
    ),
    "hidemium": BrowserProgramSpec(
        "hidemium", "Hidemium", ("hidemium_4.exe", "Hidemium.exe", "hidemium.exe"),
        ("Hidemium4", "Hidemium"), ("Hidemium4", "Hidemium"), ("Hidemium",),
        ("hidemium_4/hidemium_4.exe", "Hidemium/Hidemium.exe"),
        allow_unsigned_registry_install=True,
    ),
    "vmlogin": BrowserProgramSpec(
        "vmlogin", "VMLogin", ("VMLogin.exe",), ("VMLogin", "VMLogin Browser"),
        ("VMLogin", "VMLogin Browser"), ("VMLogin",),
        ("VMLogin/VMLogin.exe", "Programs/VMLogin/VMLogin.exe"),
        allow_unsigned_registry_install=True,
    ),
    "chrome": BrowserProgramSpec(
        "chrome", "Chrome", ("chrome.exe",), ("Google Chrome",),
        ("Google Chrome",), ("Google LLC", "Google Inc"),
        ("Google/Chrome/Application/chrome.exe",), allow_auto_start=False,
    ),
    "helix": BrowserProgramSpec(
        "helix", "Helix", ("Helix Browser.exe", "Helix.exe"),
        ("Helix Browser", "Helix"), ("Helix Browser", "Helix"), ("Helix",),
        ("Programs/Helix Browser/Helix Browser.exe", "Helix Browser/Helix Browser.exe"),
        allow_unsigned_registry_install=True,
    ),
    # Ruyi 使用自带内核探针，不扫描普通 Firefox 的注册表或运行进程。
    "ruyi": BrowserProgramSpec(
        "ruyi", "Ruyi", ("firefox.exe", "ruyi.exe"),
        ("Firefox", "Firefox Nightly", "Ruyi", "Ruyi Browser"), (),
        ("Mozilla Corporation",), (), allow_auto_start=False,
    ),
}


_MANAGED_PROGRAM_FIELDS = {
    "program_path": "programPath",
    "program_candidate_id": "programCandidateId",
    "program_sha256": "programSha256",
    "program_version": "programVersion",
    "program_publisher": "programPublisher",
    "program_source": "programSource",
    "program_signature_status": "programSignatureStatus",
    "program_confirmed_at": "programConfirmedAt",
}


class BrowserProgramService:
    """只在执行端本机使用，不进入远程协议。"""

    def __init__(
        self,
        *,
        specs: dict[str, BrowserProgramSpec] | None = None,
        metadata_reader: Callable[[Path], _ProgramMetadata] | None = None,
        launcher: Callable[[Path], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.specs = dict(specs or _PROGRAM_SPECS)
        self._metadata_reader = metadata_reader or _read_windows_metadata
        self._launcher = launcher or _launch_visible_program
        self._monotonic = monotonic
        self._lock = RLock()
        self._candidates: dict[str, tuple[BrowserProgramCandidate, ...]] = {}
        self._last_start_attempt: dict[str, float] = {}
        self._last_probe: dict[str, float] = {}
        self._last_probe_setting: dict[str, tuple[str, ...]] = {}
        self._probe_locks = {key: RLock() for key in self.specs}
        self._inspection_cache: dict[str, tuple[tuple[int, ...], float, str, _ProgramMetadata]] = {}
        self._runtime_paths: set[str] = set()
        self._probe_notes: dict[str, str] = {}

    @property
    def managed_fields(self) -> dict[str, str]:
        return dict(_MANAGED_PROGRAM_FIELDS)

    def status(
        self,
        provider: str,
        setting: BaseModel,
        *,
        discover_if_empty: bool = True,
    ) -> BrowserProgramStatus:
        key = str(provider or "").strip().lower()
        spec = self.specs.get(key)
        if spec is None:
            return BrowserProgramStatus(
                provider=key or "unknown",
                supported=False,
                message="当前浏览器尚未接入可信程序探针",
            )
        with self._lock:
            candidates = self._candidates.get(key, ())
        if discover_if_empty and not candidates:
            candidates = self.reprobe(key, setting).candidates

        confirmed = self._confirmed_candidate(spec, setting)
        if confirmed is not None and all(
            item.candidate_id != confirmed.candidate_id for item in candidates
        ):
            candidates = (confirmed, *candidates)
        confirmed_id = str(getattr(setting, "program_candidate_id", "") or "")
        confirmed_valid = confirmed is not None
        if confirmed_valid:
            message = "已确认可信程序，可以由执行端本机打开"
        elif confirmed_id:
            message = "已保存的程序路径或文件校验已失效，正在按本机安装情况重新探测"
        elif candidates:
            trusted_count = sum(item.trusted for item in candidates)
            message = (
                f"发现 {trusted_count} 个可信程序候选，请由执行端维护人员确认"
                if trusted_count
                else "发现程序文件，但签名或产品信息不符合可信规则"
            )
        else:
            message = "没有发现已安装程序，请先在执行端安装或打开软件"
        if not confirmed_valid and key in self._probe_notes:
            message = self._probe_notes[key]
        return BrowserProgramStatus(
            provider=key,
            supported=True,
            candidates=candidates,
            confirmedCandidateId=confirmed_id,
            confirmedValid=confirmed_valid,
            confirmedCandidate=confirmed,
            message=message,
        )

    def reprobe(self, provider: str, setting: BaseModel, *, force: bool = True) -> BrowserProgramStatus:
        key = str(provider or "").strip().lower()
        spec = self._require_spec(key)
        with self._probe_locks[key]:
            binding = tuple(str(getattr(setting, name, "") or "") for name in ("program_path", "browser_path", "core_root"))
            if (not force and self._last_probe_setting.get(key) == binding
                    and self._monotonic() - self._last_probe.get(key, float("-inf")) < 30
                    and self._candidates_unchanged(key)):
                return self.status(key, setting, discover_if_empty=False)
            result = self._reprobe(spec, setting, force=force)
            self._last_probe[key] = self._monotonic()
            self._last_probe_setting[key] = binding
            return result

    def _candidates_unchanged(self, key: str) -> bool:
        for candidate in self._candidates.get(key, ()):
            try:
                stat = Path(candidate.path).stat()
            except OSError:
                return False
            cached = self._inspection_cache.get(str(Path(candidate.path)).casefold())
            stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
            if not cached or cached[0] != stamp or cached[2] != candidate.sha256:
                return False
        return True

    def _reprobe(self, spec: BrowserProgramSpec, setting: BaseModel, *, force: bool = False) -> BrowserProgramStatus:
        key = spec.provider
        candidates: list[BrowserProgramCandidate] = []
        seen: set[str] = set()
        self._probe_notes.pop(key, None)
        for path, source in self._candidate_sources(spec, setting):
            try:
                canonical = str(path.expanduser().resolve(strict=True)).casefold()
            except OSError:
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            try:
                candidates.append(self._inspect(spec, path, source, force=force))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self._probe_notes[key] = f"检查程序 {path} 失败：{type(exc).__name__}: {exc}"[:1000]
                continue
        ordered = tuple(sorted(
            candidates,
            key=lambda item: (
                not item.trusted,
                _source_rank(item.source),
                item.path.casefold(),
            ),
        ))
        with self._lock:
            self._candidates[key] = ordered
        trusted = [item for item in ordered if item.trusted]
        if len(trusted) > 1:
            self._probe_notes[key] = "发现多个通过校验的安装位置，无法自动确定；请选择本次使用的程序并确认"
        elif ordered and not trusted:
            self._probe_notes[key] = ("已找到程序，但校验未通过：" + "；".join(
                f"{item.path}：{item.reason}" for item in ordered
            ))[:1000]
        elif not ordered and key not in self._probe_notes:
            self._probe_notes[key] = f"未找到 {spec.display_name} 的可用程序；已检查保存位置、本机安装记录及运行进程。安装或打开该浏览器后可重新探测"
        return self.status(key, setting, discover_if_empty=False)

    def confirm(
        self,
        provider: str,
        setting: BaseModel,
        candidate_id: str,
    ) -> BrowserProgramCandidate:
        key = str(provider or "").strip().lower()
        spec = self._require_spec(key)
        requested = str(candidate_id or "").strip()
        status = self.status(key, setting)
        candidate = next(
            (item for item in status.candidates if item.candidate_id == requested),
            None,
        )
        if candidate is None:
            raise BrowserProgramError(
                "BROWSER_PROGRAM_CANDIDATE_NOT_FOUND",
                "程序候选已经失效，请重新探测",
            )
        current = self._inspect(spec, Path(candidate.path), candidate.source, force=True)
        if current.candidate_id != candidate.candidate_id or not current.trusted:
            raise BrowserProgramError(
                "BROWSER_PROGRAM_CANDIDATE_CHANGED",
                "程序文件已经变化或不再可信，请重新探测",
            )
        return current

    def automatic_candidate(
        self, status: BrowserProgramStatus, setting: BaseModel,
    ) -> BrowserProgramCandidate | None:
        """保留明确选过的位置；迁机只选择唯一通过校验的安装，不猜多个候选。"""
        trusted = [item for item in status.candidates if item.trusted]
        for field in ("program_path", "browser_path"):
            saved = str(getattr(setting, field, "") or "").strip()
            if saved:
                selected = [item for item in trusted if Path(item.path) == Path(saved)]
                if len(selected) == 1:
                    return selected[0]
        return trusted[0] if len(trusted) == 1 else None

    def start(
        self,
        provider: str,
        setting: BaseModel,
        *,
        enforce_cooldown: bool = True,
        automatic: bool = False,
    ) -> BrowserProgramStartResult:
        key = str(provider or "").strip().lower()
        spec = self._require_spec(key)
        if automatic and not spec.allow_auto_start:
            raise BrowserProgramError(
                "BROWSER_PROGRAM_AUTO_START_UNSUPPORTED",
                "当前浏览器不允许任务自动打开，请由执行端维护人员手动处理",
            )
        candidate = self._confirmed_candidate(spec, setting)
        if candidate is None:
            raise BrowserProgramError(
                "BROWSER_PROGRAM_NOT_CONFIRMED",
                "请先在执行端重新探测并确认浏览器软件",
            )
        now = self._monotonic()
        with self._lock:
            previous = self._last_start_attempt.get(key)
            if (
                enforce_cooldown
                and previous is not None
                and now - previous < spec.start_cooldown_seconds
            ):
                remaining = max(1, int(spec.start_cooldown_seconds - (now - previous)))
                raise BrowserProgramError(
                    "BROWSER_PROGRAM_START_COOLDOWN",
                    f"刚刚已经尝试启动，请等待 {remaining} 秒后再试",
                )
            self._last_start_attempt[key] = now
        try:
            self._launcher(Path(candidate.path))
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrowserProgramError(
                "BROWSER_PROGRAM_START_FAILED",
                "浏览器软件启动失败，请由执行端维护人员手动打开",
            ) from exc
        return BrowserProgramStartResult(
            provider=key,
            started=True,
            candidateId=candidate.candidate_id,
            message=f"已向 {spec.display_name} 发送一次启动命令",
        )

    def _candidate_sources(
        self,
        spec: BrowserProgramSpec,
        setting: BaseModel,
    ) -> Iterable[tuple[Path, str]]:
        if spec.provider == "ruyi":
            from ..ruyi_browser_s.ruyi_browser_probe import probe_ruyi
            probe = probe_ruyi(setting)
            self._probe_notes[spec.provider] = probe.message
            if probe.ready and probe.browser_path is not None:
                canonical = probe.browser_path.resolve(strict=True)
                self._runtime_paths.add(str(canonical).casefold())
                yield canonical, "managed_runtime"
            return
        # 安装记录先于旧配置；避免同一路径先被旧来源去重，丢失无签名安装的凭据。
        yield from _registry_uninstall_paths(spec)
        saved = str(getattr(setting, "program_path", "") or "").strip()
        if saved:
            yield Path(saved), "saved_setting"
        configured = str(getattr(setting, "browser_path", "") or "").strip()
        if configured:
            from server.python.utils_s.path_utils import get_server_paths
            yield get_server_paths().resolve_resource(configured), "saved_setting"
        yield from _registry_app_paths(spec)
        yield from _default_program_paths(spec)
        yield from _running_process_paths(spec)

    def _inspect(
        self,
        spec: BrowserProgramSpec,
        path: Path,
        source: str,
        *,
        force: bool = False,
    ) -> BrowserProgramCandidate:
        canonical = path.expanduser().resolve(strict=True)
        if not canonical.is_file():
            raise OSError("程序文件不存在")
        stat = canonical.stat()
        stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
        cache_key = str(canonical).casefold()
        with self._lock:
            cached = self._inspection_cache.get(cache_key)
        if not force and cached and cached[0] == stamp and self._monotonic() - cached[1] < 60:
            _, _, digest, metadata = cached
        else:
            digest = _sha256(canonical)
            metadata = self._metadata_reader(canonical)
            current = canonical.stat()
            if stamp != (current.st_size, current.st_mtime_ns, current.st_ctime_ns, current.st_ino):
                raise ValueError("程序文件在检查过程中发生变化，请重新探测")
            with self._lock:
                self._inspection_cache[cache_key] = (stamp, self._monotonic(), digest, metadata)
        name_ok = canonical.name.casefold() in {
            name.casefold() for name in spec.executable_names
        }
        product_ok = metadata.product_name.casefold() in {
            name.casefold() for name in spec.product_names
        }
        signature_ok = metadata.signature_status.casefold() == "valid"
        publisher_text = metadata.publisher.casefold()
        publisher_ok = any(
            fragment.casefold() in publisher_text
            for fragment in spec.publisher_fragments
        )
        unsigned_registry_install_ok = (
            spec.allow_unsigned_registry_install
            and metadata.signature_status.casefold() in {"notsigned", "valid"}
            and _registered_install_matches(spec, canonical)
        )
        runtime_ok = (spec.provider == "ruyi" and cache_key in self._runtime_paths
                      and metadata.signature_status.casefold() in {"valid", "notsigned"})
        identity_ok = (signature_ok and publisher_ok) or unsigned_registry_install_ok or runtime_ok
        reasons: list[str] = []
        if not name_ok:
            reasons.append("程序文件名不符合厂商规则")
        if not product_ok:
            reasons.append("产品名称不符合厂商规则")
        if not identity_ok:
            if not signature_ok:
                reasons.append("程序签名无效或无法验证")
            if not publisher_ok:
                reasons.append("签名发布者不符合厂商规则")
        trusted = not reasons
        candidate_id = hashlib.sha256(
            "\0".join((spec.provider, str(canonical).casefold(), digest)).encode("utf-8")
        ).hexdigest()[:32]
        return BrowserProgramCandidate(
            candidateId=candidate_id,
            path=str(canonical),
            productName=metadata.product_name,
            version=metadata.version,
            publisher=metadata.publisher,
            signatureStatus=metadata.signature_status,
            sha256=digest,
            source=source,
            trusted=trusted,
            reason="；".join(reasons),
        )

    def _confirmed_candidate(
        self,
        spec: BrowserProgramSpec,
        setting: BaseModel,
    ) -> BrowserProgramCandidate | None:
        path_text = str(getattr(setting, "program_path", "") or "").strip()
        expected_id = str(
            getattr(setting, "program_candidate_id", "") or ""
        ).strip()
        expected_hash = str(getattr(setting, "program_sha256", "") or "").lower()
        if not path_text or not expected_id or len(expected_hash) != 64:
            return None
        try:
            candidate = self._inspect(
                spec,
                Path(path_text),
                str(getattr(setting, "program_source", "") or "saved_setting"),
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if (
            not candidate.trusted
            or candidate.candidate_id != expected_id
            or candidate.sha256.lower() != expected_hash
        ):
            return None
        return candidate

    def _require_spec(self, provider: str) -> BrowserProgramSpec:
        try:
            return self.specs[provider]
        except KeyError as exc:
            raise BrowserProgramError(
                "BROWSER_PROGRAM_PROBE_UNSUPPORTED",
                "当前浏览器尚未接入可信程序探针",
            ) from exc


def managed_program_values(
    current: BaseModel,
    settings: dict[str, object],
) -> dict[str, object]:
    """忽略网页传入的程序信任字段，只保留执行端已经确认的值。"""

    values = dict(settings)
    current_values = current.model_dump(mode="python")
    for field_name, alias in _MANAGED_PROGRAM_FIELDS.items():
        values.pop(field_name, None)
        values.pop(alias, None)
        values[field_name] = current_values.get(field_name)
    return values


def confirmed_program_updates(candidate: BrowserProgramCandidate) -> dict[str, object]:
    """生成只供 BrowserServer 本机确认流程写入的信任字段。"""

    from datetime import datetime, timezone

    return {
        "program_path": candidate.path,
        "program_candidate_id": candidate.candidate_id,
        "program_sha256": candidate.sha256,
        "program_version": candidate.version,
        "program_publisher": candidate.publisher,
        "program_source": candidate.source,
        "program_signature_status": candidate.signature_status,
        "program_confirmed_at": datetime.now(timezone.utc),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_windows_metadata(path: Path) -> _ProgramMetadata:
    escaped = str(path).replace("'", "''")
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "$ErrorActionPreference='Stop';"
        "Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1');"
        f"$p='{escaped}';"
        "$f=Get-Item -LiteralPath $p;"
        "$s=Get-AuthenticodeSignature -FilePath $p;"
        "[pscustomobject]@{"
        "product=[string]$f.VersionInfo.ProductName;"
        "version=[string]$f.VersionInfo.ProductVersion;"
        "status=[string]$s.Status;"
        "publisher=[string]$s.SignerCertificate.Subject"
        "}|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        # 父进程可能是 PowerShell 7；它的模块目录会让 Windows PowerShell 5
        # 误加载不兼容的 Security 模块，造成有效签名被读成 Unknown。
        env={key:value for key,value in os.environ.items() if key.upper()!='PSMODULEPATH'},
        **no_window_kwargs(),
    )
    payload = json.loads(completed.stdout or "{}")
    return _ProgramMetadata(
        product_name=str(payload.get("product") or "").strip(),
        version=str(payload.get("version") or "").strip(),
        publisher=str(payload.get("publisher") or "").strip(),
        signature_status=str(payload.get("status") or "Unknown").strip(),
    )


_external_launch_lock = RLock()


def _launch_visible_program(path: Path) -> object:
    # 外部客户端不能继承本软件单文件包的 DLL 目录或 PyInstaller 引导状态。
    # https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application
    env = dict(os.environ)
    frozen = bool(getattr(sys, 'frozen', False))
    if frozen:
        env = {key:value for key,value in env.items()
               if not key.startswith('_PYI_') and key != '_MEIPASS2'}
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        bundle = str(getattr(sys, '_MEIPASS', '')).rstrip('\\/').casefold()
        if bundle:
            for key in list(env):
                if key.upper() == 'PATH':
                    env[key] = os.pathsep.join(part for part in env[key].split(os.pathsep)
                        if not (part.strip('"').rstrip('\\/').casefold() == bundle
                                or part.strip('"').casefold().startswith(bundle + os.sep)))
    with _external_launch_lock:
        kernel = None
        if frozen and os.name == 'nt':
            import ctypes
            kernel = ctypes.windll.kernel32
            previous = ctypes.create_unicode_buffer(32768)
            kernel.GetDllDirectoryW(len(previous), previous)
            if not kernel.SetDllDirectoryW(None):
                raise OSError('无法恢复外部浏览器的系统 DLL 搜索路径')
        try:
            return subprocess.Popen(
                [str(path)], cwd=str(path.parent), close_fds=True, shell=False, env=env,
                # 图形程序仍显示自己的窗口，只隐藏附带的控制台。
                **no_window_kwargs(),
            )
        finally:
            if kernel is not None:
                kernel.SetDllDirectoryW(previous.value or None)


def _source_rank(source: str) -> int:
    return {
        "saved_setting": 0,
        "registry_app_paths": 1,
        "registry_uninstall": 2,
        "default_directory": 3,
        "running_process": 4,
    }.get(source, 99)


def _registry_app_paths(spec: BrowserProgramSpec) -> Iterable[tuple[Path, str]]:
    if winreg is None:
        return
    key_root = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
    for hive, access in _registry_locations():
        for executable in spec.executable_names:
            try:
                with winreg.OpenKey(
                    hive,
                    f"{key_root}\\{executable}",
                    0,
                    access,
                ) as key:
                    value = str(winreg.QueryValue(key, "") or "").strip()
            except OSError:
                continue
            if value:
                yield Path(value.strip('"')), "registry_app_paths"


def _registry_uninstall_paths(spec: BrowserProgramSpec) -> Iterable[tuple[Path, str]]:
    if winreg is None:
        return
    root_name = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    allowed = tuple(name.casefold() for name in spec.registry_display_names)
    for hive, access in _registry_locations():
        try:
            root = winreg.OpenKey(hive, root_name, 0, access)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, subkey_name, 0, access) as key:
                        display_name = _registry_string(key, "DisplayName")
                        folded = display_name.casefold()
                        if not any(
                            folded == name or folded.startswith(name + " ")
                            for name in allowed
                        ):
                            continue
                        install_location = _registry_string(key, "InstallLocation")
                        display_icon = _registry_string(key, "DisplayIcon")
                except OSError:
                    continue
                if install_location:
                    for executable in spec.executable_names:
                        yield Path(install_location) / executable, "registry_uninstall"
                icon_path = _display_icon_path(display_icon)
                # DisplayIcon 可能指向 MSI 图标缓存或 uninstall.exe，不能当浏览器。
                if icon_path and Path(icon_path).name.casefold() in {
                    name.casefold() for name in spec.executable_names
                }:
                    yield Path(icon_path), "registry_uninstall"
                elif icon_path:
                    # Electron 安装器常把 DisplayIcon 指向同目录的图标，AdsPower 即如此。
                    for executable in spec.executable_names:
                        yield Path(icon_path).parent / executable, "registry_uninstall"


def _registered_install_matches(spec: BrowserProgramSpec, path: Path) -> bool:
    """来源必须仍在本机厂商安装记录中；不能信任复制来的 program_source 字符串。"""
    for candidate, _ in _registry_uninstall_paths(spec):
        try:
            if candidate.resolve(strict=True) == path:
                return True
        except OSError:
            continue
    return False


def _default_program_paths(spec: BrowserProgramSpec) -> Iterable[tuple[Path, str]]:
    roots: list[Path] = []
    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            roots.append(Path(value))
    seen: set[str] = set()
    for root in roots:
        for relative in spec.default_relative_paths:
            path = root / Path(relative)
            folded = str(path).casefold()
            if folded in seen:
                continue
            seen.add(folded)
            yield path, "default_directory"


def _running_process_paths(spec: BrowserProgramSpec) -> Iterable[tuple[Path, str]]:
    if os.name != "nt":
        return
    names = ",".join(
        "'" + name.replace("'", "''") + "'" for name in spec.executable_names
    )
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        f"$n=@({names});"
        "@(Get-CimInstance Win32_Process | Where-Object {$_.Name -in $n} | "
        "Select-Object -ExpandProperty ExecutablePath)|ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            **no_window_kwargs(),
        )
        payload = json.loads(completed.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    values = [payload] if isinstance(payload, str) else payload
    for value in values if isinstance(values, list) else []:
        text = str(value or "").strip()
        if text:
            yield Path(text), "running_process"


def _registry_locations() -> Iterable[tuple[object, int]]:
    if winreg is None:
        return
    views = [getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)]
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in dict.fromkeys(views):
            yield hive, winreg.KEY_READ | view


def _registry_string(key, name: str) -> str:
    try:
        return str(winreg.QueryValueEx(key, name)[0] or "").strip()
    except OSError:
        return ""


def _display_icon_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 1 else ""
    return text.split(",", 1)[0].strip()


__all__ = [
    "BrowserProgramError",
    "BrowserProgramService",
    "BrowserProgramSpec",
    "confirmed_program_updates",
    "managed_program_values",
]
