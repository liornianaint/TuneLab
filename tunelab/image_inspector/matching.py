"""Nearby ROI template matching with an optional OpenCV fast path."""

from __future__ import annotations

import logging
import math
from typing import Optional, Tuple

import numpy as np

from .constants import MATCH_CONFIDENCE_HIGH, MATCH_CONFIDENCE_RELIABLE, MAX_MATCH_TEMPLATE_PIXELS
from .types import MatchResult, ROI


LOGGER = logging.getLogger(__name__)

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - availability varies by installation
    cv2 = None


class MatchingError(ValueError):
    pass


def opencv_available() -> bool:
    return cv2 is not None


def confidence_for_score(score: float, reliable_threshold: float = MATCH_CONFIDENCE_RELIABLE) -> str:
    # Confidence labels stay comparable between exports.  The separately
    # configurable reliable_threshold is a conclusion gate, not a relabelling
    # of the documented 0.80/0.92 bands.
    _ = reliable_threshold
    if score >= MATCH_CONFIDENCE_HIGH:
        return "高"
    if score >= MATCH_CONFIDENCE_RELIABLE:
        return "中"
    return "低"


def _to_gray(rgb: np.ndarray) -> np.ndarray:
    values = np.asarray(rgb, dtype=np.float32)
    if values.ndim != 3 or values.shape[-1] < 3:
        raise MatchingError("匹配输入必须是 H×W×3 RGB 数组。")
    return values[..., 0] * 0.2126 + values[..., 1] * 0.7152 + values[..., 2] * 0.0722


def _resize_gray(gray: np.ndarray, width: int, height: int) -> np.ndarray:
    if gray.shape == (height, width):
        return np.asarray(gray, dtype=np.float32)
    if width <= 0 or height <= 0:
        raise MatchingError("映射后的 ROI 尺寸无效。")
    if cv2 is not None:
        interpolation = cv2.INTER_AREA if width < gray.shape[1] or height < gray.shape[0] else cv2.INTER_LINEAR
        return cv2.resize(gray, (width, height), interpolation=interpolation).astype(np.float32)
    try:
        from PIL import Image
    except ImportError as exc:
        raise MatchingError("不同尺寸图片的 ROI 匹配需要 Pillow。") from exc
    image = Image.fromarray(np.asarray(gray, dtype=np.float32), mode="F")
    return np.asarray(image.resize((width, height), Image.Resampling.BILINEAR), dtype=np.float32)


def _integral_window_sum(values: np.ndarray, height: int, width: int) -> np.ndarray:
    integral = np.pad(
        np.cumsum(np.cumsum(values.astype(np.float64), axis=0), axis=1),
        ((1, 0), (1, 0)),
        mode="constant",
    )
    return (
        integral[height:, width:]
        - integral[:-height, width:]
        - integral[height:, :-width]
        + integral[:-height, :-width]
    )


def _ncc_map_fft(search: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Equivalent to TM_CCOEFF_NORMED, implemented without OpenCV."""

    search_values = np.asarray(search, dtype=np.float64)
    template_values = np.asarray(template, dtype=np.float64)
    template_height, template_width = template_values.shape
    search_height, search_width = search_values.shape
    if search_height < template_height or search_width < template_width:
        raise MatchingError("搜索区域小于模板 ROI。")
    centred_template = template_values - float(np.mean(template_values))
    template_energy = float(np.sum(centred_template * centred_template))
    output_shape = (search_height + template_height - 1, search_width + template_width - 1)
    correlation = np.fft.irfftn(
        np.fft.rfftn(search_values, s=output_shape, axes=(0, 1))
        * np.fft.rfftn(centred_template[::-1, ::-1], s=output_shape, axes=(0, 1)),
        s=output_shape,
        axes=(0, 1),
    )
    numerator = correlation[
        template_height - 1 : search_height,
        template_width - 1 : search_width,
    ]
    count = float(template_height * template_width)
    sums = _integral_window_sum(search_values, template_height, template_width)
    square_sums = _integral_window_sum(search_values * search_values, template_height, template_width)
    patch_energy = np.maximum(square_sums - sums * sums / count, 0.0)
    denominator = np.sqrt(patch_energy * template_energy)
    return np.divide(
        numerator,
        denominator,
        out=np.full_like(numerator, -1.0),
        where=denominator > 1e-12,
    )


def _direct_ncc(template: np.ndarray, patch: np.ndarray, stride: int = 1) -> float:
    template_values = np.asarray(template[::stride, ::stride], dtype=np.float64)
    patch_values = np.asarray(patch[::stride, ::stride], dtype=np.float64)
    if template_values.shape != patch_values.shape or template_values.size == 0:
        return -1.0
    template_values = template_values - float(np.mean(template_values))
    patch_values = patch_values - float(np.mean(patch_values))
    denominator = math.sqrt(
        float(np.sum(template_values * template_values))
        * float(np.sum(patch_values * patch_values))
    )
    if denominator <= 1e-12:
        return -1.0
    return float(np.sum(template_values * patch_values) / denominator)


def _expected_after_roi(before_shape: Tuple[int, int], after_shape: Tuple[int, int], roi: ROI) -> Tuple[ROI, float, float]:
    before_height, before_width = before_shape
    after_height, after_width = after_shape
    scale_x = after_width / float(before_width)
    scale_y = after_height / float(before_height)
    width = min(after_width, max(1, int(round(roi.width * scale_x))))
    height = min(after_height, max(1, int(round(roi.height * scale_y))))
    centre_x = (roi.x + roi.width / 2.0) * scale_x
    centre_y = (roi.y + roi.height / 2.0) * scale_y
    x = max(0, min(after_width - width, int(round(centre_x - width / 2.0))))
    y = max(0, min(after_height - height, int(round(centre_y - height / 2.0))))
    return ROI(x, y, width, height, roi.name), scale_x, scale_y


def _low_result(
    before_roi: ROI,
    expected: ROI,
    search_bounds: ROI,
    method: str,
    warning: str,
) -> MatchResult:
    return MatchResult(
        before_roi=before_roi,
        after_roi=expected,
        expected_roi=expected,
        search_bounds=search_bounds,
        score=0.0,
        confidence="低",
        reliable=False,
        method=method,
        warning=warning,
    )


def match_roi(
    before_rgb: np.ndarray,
    after_rgb: np.ndarray,
    before_roi: ROI,
    *,
    search_range: int = 100,
    reliable_threshold: float = MATCH_CONFIDENCE_RELIABLE,
) -> MatchResult:
    """Find the most correlated grayscale ROI near the relative mapped position."""

    if search_range < 0:
        raise MatchingError("搜索范围不能为负数。")
    if not 0.0 <= reliable_threshold <= 1.0:
        raise MatchingError("匹配阈值必须位于 0~1。")
    before_values = np.asarray(before_rgb)
    after_values = np.asarray(after_rgb)
    if before_values.ndim != 3 or after_values.ndim != 3:
        raise MatchingError("基准图或目标图的数组维度无效。")
    before_height, before_width = before_values.shape[:2]
    after_height, after_width = after_values.shape[:2]
    roi = before_roi.normalized()
    if roi.width <= 0 or roi.height <= 0:
        raise MatchingError("基准图 ROI 为空。")
    if roi != roi.clipped(before_width, before_height):
        raise MatchingError("基准图 ROI 超出图片边界。")

    expected, scale_x, scale_y = _expected_after_roi(
        (before_height, before_width),
        (after_height, after_width),
        roi,
    )
    radius_x = max(0, int(round(search_range * scale_x)))
    radius_y = max(0, int(round(search_range * scale_y)))
    candidate_left = max(0, expected.x - radius_x)
    candidate_top = max(0, expected.y - radius_y)
    candidate_right = min(after_width - expected.width, expected.x + radius_x)
    candidate_bottom = min(after_height - expected.height, expected.y + radius_y)
    if candidate_right < candidate_left or candidate_bottom < candidate_top:
        raise MatchingError("当前图像搜索区域小于映射后的模板 ROI。")
    search_bounds = ROI(
        candidate_left,
        candidate_top,
        candidate_right - candidate_left + expected.width,
        candidate_bottom - candidate_top + expected.height,
        "搜索区域",
    )

    before_gray = _to_gray(before_values[roi.y : roi.bottom, roi.x : roi.right])
    template = _resize_gray(before_gray, expected.width, expected.height)
    search = _to_gray(
        after_values[
            search_bounds.y : search_bounds.bottom,
            search_bounds.x : search_bounds.right,
        ]
    )
    full_template = template
    full_search = search
    method = "OpenCV 灰度 NCC" if cv2 is not None else "NumPy FFT 灰度 NCC（OpenCV 未安装）"
    reduction = max(1, int(math.ceil(math.sqrt(template.size / MAX_MATCH_TEMPLATE_PIXELS))))
    if reduction > 1:
        template = _resize_gray(
            template,
            max(1, int(round(template.shape[1] / reduction))),
            max(1, int(round(template.shape[0] / reduction))),
        )
        search = _resize_gray(
            search,
            max(template.shape[1], int(round(search.shape[1] / reduction))),
            max(template.shape[0], int(round(search.shape[0] / reduction))),
        )
        method += f" · 1/{reduction} 降采样"
    if float(np.std(template)) < 1.0:
        return _low_result(
            roi,
            expected,
            search_bounds,
            method,
            "基准图 ROI 缺少可辨识纹理，无法可靠自动匹配；请手动确认目标图 ROI。",
        )

    if cv2 is not None:
        template_input = cv2.GaussianBlur(template.astype(np.float32), (3, 3), 0.6)
        search_input = cv2.GaussianBlur(search.astype(np.float32), (3, 3), 0.6)
        scores = cv2.matchTemplate(search_input, template_input, cv2.TM_CCOEFF_NORMED)
    else:
        scores = _ncc_map_fft(search, template)
    if scores.size == 0 or not np.any(np.isfinite(scores)):
        return _low_result(roi, expected, search_bounds, method, "模板匹配没有产生有效分数。")
    safe_scores = np.where(np.isfinite(scores), scores, -1.0)
    best_y, best_x = np.unravel_index(int(np.argmax(safe_scores)), safe_scores.shape)
    candidate_span_x = candidate_right - candidate_left
    candidate_span_y = candidate_bottom - candidate_top
    mapped_x = 0 if safe_scores.shape[1] <= 1 else int(round(best_x / (safe_scores.shape[1] - 1) * candidate_span_x))
    mapped_y = 0 if safe_scores.shape[0] <= 1 else int(round(best_y / (safe_scores.shape[0] - 1) * candidate_span_y))
    score = float(np.clip(safe_scores[best_y, best_x], 0.0, 1.0))
    if reduction > 1:
        if reduction <= 4:
            offsets = tuple(range(-reduction, reduction + 1))
        else:
            offsets = tuple(sorted(set((-reduction, -reduction // 2, -2, -1, 0, 1, 2, reduction // 2, reduction))))
        best_refined = (score, mapped_x, mapped_y)
        for offset_y in offsets:
            candidate_y = max(0, min(candidate_span_y, mapped_y + offset_y))
            for offset_x in offsets:
                candidate_x = max(0, min(candidate_span_x, mapped_x + offset_x))
                patch = full_search[
                    candidate_y : candidate_y + expected.height,
                    candidate_x : candidate_x + expected.width,
                ]
                candidate_score = _direct_ncc(full_template, patch, stride=reduction)
                if candidate_score > best_refined[0]:
                    best_refined = (candidate_score, candidate_x, candidate_y)
        score = float(np.clip(best_refined[0], 0.0, 1.0))
        mapped_x, mapped_y = best_refined[1], best_refined[2]
        method += " · 原图邻域精修"
    confidence = confidence_for_score(score, reliable_threshold)
    reliable = score >= reliable_threshold
    warning = ""
    if not reliable:
        warning = (
            "当前 ROI 匹配置信度较低，两个区域可能不是同一物体位置。"
            "以下颜色变化仅供参考，请手动确认当前图像 ROI。"
        )
    return MatchResult(
        before_roi=roi,
        after_roi=ROI(
            search_bounds.x + mapped_x,
            search_bounds.y + mapped_y,
            expected.width,
            expected.height,
            roi.name,
        ),
        expected_roi=expected,
        search_bounds=search_bounds,
        score=score,
        confidence=confidence,
        reliable=reliable,
        method=method,
        warning=warning,
    )


def confirm_match(result: MatchResult, roi: Optional[ROI] = None) -> MatchResult:
    confirmed_roi = roi or result.after_roi
    return MatchResult(
        before_roi=result.before_roi,
        after_roi=confirmed_roi,
        expected_roi=result.expected_roi,
        search_bounds=result.search_bounds,
        score=result.score,
        confidence="手动确认",
        reliable=True,
        method=result.method,
        warning="",
        manually_confirmed=True,
    )


def manual_match(before_roi: ROI, after_roi: ROI) -> MatchResult:
    """Create a reliable gate after the user explicitly adjusts a target ROI."""

    return MatchResult(
        before_roi=before_roi,
        after_roi=after_roi,
        expected_roi=after_roi,
        search_bounds=after_roi,
        score=0.0,
        confidence="手动确认",
        reliable=True,
        method="用户手动选择",
        warning="",
        manually_confirmed=True,
    )
