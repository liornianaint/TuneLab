"""UTF-8 BOM CSV export for one-to-four-image ROI results."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .types import ComparisonResult, ImageData, MatchResult, ROIStatistics


CSV_FIELDS = [
    "image_role",
    "image_filename",
    "image_path",
    "image_width",
    "image_height",
    "bit_depth",
    "source_mode",
    "precision_preserved",
    "roi_name",
    "roi_x",
    "roi_y",
    "roi_width",
    "roi_height",
    "pixel_count",
    "match_score",
    "match_confidence",
    "mean_r",
    "mean_g",
    "mean_b",
    "median_r",
    "median_g",
    "median_b",
    "std_r",
    "std_g",
    "std_b",
    "min_r",
    "min_g",
    "min_b",
    "max_r",
    "max_g",
    "max_b",
    "r_over_g",
    "b_over_g",
    "normalized_r",
    "normalized_g",
    "normalized_b",
    "h",
    "s",
    "v",
    "lab_l",
    "lab_a",
    "lab_b",
    "relative_luminance",
    "clipped_ratio",
    "dark_ratio",
    "stability",
    "color_tendency",
    "neutral_assessment",
    "delta_r",
    "delta_g",
    "delta_b",
    "delta_r_percent",
    "delta_g_percent",
    "delta_b_percent",
    "delta_normalized_r",
    "delta_normalized_g",
    "delta_normalized_b",
    "delta_r_over_g",
    "delta_b_over_g",
    "delta_h",
    "delta_s",
    "delta_v",
    "delta_lab_l",
    "delta_lab_a",
    "delta_lab_b",
    "delta_luminance",
    "delta_saturation",
    "notes",
]


def _value(value: Optional[float]) -> Any:
    return "" if value is None else value


def _base_row(
    role: str,
    image: ImageData,
    stats: ROIStatistics,
    match: Optional[MatchResult],
    *,
    include_full_path: bool,
) -> Dict[str, Any]:
    roi = stats.roi
    notes = [f"区域稳定性={stats.stability}（仅表示区域内部颜色一致性，不代表匹配准确度）"]
    if match is not None and not match.reliable:
        notes.append(match.warning or "低置信度：请手动确认当前图像 ROI。")
    notes.append("按 sRGB 最终输出像素分析；不等同于 RAW、AWB Gain 或 ISP 中间节点数据")
    return {
        "image_role": role,
        "image_filename": image.filename,
        "image_path": str(image.path) if include_full_path else image.filename,
        "image_width": image.width,
        "image_height": image.height,
        "bit_depth": image.bit_depth,
        "source_mode": image.source_mode,
        "precision_preserved": image.precision_preserved,
        "roi_name": roi.name,
        "roi_x": roi.x,
        "roi_y": roi.y,
        "roi_width": roi.width,
        "roi_height": roi.height,
        "pixel_count": stats.pixel_count,
        "match_score": "" if match is None else match.score,
        "match_confidence": "" if match is None else match.confidence,
        "mean_r": stats.mean_rgb[0],
        "mean_g": stats.mean_rgb[1],
        "mean_b": stats.mean_rgb[2],
        "median_r": stats.median_rgb[0],
        "median_g": stats.median_rgb[1],
        "median_b": stats.median_rgb[2],
        "std_r": stats.std_rgb[0],
        "std_g": stats.std_rgb[1],
        "std_b": stats.std_rgb[2],
        "min_r": stats.min_rgb[0],
        "min_g": stats.min_rgb[1],
        "min_b": stats.min_rgb[2],
        "max_r": stats.max_rgb[0],
        "max_g": stats.max_rgb[1],
        "max_b": stats.max_rgb[2],
        "r_over_g": _value(stats.r_over_g),
        "b_over_g": _value(stats.b_over_g),
        "normalized_r": stats.normalized_rgb[0],
        "normalized_g": stats.normalized_rgb[1],
        "normalized_b": stats.normalized_rgb[2],
        "h": stats.hsv_mean[0],
        "s": stats.hsv_mean[1],
        "v": stats.hsv_mean[2],
        "lab_l": stats.lab_mean[0],
        "lab_a": stats.lab_mean[1],
        "lab_b": stats.lab_mean[2],
        "relative_luminance": stats.relative_luminance,
        "clipped_ratio": stats.clipped_ratio,
        "dark_ratio": stats.dark_ratio,
        "stability": stats.stability,
        "color_tendency": stats.color_tendency,
        "neutral_assessment": stats.neutral_assessment,
        "notes": "；".join(notes),
    }


def _add_delta(row: Dict[str, Any], comparison: ComparisonResult) -> None:
    row.update(
        {
            "delta_r": comparison.delta_rgb[0],
            "delta_g": comparison.delta_rgb[1],
            "delta_b": comparison.delta_rgb[2],
            "delta_r_percent": _value(comparison.delta_rgb_percent[0]),
            "delta_g_percent": _value(comparison.delta_rgb_percent[1]),
            "delta_b_percent": _value(comparison.delta_rgb_percent[2]),
            "delta_normalized_r": comparison.delta_normalized_rgb[0],
            "delta_normalized_g": comparison.delta_normalized_rgb[1],
            "delta_normalized_b": comparison.delta_normalized_rgb[2],
            "delta_r_over_g": _value(comparison.delta_r_over_g),
            "delta_b_over_g": _value(comparison.delta_b_over_g),
            "delta_h": comparison.delta_hsv[0],
            "delta_s": comparison.delta_hsv[1],
            "delta_v": comparison.delta_hsv[2],
            "delta_lab_l": comparison.delta_lab[0],
            "delta_lab_a": comparison.delta_lab[1],
            "delta_lab_b": comparison.delta_lab[2],
            "delta_luminance": comparison.delta_luminance,
            "delta_saturation": comparison.delta_saturation,
        }
    )


def build_export_rows(
    before_image: ImageData,
    before_stats: ROIStatistics,
    *,
    after_image: Optional[ImageData] = None,
    after_stats: Optional[ROIStatistics] = None,
    match: Optional[MatchResult] = None,
    comparison: Optional[ComparisonResult] = None,
    include_full_path: bool = True,
) -> List[Dict[str, Any]]:
    dual = after_image is not None and after_stats is not None
    rows = [
        _base_row(
            "before" if dual else "single",
            before_image,
            before_stats,
            match,
            include_full_path=include_full_path,
        )
    ]
    if dual:
        assert after_image is not None and after_stats is not None
        after_row = _base_row("after", after_image, after_stats, match, include_full_path=include_full_path)
        if comparison is not None:
            _add_delta(after_row, comparison)
            after_row["notes"] += "；" + " ".join(comparison.conclusions)
        rows.append(after_row)
    return rows


def build_multi_export_rows(
    images: Sequence[ImageData],
    statistics: Sequence[ROIStatistics],
    *,
    matches: Sequence[Optional[MatchResult]] = (),
    comparisons: Sequence[Optional[ComparisonResult]] = (),
    include_full_path: bool = True,
) -> List[Dict[str, Any]]:
    """Build one reference row plus up to three comparison rows."""

    if not 1 <= len(images) <= 4:
        raise ValueError("多图导出需要 1–4 张图片。")
    if len(images) != len(statistics):
        raise ValueError("图片数量与 ROI 统计数量不一致。")
    rows: List[Dict[str, Any]] = []
    reference_role = "single" if len(images) == 1 else "reference"
    rows.append(_base_row(reference_role, images[0], statistics[0], None, include_full_path=include_full_path))
    for index in range(1, len(images)):
        match = matches[index - 1] if index - 1 < len(matches) else None
        comparison = comparisons[index - 1] if index - 1 < len(comparisons) else None
        row = _base_row(
            f"comparison_{index + 1}",
            images[index],
            statistics[index],
            match,
            include_full_path=include_full_path,
        )
        if comparison is not None:
            _add_delta(row, comparison)
            row["notes"] += "；" + " ".join(comparison.conclusions)
        rows.append(row)
    return rows


def export_csv(
    path: Union[str, Path],
    before_image: ImageData,
    before_stats: ROIStatistics,
    *,
    after_image: Optional[ImageData] = None,
    after_stats: Optional[ROIStatistics] = None,
    match: Optional[MatchResult] = None,
    comparison: Optional[ComparisonResult] = None,
    include_full_path: bool = True,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = build_export_rows(
        before_image,
        before_stats,
        after_image=after_image,
        after_stats=after_stats,
        match=match,
        comparison=comparison,
        include_full_path=include_full_path,
    )
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return destination


def export_multi_csv(
    path: Union[str, Path],
    images: Sequence[ImageData],
    statistics: Sequence[ROIStatistics],
    *,
    matches: Sequence[Optional[MatchResult]] = (),
    comparisons: Sequence[Optional[ComparisonResult]] = (),
    include_full_path: bool = True,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = build_multi_export_rows(
        images,
        statistics,
        matches=matches,
        comparisons=comparisons,
        include_full_path=include_full_path,
    )
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return destination
