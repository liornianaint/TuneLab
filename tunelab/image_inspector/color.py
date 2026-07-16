"""Vectorised sRGB, HSV, XYZ and CIE Lab calculations for final images."""

from __future__ import annotations

from typing import Any, Tuple

import numpy as np


D65_WHITE = np.array((0.95047, 1.0, 1.08883), dtype=np.float64)
SRGB_TO_XYZ = np.array(
    (
        (0.4124564, 0.3575761, 0.1804375),
        (0.2126729, 0.7151522, 0.0721750),
        (0.0193339, 0.1191920, 0.9503041),
    ),
    dtype=np.float64,
)


def _as_rgb_array(rgb: Any) -> np.ndarray:
    values = np.asarray(rgb, dtype=np.float64)
    if values.shape == (3,):
        return values
    if values.ndim < 1 or values.shape[-1] != 3:
        raise ValueError("RGB 数据最后一维必须为 3。")
    return values


def srgb_to_linear(rgb: Any, *, scale: float = 255.0) -> np.ndarray:
    """Decode gamma-encoded sRGB to linear RGB in [0, 1]."""

    if scale <= 0:
        raise ValueError("scale 必须大于 0。")
    encoded = np.clip(_as_rgb_array(rgb) / scale, 0.0, 1.0)
    return np.where(
        encoded <= 0.04045,
        encoded / 12.92,
        np.power((encoded + 0.055) / 1.055, 2.4),
    )


def linear_rgb_to_xyz(linear_rgb: Any) -> np.ndarray:
    """Convert linear-light sRGB to D65 XYZ, with Y=1 for reference white."""

    values = _as_rgb_array(linear_rgb)
    return np.matmul(values, SRGB_TO_XYZ.T)


def xyz_to_lab(xyz: Any) -> np.ndarray:
    values = _as_rgb_array(xyz) / D65_WHITE
    delta = 6.0 / 29.0
    threshold = delta ** 3
    transformed = np.where(
        values > threshold,
        np.cbrt(values),
        values / (3.0 * delta * delta) + 4.0 / 29.0,
    )
    x, y, z = np.moveaxis(transformed, -1, 0)
    return np.stack((116.0 * y - 16.0, 500.0 * (x - y), 200.0 * (y - z)), axis=-1)


def srgb_to_xyz(rgb: Any, *, scale: float = 255.0) -> np.ndarray:
    return linear_rgb_to_xyz(srgb_to_linear(rgb, scale=scale))


def rgb_to_lab(rgb: Any, *, scale: float = 255.0) -> np.ndarray:
    return xyz_to_lab(srgb_to_xyz(rgb, scale=scale))


def relative_luminance(rgb: Any, *, scale: float = 255.0) -> np.ndarray:
    linear = srgb_to_linear(rgb, scale=scale)
    return np.matmul(linear, np.array((0.2126, 0.7152, 0.0722), dtype=np.float64))


def rgb_to_hsv(rgb: Any, *, scale: float = 255.0) -> np.ndarray:
    """Return hue in degrees and saturation/value in [0, 1]."""

    values = np.clip(_as_rgb_array(rgb) / scale, 0.0, 1.0)
    maximum = np.max(values, axis=-1)
    minimum = np.min(values, axis=-1)
    chroma = maximum - minimum
    safe_chroma = np.where(chroma == 0.0, 1.0, chroma)
    r, g, b = np.moveaxis(values, -1, 0)

    hue = np.zeros_like(maximum)
    r_mask = (chroma != 0.0) & (maximum == r)
    g_mask = (chroma != 0.0) & (~r_mask) & (maximum == g)
    b_mask = (chroma != 0.0) & (~r_mask) & (~g_mask)
    hue = np.where(r_mask, 60.0 * np.mod((g - b) / safe_chroma, 6.0), hue)
    hue = np.where(g_mask, 60.0 * (((b - r) / safe_chroma) + 2.0), hue)
    hue = np.where(b_mask, 60.0 * (((r - g) / safe_chroma) + 4.0), hue)
    saturation = np.divide(chroma, maximum, out=np.zeros_like(chroma), where=maximum != 0.0)
    return np.stack((hue, saturation, maximum), axis=-1)


def circular_hue_mean(hues: Any, weights: Any) -> float:
    hue_values = np.asarray(hues, dtype=np.float64)
    weight_values = np.asarray(weights, dtype=np.float64)
    total = float(np.sum(weight_values))
    if total <= 1e-12:
        return 0.0
    radians = np.deg2rad(hue_values)
    angle = np.arctan2(
        float(np.sum(np.sin(radians) * weight_values)),
        float(np.sum(np.cos(radians) * weight_values)),
    )
    return float(np.mod(np.rad2deg(angle), 360.0))


def mean_hsv(rgb: Any, *, scale: float = 255.0) -> Tuple[float, float, float]:
    hsv = rgb_to_hsv(rgb, scale=scale).reshape(-1, 3)
    return (
        circular_hue_mean(hsv[:, 0], hsv[:, 1]),
        float(np.mean(hsv[:, 1])),
        float(np.mean(hsv[:, 2])),
    )
