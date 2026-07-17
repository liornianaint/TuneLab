"""Image loading plus pixel/ROI statistics, independent from Tk widgets."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple, Union

import numpy as np

from .color import rgb_to_hsv, rgb_to_lab, relative_luminance
from .constants import (
    COLOR_CONVERSION_CHUNK_PIXELS,
    DARK_PIXEL_VALUE,
    HIGHLIGHT_CLIP_VALUE,
    MAX_HIGH_PRECISION_IMAGE_PIXELS,
    MAX_IMAGE_PIXELS,
    STABILITY_HIGH_STD,
    STABILITY_MEDIUM_STD,
)
from .types import ComparisonResult, ImageData, PixelMetrics, ROI, ROIStatistics


LOGGER = logging.getLogger(__name__)
CHANNELS = ("R", "G", "B")


class ImageInspectorError(ValueError):
    pass


class ImageLoadError(ImageInspectorError):
    pass


class ROIError(ImageInspectorError):
    pass


def _detect_bit_depth(image: Any) -> int:
    mode = str(getattr(image, "mode", ""))
    if mode.startswith("I;16"):
        return 16
    if mode == "F":
        return 32
    info_bits = getattr(image, "info", {}).get("bits")
    if isinstance(info_bits, int) and info_bits > 0:
        return info_bits
    try:
        tag_bits = image.tag_v2.get(258)
        if isinstance(tag_bits, (tuple, list)) and tag_bits:
            return int(max(tag_bits))
        if tag_bits:
            return int(tag_bits)
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    if mode == "1":
        return 1
    if mode == "I":
        try:
            extrema = image.getextrema()
            maximum = int(extrema[1])
            if maximum <= 65535:
                return 16
        except (TypeError, ValueError):
            pass
        return 32
    return 8


def _scale_integer_array(values: np.ndarray, bit_depth: int) -> np.ndarray:
    maximum = float((1 << min(max(bit_depth, 1), 32)) - 1)
    return np.clip(values.astype(np.float64), 0.0, maximum) * (255.0 / maximum)


def _normalise_float_array(values: np.ndarray) -> np.ndarray:
    data = values.astype(np.float64)
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        raise ImageLoadError("浮点图片没有可分析的有限像素值。")
    low = float(np.min(finite))
    high = float(np.max(finite))
    if 0.0 <= low and high <= 1.0:
        return np.clip(data, 0.0, 1.0) * 255.0
    if 0.0 <= low and high <= 255.0:
        return np.clip(data, 0.0, 255.0)
    if high <= low:
        return np.zeros_like(data, dtype=np.float64)
    return np.clip((data - low) * (255.0 / (high - low)), 0.0, 255.0)


def histogram_rgb(display_rgb: np.ndarray) -> np.ndarray:
    pixels = np.asarray(display_rgb, dtype=np.uint8).reshape(-1, 3)
    return np.stack(
        [np.bincount(pixels[:, channel], minlength=256) for channel in range(3)],
        axis=0,
    ).astype(np.int64)


def load_image(path: Union[str, Path]) -> ImageData:
    """Load a supported image, apply EXIF orientation, and retain useful precision."""

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise ImageLoadError("TuneLab 默认图像依赖 Pillow 未安装；请重新同步工程环境。") from exc

    source_path = Path(path).expanduser()
    if source_path.suffix.lower() in {".heic", ".heif"}:
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError as exc:
            raise ImageLoadError(
                "HEIC/HEIF 解码依赖 pillow-heif 未安装；请运行 python3 run_tunelab.py 重新同步工程环境。"
            ) from exc
    if not source_path.is_file():
        raise ImageLoadError(f"图片不存在：{source_path}")
    try:
        with Image.open(source_path) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0:
                raise ImageLoadError("图片尺寸无效。")
            bit_depth = _detect_bit_depth(opened)
            pixel_limit = MAX_HIGH_PRECISION_IMAGE_PIXELS if bit_depth > 8 else MAX_IMAGE_PIXELS
            if width * height > pixel_limit:
                raise ImageLoadError(
                    f"图片包含 {width * height:,} 像素，超过该位深的首版安全上限 {pixel_limit:,}；"
                    "请先生成较小副本后再分析。"
                )
            source_mode = opened.mode
            try:
                orientation = int(opened.getexif().get(274, 1))
            except (AttributeError, TypeError, ValueError):
                orientation = 1
            oriented = ImageOps.exif_transpose(opened)
            oriented.load()

            raw = np.asarray(oriented)
            alpha: Optional[np.ndarray] = None
            precision_preserved = True
            original_dtype = str(raw.dtype)

            if oriented.mode == "P" and "transparency" in oriented.info:
                rgba = np.asarray(oriented.convert("RGBA"), dtype=np.uint8)
                rgb = rgba[..., :3]
                alpha = rgba[..., 3]
            elif (
                raw.ndim == 3
                and raw.shape[-1] >= 3
                and raw.dtype == np.uint8
                and oriented.mode in {"RGB", "RGBA"}
            ):
                # Reuse Pillow's decoded bytes for the two overwhelmingly
                # common modes instead of allocating another full RGB image.
                rgb = raw[..., :3]
                if oriented.mode == "RGBA":
                    alpha = raw[..., 3]
            elif raw.ndim == 3 and raw.shape[-1] >= 3 and raw.dtype.kind in "uif" and raw.dtype.itemsize > 1:
                bit_depth = max(bit_depth, raw.dtype.itemsize * 8)
                if raw.dtype.kind == "f":
                    rgb = _normalise_float_array(raw[..., :3])
                else:
                    rgb = _scale_integer_array(raw[..., :3], bit_depth)
                if raw.shape[-1] >= 4:
                    alpha_values = raw[..., 3]
                    alpha = (
                        _normalise_float_array(alpha_values)
                        if alpha_values.dtype.kind == "f"
                        else _scale_integer_array(alpha_values, bit_depth)
                    )
            elif raw.ndim == 2 and (oriented.mode.startswith("I") or oriented.mode == "F"):
                if raw.dtype.kind == "f":
                    gray = _normalise_float_array(raw)
                else:
                    if bit_depth == 32 and raw.size and int(np.max(raw)) <= 65535:
                        bit_depth = 16
                    gray = _scale_integer_array(raw, bit_depth)
                rgb = np.repeat(gray[..., np.newaxis], 3, axis=-1)
            else:
                if "A" in oriented.getbands():
                    alpha_raw = np.asarray(oriented.getchannel("A"))
                    alpha = np.asarray(alpha_raw, dtype=np.uint8)
                rgb = np.asarray(oriented.convert("RGB"), dtype=np.uint8)
                if bit_depth > 8:
                    # Pillow exposes some multi-channel 16-bit files as 8-bit RGB.
                    # The original bit depth remains visible, but precision cannot
                    # be recovered after that decoder conversion.
                    precision_preserved = False

            if np.asarray(rgb).dtype == np.uint8 and (bit_depth <= 8 or not precision_preserved):
                # Avoid a full-image float64 intermediate for ordinary output
                # files. Analysis and display intentionally share this array.
                display = np.ascontiguousarray(rgb, dtype=np.uint8)
                rgb = display
            else:
                normalised = np.clip(rgb, 0.0, 255.0)
                display = np.ascontiguousarray(np.rint(normalised), dtype=np.uint8)
                # High-bit-depth inputs retain fractional 0–255 values in
                # float32 so ROI statistics do not collapse to 8-bit bins.
                rgb = np.ascontiguousarray(normalised, dtype=np.float32)
            if alpha is not None:
                alpha = (
                    np.ascontiguousarray(alpha, dtype=np.uint8)
                    if np.asarray(alpha).dtype == np.uint8 and bit_depth <= 8
                    else np.ascontiguousarray(np.clip(alpha, 0.0, 255.0), dtype=np.float32)
                )
            actual_height, actual_width = rgb.shape[:2]
            return ImageData(
                path=source_path.resolve(),
                width=actual_width,
                height=actual_height,
                bit_depth=bit_depth,
                source_mode=source_mode,
                rgb=rgb,
                display_rgb=display,
                alpha=alpha,
                orientation_applied=orientation not in (0, 1),
                original_dtype=original_dtype,
                precision_preserved=precision_preserved,
                histogram=histogram_rgb(display),
            )
    except ImageLoadError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        LOGGER.exception("Unable to load image %s", source_path)
        raise ImageLoadError(f"Pillow 无法解析图片“{source_path.name}”：{exc}") from exc


def _safe_ratio(numerator: float, denominator: float) -> Optional[float]:
    if abs(denominator) <= 1e-12:
        return None
    return numerator / denominator


def _normalised_rgb(rgb: Iterable[float]) -> Tuple[float, float, float]:
    values = np.asarray(tuple(rgb), dtype=np.float64)
    total = float(np.sum(values))
    if total <= 1e-12:
        return (0.0, 0.0, 0.0)
    result = values / total
    return tuple(float(value) for value in result)  # type: ignore[return-value]


def _channel_extrema(rgb: Iterable[float]) -> Tuple[str, str, float]:
    values = np.asarray(tuple(rgb), dtype=np.float64)
    maximum = float(np.max(values))
    minimum = float(np.min(values))
    max_names = [CHANNELS[index] for index, value in enumerate(values) if abs(float(value) - maximum) <= 1e-9]
    min_names = [CHANNELS[index] for index, value in enumerate(values) if abs(float(value) - minimum) <= 1e-9]
    return ("/".join(max_names), "/".join(min_names), maximum - minimum)


def is_near_neutral(rgb: Iterable[float], lab: Iterable[float], saturation: float) -> bool:
    rgb_values = np.asarray(tuple(rgb), dtype=np.float64)
    lab_values = np.asarray(tuple(lab), dtype=np.float64)
    channel_spread = float(np.max(rgb_values) - np.min(rgb_values))
    lab_chroma = math.hypot(float(lab_values[1]), float(lab_values[2]))
    return channel_spread <= 10.0 and saturation <= 0.065 and lab_chroma <= 6.0


def color_tendency(rgb: Iterable[float], lab: Iterable[float], saturation: float) -> str:
    rgb_values = tuple(float(value) for value in rgb)
    lab_values = tuple(float(value) for value in lab)
    near_neutral = is_near_neutral(rgb_values, lab_values, saturation)
    a_value, b_value = lab_values[1], lab_values[2]
    if near_neutral:
        if b_value > 1.5 or rgb_values[0] - rgb_values[2] > 3.0:
            return "接近中性，轻微暖倾向"
        if b_value < -1.5 or rgb_values[2] - rgb_values[0] > 3.0:
            return "接近中性，轻微冷倾向"
        return "接近中性"

    components = []
    if abs(a_value) >= 2.0:
        components.append("红" if a_value > 0 else "绿")
    if abs(b_value) >= 2.0:
        components.append("黄" if b_value > 0 else "蓝")
    if not components:
        return "颜色分量不明显"
    return "该区域以" + "、".join(components) + "分量为主"


def neutral_assessment(rgb: Iterable[float], lab: Iterable[float], hsv: Iterable[float]) -> str:
    rgb_values = np.asarray(tuple(rgb), dtype=np.float64)
    lab_values = np.asarray(tuple(lab), dtype=np.float64)
    hsv_values = np.asarray(tuple(hsv), dtype=np.float64)
    spread = float(np.max(rgb_values) - np.min(rgb_values))
    saturation = float(hsv_values[1])
    value = float(hsv_values[2])
    a_value, b_value = float(lab_values[1]), float(lab_values[2])

    if value <= 0.06:
        return "亮度过低，不做确定性偏色判断"
    if value >= 0.98 and (saturation > 0.08 or spread > 16.0):
        return "接近高光或剪切，不做确定性偏色判断"
    if spread <= 6.0 and saturation <= 0.04 and abs(a_value) <= 2.5 and abs(b_value) <= 3.0:
        return "基本中性"

    strength = "轻微" if max(abs(a_value), abs(b_value)) < 8.0 and saturation < 0.12 else "明显"
    if abs(b_value) >= abs(a_value) * 0.8:
        return f"{strength}偏暖" if b_value > 0 else f"{strength}偏冷"
    return f"{strength}偏红" if a_value > 0 else f"{strength}偏绿"


def pixel_metrics(image: ImageData, x: int, y: int) -> PixelMetrics:
    if not (0 <= x < image.width and 0 <= y < image.height):
        raise ROIError(f"像素坐标 ({x}, {y}) 超出 {image.width}×{image.height} 图片范围。")
    rgb = tuple(float(value) for value in image.rgb[y, x])
    hsv = tuple(float(value) for value in rgb_to_hsv(rgb))
    lab = tuple(float(value) for value in rgb_to_lab(rgb))
    maximum_channel, minimum_channel, spread = _channel_extrema(rgb)
    saturation = hsv[1]
    alpha = None if image.alpha is None else float(image.alpha[y, x])
    return PixelMetrics(
        x=x,
        y=y,
        rgb=rgb,  # type: ignore[arg-type]
        normalized_rgb=_normalised_rgb(rgb),
        channel_differences=(rgb[0] - rgb[1], rgb[0] - rgb[2], rgb[1] - rgb[2]),
        r_over_g=_safe_ratio(rgb[0], rgb[1]),
        b_over_g=_safe_ratio(rgb[2], rgb[1]),
        hsv=hsv,  # type: ignore[arg-type]
        lab=lab,  # type: ignore[arg-type]
        relative_luminance=float(relative_luminance(rgb)),
        maximum_channel=maximum_channel,
        minimum_channel=minimum_channel,
        maximum_channel_difference=spread,
        near_neutral=is_near_neutral(rgb, lab, saturation),
        color_tendency=color_tendency(rgb, lab, saturation),
        alpha=alpha,
    )


def _mean_perceptual_metrics(pixels: np.ndarray) -> Tuple[Tuple[float, float, float], Tuple[float, float, float], float]:
    flat = pixels.reshape(-1, 3)
    count = flat.shape[0]
    lab_sum = np.zeros(3, dtype=np.float64)
    saturation_sum = 0.0
    value_sum = 0.0
    hue_sin = 0.0
    hue_cos = 0.0
    hue_weight = 0.0
    luminance_sum = 0.0
    for start in range(0, count, COLOR_CONVERSION_CHUNK_PIXELS):
        chunk = flat[start : start + COLOR_CONVERSION_CHUNK_PIXELS]
        labs = rgb_to_lab(chunk)
        hsv = rgb_to_hsv(chunk)
        weights = hsv[:, 1]
        radians = np.deg2rad(hsv[:, 0])
        lab_sum += np.sum(labs, axis=0)
        saturation_sum += float(np.sum(hsv[:, 1]))
        value_sum += float(np.sum(hsv[:, 2]))
        hue_sin += float(np.sum(np.sin(radians) * weights))
        hue_cos += float(np.sum(np.cos(radians) * weights))
        hue_weight += float(np.sum(weights))
        luminance_sum += float(np.sum(relative_luminance(chunk)))
    hue = 0.0 if hue_weight <= 1e-12 else float(np.mod(np.rad2deg(math.atan2(hue_sin, hue_cos)), 360.0))
    return (
        tuple(float(value) for value in (lab_sum / count)),  # type: ignore[return-value]
        (hue, saturation_sum / count, value_sum / count),
        luminance_sum / count,
    )


def _uint8_channel_statistics(
    histogram: np.ndarray,
    count: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Derive exact uint8 channel statistics from one compact histogram."""

    levels = np.arange(256, dtype=np.float64)
    channel_histogram = np.asarray(histogram, dtype=np.int64)
    mean = channel_histogram @ levels / count
    second_moment = channel_histogram @ np.square(levels) / count
    standard_deviation = np.sqrt(np.maximum(0.0, second_moment - np.square(mean)))
    present = channel_histogram > 0
    minimum = np.argmax(present, axis=1).astype(np.float64)
    maximum = (255 - np.argmax(present[:, ::-1], axis=1)).astype(np.float64)
    lower_position = (count - 1) // 2
    upper_position = count // 2
    median = np.empty(3, dtype=np.float64)
    cumulative = np.cumsum(channel_histogram, axis=1)
    for channel in range(3):
        lower = np.searchsorted(cumulative[channel], lower_position, side="right")
        upper = np.searchsorted(cumulative[channel], upper_position, side="right")
        median[channel] = (float(lower) + float(upper)) / 2.0
    return mean, median, standard_deviation, minimum, maximum


def analyse_roi(image: ImageData, roi: ROI) -> ROIStatistics:
    clipped = roi.clipped(image.width, image.height)
    if clipped.width <= 0 or clipped.height <= 0:
        raise ROIError("ROI 为空或完全位于图片范围之外。")
    pixels = image.rgb[clipped.y : clipped.bottom, clipped.x : clipped.right]
    if pixels.size == 0:
        raise ROIError("ROI 没有可分析像素。")
    flat = pixels.reshape(-1, 3)
    if pixels.dtype == np.uint8:
        display_roi = pixels
        histogram = histogram_rgb(display_roi)
        mean, median, standard_deviation, minimum, maximum = _uint8_channel_statistics(
            histogram,
            flat.shape[0],
        )
    else:
        mean = np.mean(flat, axis=0, dtype=np.float64)
        median = np.median(flat, axis=0)
        standard_deviation = np.std(flat, axis=0, dtype=np.float64)
        minimum = np.min(flat, axis=0)
        maximum = np.max(flat, axis=0)
        display_roi = np.rint(np.clip(pixels, 0.0, 255.0)).astype(np.uint8)
        histogram = histogram_rgb(display_roi)
    lab_mean, hsv_mean, luminance = _mean_perceptual_metrics(pixels)
    maximum_channel, _minimum_channel, spread = _channel_extrema(mean)
    average_std = float(np.mean(standard_deviation))
    if average_std <= STABILITY_HIGH_STD:
        stability = "高"
    elif average_std <= STABILITY_MEDIUM_STD:
        stability = "中"
    else:
        stability = "低"
    clipped_ratio = float(np.mean(np.any(flat >= HIGHLIGHT_CLIP_VALUE, axis=1)))
    dark_ratio = float(np.mean(np.max(flat, axis=1) <= DARK_PIXEL_VALUE))
    saturation = float(hsv_mean[1])
    return ROIStatistics(
        roi=clipped,
        pixel_count=clipped.area,
        mean_rgb=tuple(float(value) for value in mean),  # type: ignore[arg-type]
        median_rgb=tuple(float(value) for value in median),  # type: ignore[arg-type]
        std_rgb=tuple(float(value) for value in standard_deviation),  # type: ignore[arg-type]
        min_rgb=tuple(float(value) for value in minimum),  # type: ignore[arg-type]
        max_rgb=tuple(float(value) for value in maximum),  # type: ignore[arg-type]
        r_over_g=_safe_ratio(float(mean[0]), float(mean[1])),
        b_over_g=_safe_ratio(float(mean[2]), float(mean[1])),
        normalized_rgb=_normalised_rgb(mean),
        hsv_mean=hsv_mean,
        lab_mean=lab_mean,
        relative_luminance=luminance,
        saturation=saturation,
        maximum_channel=maximum_channel,
        maximum_channel_difference=spread,
        clipped_ratio=clipped_ratio,
        dark_ratio=dark_ratio,
        stability=stability,
        near_neutral=is_near_neutral(mean, lab_mean, saturation),
        color_tendency=color_tendency(mean, lab_mean, saturation),
        neutral_assessment=neutral_assessment(mean, lab_mean, hsv_mean),
        histogram=histogram,
    )


def _percent_change(before: float, after: float) -> Optional[float]:
    if abs(before) <= 1e-12:
        return None
    return (after - before) / before * 100.0


def _optional_delta(before: Optional[float], after: Optional[float]) -> Optional[float]:
    if before is None or after is None:
        return None
    return after - before


def compare_statistics(
    before: ROIStatistics,
    after: ROIStatistics,
    *,
    reliable: bool,
    match_score: Optional[float] = None,
    manually_confirmed: bool = False,
) -> ComparisonResult:
    delta_rgb = tuple(after.mean_rgb[index] - before.mean_rgb[index] for index in range(3))
    delta_percent = tuple(_percent_change(before.mean_rgb[index], after.mean_rgb[index]) for index in range(3))
    hue_delta = (after.hsv_mean[0] - before.hsv_mean[0] + 180.0) % 360.0 - 180.0
    delta_hsv = (hue_delta, after.hsv_mean[1] - before.hsv_mean[1], after.hsv_mean[2] - before.hsv_mean[2])
    delta_lab = tuple(after.lab_mean[index] - before.lab_mean[index] for index in range(3))
    delta_luminance = after.relative_luminance - before.relative_luminance
    delta_saturation = after.saturation - before.saturation

    conclusions = []
    if not reliable:
        conclusions.append("当前 ROI 匹配置信度较低，两个区域可能不是同一物体位置。")
        conclusions.append("以下颜色变化数值仅供参考；请手动调整并确认对比图 ROI，暂不输出确定性颜色结论。")
    else:
        condition = "对比图 ROI 已由用户手动确认" if manually_confirmed else (
            f"ROI 匹配分数为 {(match_score or 0.0) * 100.0:.1f}%"
        )
        conclusions.append(f"{condition}；如果两个 ROI 属于同一物体区域，可按以下最终输出像素变化解读。")
        channel_parts = []
        for name, value in zip(CHANNELS, delta_percent):
            if value is not None and abs(value) >= 0.05:
                channel_parts.append(f"{name} 通道{'增加' if value > 0 else '降低'} {abs(value):.1f}%")
        if channel_parts:
            conclusions.append("，".join(channel_parts) + "。")
        if abs(delta_lab[2]) >= 0.5:
            conclusions.append(
                "Lab 中 b* 降低，区域的黄色方向分量减弱。"
                if delta_lab[2] < 0
                else "Lab 中 b* 增加，区域的黄色方向分量增强。"
            )
        if abs(delta_lab[1]) >= 0.5:
            conclusions.append(
                "Lab 中 a* 降低，区域的红色方向分量减弱。"
                if delta_lab[1] < 0
                else "Lab 中 a* 增加，区域的红色方向分量增强。"
            )
        brightness_word = "较小" if abs(delta_luminance) < 0.02 else ("增加" if delta_luminance > 0 else "降低")
        saturation_word = "变化较小" if abs(delta_saturation) < 0.02 else ("上升" if delta_saturation > 0 else "下降")
        conclusions.append(f"相对亮度变化{brightness_word}，饱和度{saturation_word}。")
        conclusions.append("这些指标只描述最终 sRGB 图片，不直接证明 RAW、AWB Gain、CCM、CV 或其他 ISP 模块的具体责任。")

    return ComparisonResult(
        before=before,
        after=after,
        delta_rgb=delta_rgb,  # type: ignore[arg-type]
        delta_rgb_percent=delta_percent,  # type: ignore[arg-type]
        delta_r_over_g=_optional_delta(before.r_over_g, after.r_over_g),
        delta_b_over_g=_optional_delta(before.b_over_g, after.b_over_g),
        delta_hsv=delta_hsv,
        delta_lab=delta_lab,  # type: ignore[arg-type]
        delta_luminance=delta_luminance,
        delta_saturation=delta_saturation,
        conclusions=tuple(conclusions),
        reliable=reliable,
    )
