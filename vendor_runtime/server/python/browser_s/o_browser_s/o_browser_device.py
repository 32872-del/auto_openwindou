"""O 浏览器 Android 设备缓存的读取与选择。

网络刷新属于 UI/基础设施职责；运行规划器只读取已落盘缓存，避免创建窗口时临时
访问远程接口。
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from server.python.utils_s.path_utils import get_server_paths

OCTO_ANDROID_BRANDS = (
    "三星",
    "小米",
    "OPPO",
    "vivo",
    "摩托",
    "realme",
    "OnePlus",
    "其它",
)
OCTO_ANDROID_BRAND_LABELS = {
    "三星": "三星 Samsung",
    "小米": "小米/Redmi/POCO",
    "OPPO": "OPPO",
    "vivo": "vivo",
    "摩托": "Motorola",
    "realme": "realme",
    "OnePlus": "OnePlus",
    "其它": "其它 Other",
}
OCTO_AUTOMATION_API = "https://app.octobrowser.net/api/v2/automation"


class OBrowserDeviceCatalog:
    def __init__(
        self,
        cache_file: str | Path | None = None,
        built_in_file: str | Path | None = None,
    ) -> None:
        paths = get_server_paths()
        self.cache_file = Path(
            cache_file
            or (
                paths.browser_cache_dir
                / "O浏览器"
                / "设备型号.json"
            )
        )
        self.built_in_file = Path(
            built_in_file
            or paths.bundled_resource_root
            / "资源"
            / "浏览器"
            / "O浏览器设备型号.json"
        )

    def load(self) -> dict[str, list[dict[str, Any]]]:
        payload: Any = None
        for source in (self.cache_file, self.built_in_file):
            try:
                payload = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                break
        if not isinstance(payload, dict):
            return {}
        return {
            str(brand): [dict(item) for item in items if isinstance(item, Mapping)]
            for brand, items in payload.items()
            if isinstance(items, list)
        }

    def save(self, grouped: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            str(brand): [dict(item) for item in items]
            for brand, items in grouped.items()
        }
        temporary = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        temporary.replace(self.cache_file)

    def counts(self) -> dict[str, int]:
        cache = self.load()
        return {
            brand: len(cache.get(brand, ()))
            for brand in OCTO_ANDROID_BRANDS
        }

    def refresh(
        self,
        api_token: str,
        *,
        base_url: str = OCTO_AUTOMATION_API,
        timeout: int = 20,
    ) -> dict[str, list[dict[str, Any]]]:
        import requests

        if not api_token.strip():
            raise ValueError("缺少 Octo API token")
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            f"{base_url.rstrip('/')}/fingerprint/device_models",
            headers={"X-Octo-Api-Token": api_token.strip()},
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            raise RuntimeError("Octo device_models 返回失败")
        grouped: dict[str, list[dict[str, Any]]] = {
            brand: [] for brand in OCTO_ANDROID_BRANDS
        }
        for item in body.get("data") or ():
            if (
                not isinstance(item, Mapping)
                or item.get("device_type") != "phone"
                or item.get("os") != "android"
            ):
                continue
            value = str(item.get("value") or "")
            grouped[_classify_brand(value)].append({
                "value": value,
                "os_versions": list(item.get("os_versions") or ()),
            })
        self.save(grouped)
        return grouped

    def choose(
        self,
        brand_weights: Mapping[str, int],
        *,
        rng: random.Random,
    ) -> tuple[str, str]:
        cache = self.load()
        weighted = [
            (brand, max(0, int(weight)))
            for brand, weight in brand_weights.items()
            if max(0, int(weight)) > 0 and cache.get(brand)
        ]
        if not weighted:
            weighted = [
                (brand, 1)
                for brand in OCTO_ANDROID_BRANDS
                if cache.get(brand)
            ]
        if not weighted:
            return "", ""

        brand = rng.choices(
            [item[0] for item in weighted],
            weights=[item[1] for item in weighted],
            k=1,
        )[0]
        model = rng.choice(cache[brand])
        model_name = str(model.get("value") or "").strip()
        versions: Sequence[Any] = model.get("os_versions") or ()
        os_version = str(rng.choice(versions)).strip() if versions else ""
        return model_name, os_version


def _classify_brand(value: str) -> str:
    upper = str(value or "").upper()
    if upper.startswith("SM-"):
        return "三星"
    if upper.startswith("CPH"):
        return "OPPO"
    if upper.startswith("RMX"):
        return "realme"
    if re.match(r"V2\d", upper):
        return "vivo"
    if upper.startswith(("MOT", "XT")):
        return "摩托"
    if upper.startswith("M2") or re.match(r"^2[0-4]\d", upper):
        return "小米"
    if re.match(r"^(IN|LE|KB|EB|DN|BE|AC|HD|GM)\d", upper):
        return "OnePlus"
    return "其它"


__all__ = ["OBrowserDeviceCatalog"]
