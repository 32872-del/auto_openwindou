"""CSS、客户区物理像素和指纹屏幕坐标的纯换算。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class CoordinateCalibration:
    scale: float
    offset_x: float
    offset_y: float
    screen_origin_x: float
    screen_origin_y: float

    def css_to_client(self, x: float, y: float) -> tuple[float, float]:
        return (
            float(x) * self.scale + self.offset_x,
            float(y) * self.scale + self.offset_y,
        )

    def client_to_css(self, x: float, y: float) -> tuple[float, float]:
        return (
            (float(x) - self.offset_x) / self.scale,
            (float(y) - self.offset_y) / self.scale,
        )


def screen_css_to_physical(value: float, scale: float) -> float:
    return float(value) * (float(scale) or 1.0)


def calibration_offset(physical: float, css: float, scale: float) -> float:
    return float(physical) - float(css) * (float(scale) or 1.0)


def calibration_pair_scale(
    first: Mapping[str, object],
    second: Mapping[str, object],
    first_probe: Sequence[float],
    second_probe: Sequence[float],
    tolerance: float = 2.5,
) -> float | None:
    """只按两次相对位移反推比例，不相信可被指纹环境伪装的绝对原点。"""

    if first.get("trusted") is False or second.get("trusted") is False:
        return None
    expected_dx = float(second_probe[0]) - float(first_probe[0])
    expected_dy = float(second_probe[1]) - float(first_probe[1])
    ratios: list[float] = []
    for x_key, y_key in (("x", "y"), ("sx", "sy")):
        try:
            actual_dx = float(second[x_key]) - float(first[x_key])
            actual_dy = float(second[y_key]) - float(first[y_key])
        except (KeyError, TypeError, ValueError):
            return None
        for expected, actual in ((expected_dx, actual_dx), (expected_dy, actual_dy)):
            if abs(actual) < 1e-6:
                return None
            ratio = expected / actual
            if ratio <= 0:
                return None
            ratios.append(ratio)
    ratios.sort()
    scale = (ratios[1] + ratios[2]) / 2.0
    if scale < 0.25 or scale > 4.0:
        return None
    for x_key, y_key in (("x", "y"), ("sx", "sy")):
        actual_dx = (float(second[x_key]) - float(first[x_key])) * scale
        actual_dy = (float(second[y_key]) - float(first[y_key])) * scale
        if abs(actual_dx - expected_dx) > tolerance or abs(actual_dy - expected_dy) > tolerance:
            return None
    return scale


def calibration_pair_matches(
    first: Mapping[str, object],
    second: Mapping[str, object],
    first_probe: Sequence[float],
    second_probe: Sequence[float],
    dpr: float = 1.0,
    tolerance: float = 2.5,
) -> bool:
    """兼容旧签名；DPR 只保留作诊断输入，不参与比例假设。"""

    del dpr
    return calibration_pair_scale(
        first,
        second,
        first_probe,
        second_probe,
        tolerance=tolerance,
    ) is not None


def find_calibration_pair(
    samples_by_probe: Sequence[Sequence[Mapping[str, object]]],
    probes: Sequence[Sequence[float]],
    dpr: float = 1.0,
):
    limit = min(len(samples_by_probe), len(probes))
    for second_index in range(1, limit):
        second_samples = samples_by_probe[second_index] or ()
        for first_index in range(second_index - 1, -1, -1):
            first_samples = samples_by_probe[first_index] or ()
            for first in reversed(first_samples):
                for second in reversed(second_samples):
                    if calibration_pair_matches(
                        first,
                        second,
                        probes[first_index],
                        probes[second_index],
                        dpr,
                    ):
                        return (
                            (probes[first_index][0], probes[first_index][1], first),
                            (probes[second_index][0], probes[second_index][1], second),
                        )
    return None


__all__ = [
    "CoordinateCalibration",
    "calibration_offset",
    "calibration_pair_matches",
    "calibration_pair_scale",
    "find_calibration_pair",
    "screen_css_to_physical",
]
