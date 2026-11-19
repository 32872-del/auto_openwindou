"""浏览器公共实现：把统一配置转换成各家指纹参数。"""

from __future__ import annotations

import random
import threading
from typing import Any, Mapping

from ..browser_task_setting import (
    BrowserProviderSettings,
    FingerprintSetting,
    FingerprintSettings,
)
from ..o_browser_s.o_browser_device import OBrowserDeviceCatalog


_OS_ORDER = (
    ("Win/Win10", "win", "10", "x86"),
    ("Win/Win11", "win", "11", "x86"),
    ("Mac/Intel", "mac", "", "x86"),
    ("Mac/ARM", "mac", "", "arm"),
    ("Android", "android", "", "arm"),
)


class BrowserFingerprint:
    """线程安全的浏览器指纹生成器。

    sequential 会在同一浏览器的每次创建时轮转，而不是按线程编号固定；
    因此单线程连续提交也能依次使用多套 OS。
    """

    def __init__(
        self,
        *,
        rng: random.Random | None = None,
        o_browser_devices: OBrowserDeviceCatalog | None = None,
    ) -> None:
        self._rng = rng or random.Random()
        self._o_browser_devices = (
            o_browser_devices or OBrowserDeviceCatalog()
        )
        self._sequential_indexes: dict[str, int] = {}
        self._lock = threading.Lock()

    def build(
        self,
        provider: Any,
        settings: BrowserProviderSettings,
    ) -> FingerprintSetting:
        fp = settings.fingerprint
        os_name, os_version, architecture = self._choose_os(provider.key, fp)
        # O 的 macOS 支持池由厂商维护；未指定版本时省略可选字段，避免静态
        # 随机池里的旧版本被当前 API 拒绝。其它厂商沿用各自版本选择逻辑。
        if os_name == "mac" and not os_version and provider.key not in ("octo", "adspower"):
            with self._lock:
                # MoreLogin 的适配器当前只生成 12～14，展示和提交须为同一版本。
                versions = ("12", "13", "14") if provider.key == "morelogin" else ("12", "13", "14", "15")
                os_version = self._rng.choice(versions)

        device_model = ""
        device_type = ""
        if os_name == "android":
            device_type = "phone"
            device_model, device_os = self._android_device(provider, fp)
            os_version = device_os or os_version

        options = self._provider_options(
            provider,
            fp,
            os_name,
            os_version,
            architecture,
        )
        return FingerprintSetting(
            os=os_name,
            os_version=os_version,
            architecture=architecture,
            device_type=device_type,
            device_model=device_model,
            options=options,
        )

    def _choose_os(
        self,
        provider_key: str,
        settings: FingerprintSettings,
    ) -> tuple[str, str, str]:
        configured = [
            (key, os_name, version, architecture, int(settings.os_weights.get(key, 0)))
            for key, os_name, version, architecture in _OS_ORDER
            if int(settings.os_weights.get(key, 0)) > 0
        ]
        if not configured:
            configured = [(*_OS_ORDER[1], 1)]

        with self._lock:
            if settings.os_mode == "weighted":
                selected = self._rng.choices(
                    configured,
                    weights=[item[4] for item in configured],
                    k=1,
                )[0]
            else:
                index = self._sequential_indexes.get(provider_key, 0)
                selected = configured[index % len(configured)]
                self._sequential_indexes[provider_key] = index + 1
        return selected[1], selected[2], selected[3]

    def _android_device(
        self,
        provider: Any,
        settings: FingerprintSettings,
    ) -> tuple[str, str]:
        extras=settings.model_extra or {}
        requested=extras.get('android_models') or []
        versions=extras.get('android_versions') or []
        if requested or versions or (provider.key=='hub' and settings.android_brand_weights):
            models=list(provider.list_device_models())
            if requested:
                missing=set(requested)-{m.value for m in models}
                if missing:raise ValueError('指定 Android 型号已经不可用：'+', '.join(sorted(missing)))
                models=[m for m in models if m.value in requested]
            weights=settings.android_brand_weights
            if weights:models=[m for m in models if weights.get(m.brand,0)>0]
            if versions:models=[m for m in models if set(m.os_versions)&set(versions)]
            if not models:raise ValueError('所选 Android 品牌、型号与版本没有可用组合，请刷新型号目录')
            with self._lock:
                model=self._rng.choices(models,weights=[weights.get(m.brand,1) for m in models],k=1)[0]
                pool=[v for v in model.os_versions if not versions or v in versions]
                return model.value,self._rng.choice(pool) if pool else ''
        with self._lock:
            if provider.key == "hub":
                extras = settings.model_extra or {}
                configured = [
                    str(value).strip()
                    for value in extras.get("phone_models", ())
                    if str(value).strip() in provider.ANDROID_PHONE_MODELS
                ]
                pool = configured or list(provider.ANDROID_PHONE_MODELS)
                return self._rng.choice(pool), ""
            if provider.key == "octo":
                return self._o_browser_devices.choose(
                    settings.android_brand_weights,
                    rng=self._rng,
                )
        return "", ""

    def _provider_options(
        self,
        provider: Any,
        settings: FingerprintSettings,
        os_name: str,
        os_version: str,
        architecture: str,
    ) -> dict[str, Any]:
        core = self._choice(settings.chrome_versions)
        cpu = self._choice(settings.cpu_cores)
        memory = self._choice(settings.memory_gb)
        resolution = self._choice(settings.resolutions)
        noise = bool(settings.noise)
        key = provider.key

        if key == "kameleo":
            # 由厂商真实指纹库决定硬件；只传用户指定的版本或指纹 ID。
            extras = settings.model_extra or {}
            ids = extras.get("fingerprint_ids") or []
            return {
                **({"fingerprint_id": self._choice(ids)} if ids else {}),
                **({"core_version": core} if core else {}),
                "browser_product": str(extras.get("browser_product") or "chrome"),
            }
        if key == "octo":
            # Octo 由服务端生成相互匹配的硬件指纹；这里只固定 OS/架构/Android
            # 机型，避免手工拼 CPU/UA/GPU 破坏一致性。
            return {}
        if key == "ruyi":
            extras = settings.model_extra or {}
            mode = str(extras.get("ruyi_fingerprint_mode") or "random").lower()
            template = str(extras.get("ruyi_fingerprint_template") or "").strip()
            return {
                "ruyi_fingerprint_mode": "fixed" if mode == "fixed" else "random",
                **(
                    {"ruyi_fingerprint_template": template}
                    if template
                    else {}
                ),
            }
        if key == "hub":
            configured = [
                int(value)
                for value in settings.chrome_versions
                if str(value).isdigit() and int(value) > 0
            ]
            versions = configured
            if not versions:
                try:
                    versions = list(provider.available_core_versions())
                except Exception as exc:
                    raise ValueError("Hub 已安装内核目录读取失败，请检查内核目录或明确选择已验证的内核版本") from exc
                if not versions:
                    raise ValueError("Hub 未找到已安装内核，请设置内核目录或明确选择已验证的内核版本")
            selected_core = (
                self._choice(configured)
                if configured
                else max(versions)
            )
            advanced: dict[str, Any] = {
                "uaVersion": max(107, min(138, int(selected_core))),
            }
            if cpu is not None:
                advanced["cpu"] = cpu
            if memory is not None:
                advanced["memory"] = memory
            return {"core_version": int(selected_core), "advanced": advanced}
        if key == "morelogin":
            if core is None:
                core = self._choice(provider.available_core_versions()[:3])
            cpu, memory = self._morelogin_hardware(settings, os_name)
            switcher = 1 if noise else 2
            advanced: dict[str, Any] = {
                "hardware_concurrency": cpu,
                "memery_device": memory,
                # MoreLogin 的 2 表示自定义，必须提供厂商分辨率 ID；未指定时用默认真实值。
                "resolution": {"switcher": 1},
                "canvas": {"switcher": switcher},
                "webgl_image": {"switcher": switcher},
                "audio_context": {"switcher": switcher},
                "media_device": {"switcher": switcher},
                "client_rects": {"switcher": switcher},
                "speech_voise": {"switcher": switcher},
                "webgl_metadata": {"switcher": 3},
                "time_zone": {"switcher": 1},
                "language": {"switcher": 1},
                "geo_location": {"switcher": 1, "base_on_ip": True},
                "web_rtc": {"switcher": 2},
            }
            if os_name == "mac":
                advanced["os_version"] = f"macOS {os_version}"
            return {
                **({"browser_core": int(core)} if str(core or "").isdigit() else {}),
                "advanced": advanced,
            }
        if key == "undetectable":
            options = self._common_options(
                core=None,
                cpu=cpu,
                memory=memory,
                resolution=resolution,
                noise=None,
            )
            source = str((settings.model_extra or {}).get("fp_source") or "pool").lower()
            if source == "pool" and hasattr(provider, "list_fingerprint_configs"):
                configs = provider.list_fingerprint_configs()
                requested = (settings.model_extra or {}).get("config_ids") or []
                if requested:
                    missing = set(map(str, requested)) - set(map(str, configs))
                    if missing:
                        raise ValueError("U 指纹库中已找不到指定配置：" + ", ".join(sorted(missing)) + "；请刷新配置库重新选择")
                    configs = {str(k): v for k, v in configs.items() if str(k) in requested}
                config_id = self._pick_undetectable_config(
                    configs,
                    os_name=os_name,
                    os_version=os_version,
                    architecture=architecture,
                )
                if config_id:
                    options["config_id"] = config_id
                elif requested:
                    raise ValueError("指定的 U 配置与本次系统不匹配；请调整系统范围或重新选择配置，不会改用其他配置")
            return options
        if key == "hidemium":
            options = self._common_options(
                core=core,
                cpu=cpu,
                memory=memory,
                resolution=resolution,
                noise=noise,
            )
            language = str(
                (settings.model_extra or {}).get("language") or ""
            ).strip()
            if language:
                options["language"] = language
            return options
        return self._common_options(
            core=core,
            cpu=cpu,
            memory=memory,
            resolution=resolution,
            noise=noise,
        )

    def _morelogin_hardware(
        self, settings: FingerprintSettings, os_name: str,
    ) -> tuple[int, int]:
        # 留空使用厂商公开的档位；以组合为单位抽取，避免高核数配低内存。
        # 这是默认搭配策略，不是对所有实际硬件配置的判定。
        cpus = settings.cpu_cores or ((4, 8) if os_name == "android" else (2, 4, 6, 8, 10, 12))
        memories = settings.memory_gb or ((4, 6, 8) if os_name == "android" else (4, 8))
        pairs = sorted({
            (int(cpu), int(memory)) for cpu in cpus for memory in memories
            if int(cpu) > 0 and int(cpu) <= int(memory) <= int(cpu) * 4
        })
        if not pairs:
            raise ValueError(
                "MoreLogin CPU 与内存没有可用的合理搭配；内存 GB 需在 CPU 核数的 1–4 倍之间。"
                "留空内存使用厂商支持的 4/8GB（Android 另含 6GB），请调整候选值或同时填写两项。"
            )
        return self._choice(pairs)

    @staticmethod
    def _common_options(
        *,
        core: Any,
        cpu: Any,
        memory: Any,
        resolution: Any,
        noise: bool | None,
    ) -> dict[str, Any]:
        values = {
            "core_version": core,
            "cpu_cores": cpu,
            "memory_gb": memory,
            "resolution": resolution,
            "noise": noise,
        }
        return {
            key: value
            for key, value in values.items()
            if value not in (None, "")
        }

    def _choice(self, values) -> Any:
        if not values:
            return None
        with self._lock:
            return self._rng.choice(list(values))

    def _pick_undetectable_config(
        self,
        configs: Mapping[str, Any],
        *,
        os_name: str,
        os_version: str,
        architecture: str,
    ) -> str:
        target = (
            f"Windows {os_version or '11'}"
            if os_name == "win"
            else "Mac"
            if os_name == "mac"
            else "Android"
            if os_name == "android"
            else "Linux"
        ).lower()
        raw: list[str] = []
        current: list[str] = []
        strict: list[str] = []
        for config_id, info in configs.items():
            if not isinstance(info, Mapping):
                continue
            config_os = str(info.get("os") or "").lower()
            if not config_os or not (target in config_os or config_os in target):
                continue
            webgl = str(info.get("webgl") or "").lower()
            if os_name == "mac" and architecture == "x86" and ("apple m" in webgl or "apple gpu" in webgl):
                continue
            raw.append(str(config_id))
            user_agent = str(info.get("useragent") or "").lower()
            if "windows nt 6.1" in user_agent or "windows nt 6.3" in user_agent:
                continue
            current.append(str(config_id))
            if architecture == "arm":
                if "apple m" in webgl or "apple gpu" in webgl:
                    strict.append(str(config_id))
            elif architecture == "x86":
                if any(value in webgl for value in ("intel", "iris", "uhd")):
                    strict.append(str(config_id))
            else:
                strict.append(str(config_id))
        # macOS Apple Silicon 不能拿 Intel 指纹库配置充数；其它系统的显卡
        # 可能是 AMD/NVIDIA，无法仅凭 WebGL 名称判定架构。
        pool = strict if os_name == "mac" and architecture == "arm" else strict or current or raw
        return str(self._choice(pool) or "")


__all__ = ["BrowserFingerprint"]
