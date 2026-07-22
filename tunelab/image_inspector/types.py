from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple


Float3 = Tuple[float, float, float]
OptionalFloat3 = Tuple[Optional[float], Optional[float], Optional[float]]


@dataclass(frozen=True)
class ROI:
    """Half-open original-image rectangle: ``[x, x+width) × [y, y+height)``."""

    x: int
    y: int
    width: int
    height: int
    name: str = "ROI 1"

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def normalized(self) -> "ROI":
        x = self.x if self.width >= 0 else self.x + self.width
        y = self.y if self.height >= 0 else self.y + self.height
        return ROI(x, y, abs(self.width), abs(self.height), self.name)

    def clipped(self, image_width: int, image_height: int) -> "ROI":
        roi = self.normalized()
        left = max(0, min(image_width, roi.x))
        top = max(0, min(image_height, roi.y))
        right = max(left, min(image_width, roi.right))
        bottom = max(top, min(image_height, roi.bottom))
        return ROI(left, top, right - left, bottom - top, roi.name)


@dataclass
class ImageData:
    path: Path
    width: int
    height: int
    bit_depth: int
    source_mode: str
    rgb: Any
    display_rgb: Any
    alpha: Optional[Any] = None
    orientation_applied: bool = False
    original_dtype: str = "uint8"
    precision_preserved: bool = True
    histogram: Optional[Any] = None
    luminance_histogram: Optional[Any] = None
    exif: Tuple[Tuple[str, str], ...] = ()

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class PixelMetrics:
    x: int
    y: int
    rgb: Float3
    normalized_rgb: Float3
    channel_differences: Float3
    r_over_g: Optional[float]
    b_over_g: Optional[float]
    hsv: Float3
    lab: Float3
    display_luminance: float
    relative_luminance: float
    maximum_channel: str
    minimum_channel: str
    maximum_channel_difference: float
    near_neutral: bool
    color_tendency: str
    alpha: Optional[float] = None


@dataclass(frozen=True)
class ROIStatistics:
    roi: ROI
    pixel_count: int
    mean_rgb: Float3
    median_rgb: Float3
    std_rgb: Float3
    min_rgb: Float3
    max_rgb: Float3
    r_over_g: Optional[float]
    b_over_g: Optional[float]
    normalized_rgb: Float3
    hsv_mean: Float3
    lab_mean: Float3
    display_luminance: float
    relative_luminance: float
    saturation: float
    maximum_channel: str
    maximum_channel_difference: float
    clipped_ratio: float
    dark_ratio: float
    stability: str
    near_neutral: bool
    color_tendency: str
    neutral_assessment: str
    histogram: Any
    luminance_histogram: Any


@dataclass(frozen=True)
class MatchResult:
    before_roi: ROI
    after_roi: ROI
    expected_roi: ROI
    search_bounds: ROI
    score: float
    confidence: str
    reliable: bool
    method: str
    warning: str = ""
    manually_confirmed: bool = False


@dataclass(frozen=True)
class ComparisonResult:
    before: ROIStatistics
    after: ROIStatistics
    delta_rgb: Float3
    delta_rgb_percent: OptionalFloat3
    delta_normalized_rgb: Float3
    delta_r_over_g: Optional[float]
    delta_b_over_g: Optional[float]
    delta_hsv: Float3
    delta_lab: Float3
    delta_display_luminance: float
    delta_luminance: float
    delta_saturation: float
    conclusions: Tuple[str, ...]
    reliable: bool
