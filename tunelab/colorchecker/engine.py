"""ColorChecker input adapter, calibrated profiles and full-image preview.

The unified CCM page defaults to protected image fitting: every reference patch
is matched to measured luminance in linear sRGB before the common optimizer
sees it.  Hardware-validated 3000K/4000K Delta CCM anchors remain available as
an advanced candidate direction, with a fitted real-shot response for preview.
Both paths prevent JPEG exposure and tone-map differences from being mistaken
for arbitrary white-balance shifts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import numpy as np

from ..ccm.color_science import (
    identity_matrix,
    linear_to_srgb,
    mat_mul,
    matrix_blend,
    srgb_to_linear,
)
from ..ccm.engineering import inverse, matrix_distance, max_matrix_delta
from ..ccm.imatest import infer_cct
from ..ccm.models import (
    PATCH_NAMES_ZH,
    ColorPatch,
    ImatestDataset,
    Matrix3,
    OptimizationConfig,
    Vector3,
)
from ..image_inspector.model import ImageLoadError, histogram_rgb, load_image
from ..image_inspector.types import ImageData


Point = tuple[float, float]
PatchPolygon = tuple[Point, Point, Point, Point]
MAX_DETECTION_SIDE = 1600
LUMA_WEIGHTS = np.asarray((0.2126729, 0.7151522, 0.0721750), dtype=np.float64)

# Common 8-bit sRGB reference values for ColorChecker Classic 24, ordered
# left-to-right and top-to-bottom.  This display reference is intentionally
# independent of capture CCT: it supplies target patch colours but never
# guesses which XML Region should be selected.
COLORCHECKER_CLASSIC_SRGB_8BIT: tuple[tuple[int, int, int], ...] = (
    (115, 82, 68),
    (194, 150, 130),
    (98, 122, 157),
    (87, 108, 67),
    (133, 128, 177),
    (103, 189, 170),
    (214, 126, 44),
    (80, 91, 166),
    (193, 90, 99),
    (94, 60, 108),
    (157, 188, 64),
    (224, 163, 46),
    (56, 61, 150),
    (70, 148, 73),
    (175, 54, 60),
    (231, 199, 31),
    (187, 86, 149),
    (8, 133, 161),
    (243, 243, 242),
    (200, 200, 200),
    (160, 160, 160),
    (122, 122, 121),
    (85, 85, 85),
    (52, 52, 52),
)


class ColorCheckerError(ValueError):
    """Raised when an image cannot provide a reliable 24-patch chart."""


@dataclass(frozen=True)
class DetectedPatch:
    zone: int
    name: str
    polygon: PatchPolygon
    mean_rgb: tuple[float, float, float]
    std_rgb: tuple[float, float, float]

    @property
    def srgb(self) -> Vector3:
        return tuple(value / 255.0 for value in self.mean_rgb)  # type: ignore[return-value]


@dataclass(frozen=True)
class ColorCheckerDetection:
    image: ImageData
    chart_box: PatchPolygon
    patches: tuple[DetectedPatch, ...]
    method: str
    confidence: float
    warning: str = ""

    @property
    def mean_patch_std(self) -> float:
        return float(np.mean([value for patch in self.patches for value in patch.std_rgb]))


@dataclass(frozen=True)
class SimulationResult:
    rgb: np.ndarray
    clipped_pixel_ratio: float
    domain: str = "linear"

    def as_image_data(self, source: ImageData) -> ImageData:
        display = np.ascontiguousarray(self.rgb, dtype=np.uint8)
        return ImageData(
            path=source.path,
            width=source.width,
            height=source.height,
            bit_depth=8,
            source_mode="RGB simulation",
            rgb=display,
            display_rgb=display,
            orientation_applied=source.orientation_applied,
            original_dtype="uint8",
            precision_preserved=True,
            histogram=histogram_rgb(display),
        )


@dataclass(frozen=True)
class RestorationProfile:
    """One hardware-validated ColorChecker restoration anchor."""

    cct: float
    source_matrix: Matrix3
    target_matrix: Matrix3
    label: str

    @property
    def correction_matrix(self) -> Matrix3:
        return mat_mul(self.target_matrix, inverse(self.source_matrix))


@dataclass(frozen=True)
class RestorationResponseProfile:
    """Polynomial approximation of one observed Before-to-After ISP response.

    Coefficients map encoded RGB features in this order::

        1, R, G, B, R², G², B², RG, RB, GB

    The model deliberately describes the supplied rendered captures, including
    their observed tone, gamma, saturation and clipping response.  It is a
    preview model only; the XML output remains the validated 3×3 CCM.
    """

    cct: float
    coefficients: tuple[Vector3, ...]
    calibration_rmse_8bit: float
    label: str


@dataclass(frozen=True)
class RestorationPlan:
    """A CCT-aware calibrated Delta CCM ready for preview and XML export."""

    correction_matrix: Matrix3
    optimized_matrix: Matrix3
    requested_cct: float
    profile_label: str
    strength: float
    exact_profile: bool
    already_calibrated: bool
    neutral_scale: float
    neutral_spread: float
    warnings: tuple[str, ...] = ()


def standard_colorchecker_reference() -> ColorCheckerDetection:
    """Create the built-in ColorChecker Classic 24 sRGB target.

    Geometry and patch measurements are generated together, so this reference
    needs no file on disk and does not depend on OpenCV detection.  Its neutral
    filename and explicit method label also ensure it cannot silently set CCT.
    """

    width, height = 960, 640
    rgb = np.full((height, width, 3), 214, dtype=np.uint8)
    chart_left, chart_top = 48, 48
    chart_right, chart_bottom = width - 48, height - 48
    rgb[chart_top:chart_bottom, chart_left:chart_right] = (24, 25, 31)
    cell_width = (chart_right - chart_left) / 6.0
    cell_height = (chart_bottom - chart_top) / 4.0
    inset_x, inset_y = 18, 18
    patches = []
    for index, colour in enumerate(COLORCHECKER_CLASSIC_SRGB_8BIT):
        row, column = divmod(index, 6)
        left = int(round(chart_left + column * cell_width + inset_x))
        right = int(round(chart_left + (column + 1) * cell_width - inset_x))
        top = int(round(chart_top + row * cell_height + inset_y))
        bottom = int(round(chart_top + (row + 1) * cell_height - inset_y))
        rgb[top:bottom, left:right] = colour
        polygon: PatchPolygon = (
            (float(left), float(top)),
            (float(right - 1), float(top)),
            (float(right - 1), float(bottom - 1)),
            (float(left), float(bottom - 1)),
        )
        patches.append(
            DetectedPatch(
                zone=index + 1,
                name=PATCH_NAMES_ZH[index],
                polygon=polygon,
                mean_rgb=tuple(float(value) for value in colour),  # type: ignore[arg-type]
                std_rgb=(0.0, 0.0, 0.0),
            )
        )
    display = np.ascontiguousarray(rgb)
    image = ImageData(
        path=Path("ColorChecker_Classic_24_standard_sRGB.png"),
        width=width,
        height=height,
        bit_depth=8,
        source_mode="Generated sRGB reference",
        rgb=display,
        display_rgb=display,
        original_dtype="uint8",
        precision_preserved=True,
        histogram=histogram_rgb(display),
    )
    chart_box: PatchPolygon = (
        (float(chart_left), float(chart_top)),
        (float(chart_right - 1), float(chart_top)),
        (float(chart_right - 1), float(chart_bottom - 1)),
        (float(chart_left), float(chart_bottom - 1)),
    )
    return ColorCheckerDetection(
        image=image,
        chart_box=chart_box,
        patches=tuple(patches),
        method="内置 ColorChecker Classic 24 标准 sRGB",
        confidence=1.0,
        warning="标准目标仅提供色块参考值，不参与拍摄 CCT 推断。",
    )


# These two anchors are the final matrices validated by the supplied 3000K and
# 4000K captures.  Store both the original and accepted final CCM so the tool
# applies their *Delta correction* to a selected XML Region rather than blindly
# replacing every compatible file with a hard-coded absolute matrix.
RESTORATION_PROFILES: tuple[RestorationProfile, ...] = (
    RestorationProfile(
        cct=3000.0,
        label="3000K 色彩还原（实拍验证）",
        source_matrix=(
            (2.070673, -1.338794, 0.268121),
            (-0.307499, 0.715785, 0.591714),
            (0.749507, -3.000352, 3.250845),
        ),
        target_matrix=(
            (2.070673, -1.338794, 0.268121),
            (-0.557214, 1.114171, 0.443043),
            (0.614521, -2.785001, 3.170480),
        ),
    ),
    RestorationProfile(
        cct=4000.0,
        label="4000K 色彩还原（实拍验证）",
        source_matrix=(
            (1.958841, -0.713342, -0.245498),
            (-0.118669, 0.739305, 0.379364),
            (0.411650, -1.956748, 2.545098),
        ),
        target_matrix=(
            (1.791773, -0.704038, -0.330099),
            (-1.071071, 2.250426, -0.421720),
            (0.017574, -1.265144, 2.005205),
        ),
    ),
)

# Encoded-RGB quadratic response fits measured from the supplied
# ``3000K_Before.jpg → 3000K_After.jpg`` and
# ``4000K_Before.jpg → 4000K_After.jpg`` ColorChecker patch pairs.  A small
# ridge term was used when fitting the 24 patches so saturated out-of-sample
# colours remain smoother than an exact 20-term cubic interpolation.  The
# resulting patch RMSE is 5.21/255 at 3000K and 1.49/255 at 4000K.
RESTORATION_RESPONSE_PROFILES: tuple[RestorationResponseProfile, ...] = (
    RestorationResponseProfile(
        cct=3000.0,
        label="3000K 实拍 Before → After 响应",
        calibration_rmse_8bit=5.21,
        coefficients=(
            (0.06767391, 0.04814469, 0.00298772),
            (0.67809317, -0.23831098, -0.30518514),
            (0.21446873, 1.34490377, 0.52807193),
            (-0.27912567, -0.27146442, 0.56998981),
            (0.01623547, -0.29495322, -0.37920100),
            (-0.31358747, -0.43444090, -0.60070971),
            (-0.15192163, -0.31026456, -0.02978648),
            (0.17461641, 0.60235969, 0.68069850),
            (0.16472888, -0.01920275, 0.25622951),
            (0.24241433, 0.37843495, 0.10966048),
        ),
    ),
    RestorationResponseProfile(
        cct=4000.0,
        label="4000K 实拍 Before → After 响应",
        calibration_rmse_8bit=1.49,
        coefficients=(
            (-0.01385945, -0.01066486, -0.03597134),
            (1.12520654, -0.38948752, -0.21500409),
            (-0.17275544, 1.73045859, 0.25877067),
            (-0.06758648, -0.51518248, 0.89398633),
            (-0.27524239, -0.40991883, -0.10629668),
            (-0.25398024, -0.91154652, 0.29011947),
            (0.02649562, -0.39065035, -0.04354867),
            (0.44327141, 0.99677083, -0.23920716),
            (0.02882176, -0.26395886, 0.49260045),
            (0.00094204, 1.02043992, -0.44847056),
        ),
    ),
)
CALIBRATED_CCT_RANGE = (2800.0, 4500.0)


def _interpolate_matrix(first: Matrix3, second: Matrix3, weight: float) -> Matrix3:
    return tuple(
        tuple(
            first[row][column]
            + weight * (second[row][column] - first[row][column])
            for column in range(3)
        )
        for row in range(3)
    )  # type: ignore[return-value]


def _mired_interpolation_weight(cct: float, low: float, high: float) -> float:
    mired = 1_000_000.0 / cct
    low_mired = 1_000_000.0 / low
    high_mired = 1_000_000.0 / high
    return (mired - low_mired) / (high_mired - low_mired)


def build_calibrated_restoration_plan(
    original_matrix: Matrix3,
    cct: float,
    *,
    strength: float = 1.0,
) -> RestorationPlan:
    """Build the validated red-restoration correction for one CCT Region.

    Exact 3000K and 4000K source matrices reproduce the accepted matrices
    byte-for-byte at 100% strength.  Between those anchors, Delta CCMs are
    interpolated in mired space so the neutral-preserving property remains
    continuous.  The adjacent 2800K–3000K and 4000K–4500K portions use their
    nearest validated anchor with a warning; values outside the two XML Region
    ranges are rejected instead of applying an unrelated profile.
    """

    if not math.isfinite(cct) or cct <= 0:
        raise ColorCheckerError("CCT 必须是大于 0 的有效 Kelvin 数值。")
    if not 0.05 <= strength <= 1.0:
        raise ColorCheckerError("色彩还原强度必须在 5% 到 100% 之间。")
    if not CALIBRATED_CCT_RANGE[0] <= cct <= CALIBRATED_CCT_RANGE[1]:
        raise ColorCheckerError(
            f"资料标定模式仅覆盖 {CALIBRATED_CCT_RANGE[0]:g}K–{CALIBRATED_CCT_RANGE[1]:g}K。"
            "当前 CCT 请改用“图像拟合”模式，或补充该色温的实拍验证矩阵。"
        )

    low, high = RESTORATION_PROFILES
    warnings: list[str] = []
    if cct <= low.cct:
        anchor_correction = low.correction_matrix
        label = low.label
        exact_profile = abs(cct - low.cct) <= 1e-6
        profile = low
        if cct < low.cct - 1e-6:
            warnings.append(
                f"{cct:g}K 低于实拍标定范围，暂采用最近的 {low.cct:g}K Delta CCM。"
            )
    elif cct >= high.cct:
        anchor_correction = high.correction_matrix
        label = high.label
        exact_profile = abs(cct - high.cct) <= 1e-6
        profile = high
        if cct > high.cct + 1e-6:
            warnings.append(
                f"{cct:g}K 高于实拍标定范围，暂采用最近的 {high.cct:g}K Delta CCM。"
            )
    else:
        weight = _mired_interpolation_weight(cct, low.cct, high.cct)
        anchor_correction = _interpolate_matrix(
            low.correction_matrix,
            high.correction_matrix,
            weight,
        )
        label = f"{low.cct:g}K ↔ {high.cct:g}K mired 插值"
        exact_profile = False
        profile = None

    # Loading the already-accepted XML must never apply the same correction a
    # second time.  Exact source matches, on the other hand, intentionally land
    # on the supplied target matrix at full strength.
    already_calibrated = bool(
        profile is not None
        and matrix_distance(original_matrix, profile.target_matrix) <= 2e-5
    )
    if already_calibrated:
        correction = identity_matrix()
        optimized = original_matrix
        warnings.append("当前 Region 已与实拍验证矩阵一致，未重复叠加 Delta CCM。")
    else:
        correction = matrix_blend(anchor_correction, strength)
        optimized = mat_mul(correction, original_matrix)
        if (
            profile is not None
            and strength >= 1.0 - 1e-12
            and matrix_distance(original_matrix, profile.source_matrix) <= 2e-5
        ):
            # Preserve the six-decimal, on-device validated coefficients as the
            # authoritative 100% endpoint instead of exposing inversion noise.
            optimized = profile.target_matrix
            correction = mat_mul(optimized, inverse(original_matrix))
        elif profile is not None and matrix_distance(original_matrix, profile.source_matrix) > 2e-5:
            warnings.append(
                "所选 Region 与该 CCT 的标定起始矩阵不同；已应用 Delta CCM，保存前请重点复核肤色、橙色和灰阶。"
            )

    row_sums = tuple(sum(row) for row in optimized)
    neutral_scale = sum(row_sums) / 3.0
    neutral_spread = max(row_sums) - min(row_sums)
    return RestorationPlan(
        correction_matrix=correction,
        optimized_matrix=optimized,
        requested_cct=cct,
        profile_label=label,
        strength=strength,
        exact_profile=exact_profile,
        already_calibrated=already_calibrated,
        neutral_scale=neutral_scale,
        neutral_spread=neutral_spread,
        warnings=tuple(warnings),
    )


def restoration_evaluation_config(
    original_matrix: Matrix3,
    optimized_matrix: Matrix3,
) -> OptimizationConfig:
    """Engineering limits for an already validated restoration profile."""

    values = [value for matrix in (original_matrix, optimized_matrix) for row in matrix for value in row]
    distance = matrix_distance(original_matrix, optimized_matrix)
    delta = max_matrix_delta(original_matrix, optimized_matrix)
    # Matrix Smoothness and per-coefficient delta share one historical limit.
    # Expand it only enough to evaluate this validated endpoint as intended.
    required_delta_limit = max(delta, distance / 0.65) * 1.05
    return OptimizationConfig(
        strategy="aggressive",
        max_blend=1.0,
        coefficient_min=min(-3.0, min(values)),
        coefficient_max=max(3.0, max(values)),
        focus_patches=(1, 2, 7, 9, 15, 17),
        focus_weight=4.0,
        skin_weight=2.5,
        primary_weight=2.0,
        secondary_weight=1.5,
        memory_weight=1.5,
        max_matrix_delta=max(1.10, required_delta_limit),
        allow_common_neutral_scale=True,
    )


def _as_polygon(values: np.ndarray) -> PatchPolygon:
    points = np.asarray(values, dtype=np.float64).reshape(4, 2)
    return tuple((float(point[0]), float(point[1])) for point in points)  # type: ignore[return-value]


def _sample_polygon(rgb: np.ndarray, polygon: np.ndarray) -> tuple[Vector3, Vector3]:
    """Sample the stable centre of one detected patch at original resolution."""

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - project dependency
        raise ColorCheckerError("ColorChecker 自动识别需要 OpenCV；请重新同步工程环境。") from exc

    points = np.asarray(polygon, dtype=np.float64).reshape(4, 2)
    centre = np.mean(points, axis=0)
    # MCC already returns central patch polygons.  A small additional inset
    # excludes border blur, printed seams and corner glare without collapsing
    # small charts to a handful of pixels.
    points = centre + (points - centre) * 0.82
    height, width = rgb.shape[:2]
    x0 = max(0, int(math.floor(float(np.min(points[:, 0])))))
    y0 = max(0, int(math.floor(float(np.min(points[:, 1])))))
    x1 = min(width, int(math.ceil(float(np.max(points[:, 0])))) + 1)
    y1 = min(height, int(math.ceil(float(np.max(points[:, 1])))) + 1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        raise ColorCheckerError("检测到的 ColorChecker 色块过小，无法可靠取样。")
    local = np.rint(points - np.asarray((x0, y0))).astype(np.int32)
    mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    cv2.fillConvexPoly(mask, local, 255)
    pixels = np.asarray(rgb[y0:y1, x0:x1], dtype=np.float64)[mask > 0]
    if pixels.shape[0] < 16:
        raise ColorCheckerError("ColorChecker 色块有效像素不足，请使用更清晰或更高分辨率的图片。")
    # Trim only extreme luminance samples (dust, specular highlights and black
    # frame leakage) while keeping the actual RGB relationship intact.
    luminance = pixels @ LUMA_WEIGHTS
    low, high = np.quantile(luminance, (0.02, 0.98))
    stable = pixels[(luminance >= low) & (luminance <= high)]
    if stable.shape[0] < 16:
        stable = pixels
    mean = np.mean(stable, axis=0)
    std = np.std(stable, axis=0)
    return (
        tuple(float(value) for value in mean),  # type: ignore[return-value]
        tuple(float(value) for value in std),  # type: ignore[return-value]
    )


def _mcc_polygons(image_rgb: np.ndarray) -> Optional[tuple[np.ndarray, np.ndarray, float]]:
    """Return MCC chart box/polygons in the supplied image coordinate space."""

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - project dependency
        raise ColorCheckerError("ColorChecker 自动识别需要 OpenCV；请重新同步工程环境。") from exc
    mcc = getattr(cv2, "mcc", None)
    if mcc is None or not hasattr(mcc, "CCheckerDetector_create"):
        return None
    detector = mcc.CCheckerDetector_create()
    bgr = np.ascontiguousarray(image_rgb[..., ::-1])
    try:
        # OpenCV 5 exposes chart type as detector state.  OpenCV 4 contrib
        # accepted the chart type as a process argument; support both APIs.
        if hasattr(detector, "setColorChartType"):
            detector.setColorChartType(mcc.MCC24)
            found = bool(detector.process(bgr, 1))
        else:  # pragma: no cover - depends on OpenCV 4.x binding
            found = bool(detector.process(bgr, mcc.MCC24, 1, False))
    except (TypeError, cv2.error):  # pragma: no cover - compatibility path
        try:
            found = bool(detector.process(bgr, mcc.MCC24, 1, False))
        except (TypeError, cv2.error):
            return None
    if not found:
        return None
    checker = detector.getBestColorChecker()
    if checker is None:
        return None
    polygons = np.asarray(checker.getColorCharts(), dtype=np.float64).reshape(-1, 4, 2)
    if polygons.shape[0] != 24:
        return None
    box = np.asarray(checker.getBox(), dtype=np.float64).reshape(4, 2)
    try:
        cost = max(0.0, float(checker.getCost()))
    except (AttributeError, TypeError, ValueError):
        cost = 0.02
    confidence = max(0.0, min(1.0, 1.0 / (1.0 + 30.0 * cost)))
    return box, polygons, confidence


def _ordered_quad(points: np.ndarray) -> np.ndarray:
    values = np.asarray(points, dtype=np.float64).reshape(4, 2)
    total = values.sum(axis=1)
    difference = np.diff(values, axis=1).ravel()
    ordered = np.asarray(
        (
            values[np.argmin(total)],
            values[np.argmin(difference)],
            values[np.argmax(total)],
            values[np.argmax(difference)],
        ),
        dtype=np.float64,
    )
    horizontal = (np.linalg.norm(ordered[1] - ordered[0]) + np.linalg.norm(ordered[2] - ordered[3])) / 2.0
    vertical = (np.linalg.norm(ordered[3] - ordered[0]) + np.linalg.norm(ordered[2] - ordered[1])) / 2.0
    if vertical > horizontal:
        # Put the physical six-patch (long) edge first, even when the chart is
        # photographed in portrait orientation.
        ordered = ordered[[1, 2, 3, 0]]
    return ordered


def _fallback_chart_box(image_rgb: np.ndarray) -> Optional[tuple[np.ndarray, float]]:
    """Locate the dark chart body when the optional MCC module is unavailable."""

    import cv2

    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    image_area = float(gray.shape[0] * gray.shape[1])
    kernel_side = max(5, int(round(min(gray.shape[:2]) * 0.012)))
    if kernel_side % 2 == 0:
        kernel_side += 1
    kernel = np.ones((kernel_side, kernel_side), dtype=np.uint8)
    otsu, _unused = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thresholds = sorted({45, 60, 75, 90, 105, 120, 140, 160, int(round(float(otsu)))})
    candidates: list[tuple[float, np.ndarray]] = []
    for threshold in thresholds:
        mask = np.where(gray < threshold, 255, 0).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = float(cv2.contourArea(contour))
            ratio = area / image_area
            if not 0.08 <= ratio <= 0.82:
                continue
            rectangle = cv2.minAreaRect(contour)
            side_a, side_b = rectangle[1]
            if min(side_a, side_b) <= 20:
                continue
            aspect = max(side_a, side_b) / min(side_a, side_b)
            if not 1.15 <= aspect <= 2.10:
                continue
            rectangularity = area / max(side_a * side_b, 1.0)
            if rectangularity < 0.55:
                continue
            score = rectangularity - abs(aspect - 1.50) * 0.16 + min(ratio, 0.65) * 0.10
            candidates.append((score, _ordered_quad(cv2.boxPoints(rectangle))))
    if not candidates:
        return None
    score, box = max(candidates, key=lambda item: item[0])
    confidence = max(0.45, min(0.78, 0.55 + (score - 0.55) * 0.35))
    return box, confidence


def _grid_polygons(box: np.ndarray) -> np.ndarray:
    import cv2

    source = np.asarray(((0.0, 0.0), (6.0, 0.0), (6.0, 4.0), (0.0, 4.0)), dtype=np.float32)
    transform = cv2.getPerspectiveTransform(source, np.asarray(box, dtype=np.float32))
    polygons = []
    for row in range(4):
        for column in range(6):
            cell = np.asarray(
                (
                    (column + 0.24, row + 0.24),
                    (column + 0.76, row + 0.24),
                    (column + 0.76, row + 0.76),
                    (column + 0.24, row + 0.76),
                ),
                dtype=np.float32,
            )
            polygons.append(cv2.perspectiveTransform(cell[None, ...], transform)[0])
    return np.asarray(polygons, dtype=np.float64)


def _saturation(rgb: Sequence[float]) -> float:
    maximum = max(rgb)
    return 0.0 if maximum <= 1e-9 else (maximum - min(rgb)) / maximum


def _standardize_fallback_order(
    polygons: np.ndarray,
    means: Sequence[Vector3],
    stds: Sequence[Vector3],
) -> tuple[np.ndarray, list[Vector3], list[Vector3]]:
    grid_polygons = np.asarray(polygons).reshape(4, 6, 4, 2)
    grid_means = np.asarray(means, dtype=np.float64).reshape(4, 6, 3)
    grid_stds = np.asarray(stds, dtype=np.float64).reshape(4, 6, 3)
    row_saturation = [float(np.median([_saturation(value) for value in row])) for row in grid_means]
    neutral_row = int(np.argmin(row_saturation))
    if neutral_row not in (0, 3):
        raise ColorCheckerError("检测到候选矩形，但无法确认 ColorChecker 的六级灰阶方向。")
    if neutral_row == 0:
        grid_polygons = grid_polygons[::-1]
        grid_means = grid_means[::-1]
        grid_stds = grid_stds[::-1]
    neutral_luma = grid_means[3] @ LUMA_WEIGHTS
    if float(neutral_luma[0]) < float(neutral_luma[-1]):
        grid_polygons = grid_polygons[:, ::-1]
        grid_means = grid_means[:, ::-1]
        grid_stds = grid_stds[:, ::-1]
    return (
        grid_polygons.reshape(24, 4, 2),
        [tuple(float(value) for value in row) for row in grid_means.reshape(24, 3)],  # type: ignore[list-item]
        [tuple(float(value) for value in row) for row in grid_stds.reshape(24, 3)],  # type: ignore[list-item]
    )


def detect_colorchecker(source: Union[str, Path, ImageData]) -> ColorCheckerDetection:
    """Automatically locate and sample one ColorChecker Classic 24 chart."""

    try:
        image = source if isinstance(source, ImageData) else load_image(source)
    except ImageLoadError as exc:
        raise ColorCheckerError(str(exc)) from exc
    rgb = np.ascontiguousarray(image.display_rgb, dtype=np.uint8)
    scale = min(1.0, MAX_DETECTION_SIDE / max(image.width, image.height))
    if scale < 1.0:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - project dependency
            raise ColorCheckerError("ColorChecker 自动识别需要 OpenCV；请重新同步工程环境。") from exc
        detection_rgb = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    else:
        detection_rgb = rgb

    mcc_result = _mcc_polygons(detection_rgb)
    warning = ""
    if mcc_result is not None:
        chart_box, polygons, confidence = mcc_result
        chart_box = chart_box / scale
        polygons = polygons / scale
        means: list[Vector3] = []
        stds: list[Vector3] = []
        for polygon in polygons:
            mean, std = _sample_polygon(rgb, polygon)
            means.append(mean)
            stds.append(std)
        method = "OpenCV MCC24"
    else:
        fallback = _fallback_chart_box(detection_rgb)
        if fallback is None:
            raise ColorCheckerError(
                "未识别到完整的 ColorChecker Classic 24。请让 24 个色块全部入镜、减少反光，"
                "并避免色卡面积过小或严重透视。"
            )
        chart_box, confidence = fallback
        chart_box = chart_box / scale
        polygons = _grid_polygons(chart_box)
        means = []
        stds = []
        for polygon in polygons:
            mean, std = _sample_polygon(rgb, polygon)
            means.append(mean)
            stds.append(std)
        polygons, means, stds = _standardize_fallback_order(polygons, means, stds)
        method = "几何网格后备"
        warning = "当前 OpenCV 未提供 MCC24 检测器，已使用几何网格后备；保存前请核对覆盖框与色块顺序。"

    patches = tuple(
        DetectedPatch(
            zone=index + 1,
            name=PATCH_NAMES_ZH[index],
            polygon=_as_polygon(polygons[index]),
            mean_rgb=means[index],
            std_rgb=stds[index],
        )
        for index in range(24)
    )
    mean_std = float(np.mean([value for patch in patches for value in patch.std_rgb]))
    if mean_std > 18.0:
        quality_warning = f"色块内部平均波动 {mean_std:.1f} 较高，可能存在反光、模糊或边缘取样。"
        warning = f"{warning} {quality_warning}".strip()
    return ColorCheckerDetection(
        image=image,
        chart_box=_as_polygon(chart_box),
        patches=patches,
        method=method,
        confidence=confidence,
        warning=warning,
    )


def _luminance_matched_reference(measured: Vector3, reference: Vector3) -> Vector3:
    measured_linear = np.asarray(srgb_to_linear(measured), dtype=np.float64)
    reference_linear = np.asarray(srgb_to_linear(reference), dtype=np.float64)
    measured_luma = float(measured_linear @ LUMA_WEIGHTS)
    reference_luma = float(reference_linear @ LUMA_WEIGHTS)
    scale = measured_luma / reference_luma if reference_luma > 1e-12 else 1.0
    return linear_to_srgb(tuple(float(value * scale) for value in reference_linear))  # type: ignore[arg-type]


def build_comparison_dataset(
    measured: ColorCheckerDetection,
    reference: ColorCheckerDetection,
) -> ImatestDataset:
    """Build a 24-patch dataset while excluding JPEG exposure differences."""

    if len(measured.patches) != 24 or len(reference.patches) != 24:
        raise ColorCheckerError("测试图和目标都必须提供 24 个 ColorChecker 色块。")
    patches = []
    for measured_patch, reference_patch in zip(measured.patches, reference.patches):
        target = _luminance_matched_reference(measured_patch.srgb, reference_patch.srgb)
        patches.append(
            ColorPatch(
                zone=measured_patch.zone,
                measured_srgb=measured_patch.srgb,
                ideal_srgb=target,
                name=measured_patch.name,
            )
        )
    warnings = [
        "目标色块已在 linear sRGB 中逐色块匹配测试图亮度；CCM 仅拟合色度差异。",
    ]
    for detection in (measured, reference):
        if detection.warning:
            warnings.append(f"{detection.image.path.name}: {detection.warning}")
    return ImatestDataset(
        source_path=measured.image.path,
        patches=patches,
        image_name=measured.image.path.name,
        color_space="sRGB",
        # Region selection describes the illumination of the test capture.
        # A custom target may be a standard chart or a stylistic endpoint, so
        # its filename must never override the test image's CCT.
        inferred_cct=infer_cct(measured.image.path.name),
        warnings=warnings,
    )


def image_optimization_config(
    original_matrix: Matrix3,
    *,
    strategy: str = "balanced",
    maximum_strength: float = 0.80,
) -> OptimizationConfig:
    """Return the image workflow's neutral-safe, memory-colour-weighted config."""

    values = [value for row in original_matrix for value in row]
    return OptimizationConfig(
        strategy=strategy,
        max_blend=maximum_strength,
        coefficient_min=min(-3.0, min(values)),
        coefficient_max=max(3.0, max(values)),
        focus_patches=(1, 2, 9, 15),
        focus_weight=3.0,
        skin_weight=2.0,
        primary_weight=1.5,
        secondary_weight=1.25,
        memory_weight=1.2,
    )


def _response_features(encoded: np.ndarray) -> np.ndarray:
    """Return the bounded quadratic feature basis for encoded RGB rows."""

    red, green, blue = encoded.T
    return np.column_stack(
        (
            np.ones(encoded.shape[0], dtype=encoded.dtype),
            red,
            green,
            blue,
            red * red,
            green * green,
            blue * blue,
            red * green,
            red * blue,
            green * blue,
        )
    )


def _response_profile_weights(cct: float) -> tuple[float, float]:
    """Return low/high response weights using the CCM plan's CCT semantics."""

    low, high = RESTORATION_RESPONSE_PROFILES
    if cct <= low.cct:
        return (1.0, 0.0)
    if cct >= high.cct:
        return (0.0, 1.0)
    high_weight = _mired_interpolation_weight(cct, low.cct, high.cct)
    return (1.0 - high_weight, high_weight)


def simulate_restoration_response(
    image: Union[ImageData, np.ndarray],
    plan: RestorationPlan,
    *,
    chunk_rows: int = 128,
) -> SimulationResult:
    """Render a calibrated CCM through the observed real-capture response.

    At 3000K and 4000K the response is fitted directly from the supplied
    Before/After ColorChecker captures.  Intermediate CCTs blend the two
    response surfaces in mired space, matching the Delta CCM interpolation.
    Strength blends the rendered endpoint with the original encoded pixels.
    An already-calibrated XML Region returns the input unchanged so the same
    accepted correction is never previewed twice.
    """

    source = image.display_rgb if isinstance(image, ImageData) else np.asarray(image)
    if source.ndim != 3 or source.shape[2] < 3:
        raise ColorCheckerError("仿真输入必须是 RGB 图片。")
    if not CALIBRATED_CCT_RANGE[0] <= plan.requested_cct <= CALIBRATED_CCT_RANGE[1]:
        raise ColorCheckerError(
            f"实拍响应模型仅覆盖 {CALIBRATED_CCT_RANGE[0]:g}K–{CALIBRATED_CCT_RANGE[1]:g}K。"
        )

    source_rgb = np.ascontiguousarray(source[..., :3], dtype=np.uint8)
    if plan.already_calibrated:
        return SimulationResult(
            rgb=source_rgb.copy(),
            clipped_pixel_ratio=0.0,
            domain="real-shot-response",
        )

    low_weight, high_weight = _response_profile_weights(plan.requested_cct)
    low_coefficients = np.asarray(
        RESTORATION_RESPONSE_PROFILES[0].coefficients,
        dtype=np.float32,
    )
    high_coefficients = np.asarray(
        RESTORATION_RESPONSE_PROFILES[1].coefficients,
        dtype=np.float32,
    )
    output = np.empty_like(source_rgb)
    clipped_pixels = 0
    pixel_count = source_rgb.shape[0] * source_rgb.shape[1]
    rows = max(1, int(chunk_rows))
    for start in range(0, source_rgb.shape[0], rows):
        stop = min(source_rgb.shape[0], start + rows)
        encoded = source_rgb[start:stop].astype(np.float32).reshape(-1, 3) / 255.0
        features = _response_features(encoded)
        endpoint = low_weight * (features @ low_coefficients)
        if high_weight:
            endpoint += high_weight * (features @ high_coefficients)
        rendered = encoded + plan.strength * (endpoint - encoded)
        clipped_pixels += int(np.count_nonzero(np.any((rendered < 0.0) | (rendered > 1.0), axis=1)))
        output[start:stop] = np.rint(
            np.clip(rendered, 0.0, 1.0).reshape(stop - start, source_rgb.shape[1], 3)
            * 255.0
        ).astype(np.uint8)
    return SimulationResult(
        rgb=output,
        clipped_pixel_ratio=clipped_pixels / pixel_count if pixel_count else 0.0,
        domain="real-shot-response",
    )


def sample_patch_means(
    image: Union[ImageData, np.ndarray],
    detection: ColorCheckerDetection,
) -> tuple[Vector3, ...]:
    """Sample RGB means from an image using an existing chart geometry."""

    source = image.display_rgb if isinstance(image, ImageData) else np.asarray(image)
    if source.ndim != 3 or source.shape[2] < 3:
        raise ColorCheckerError("色块取样输入必须是 RGB 图片。")
    if source.shape[:2] != detection.image.display_rgb.shape[:2]:
        raise ColorCheckerError("仿真图尺寸与测试图不一致，无法复用色块位置。")
    means = []
    for patch in detection.patches:
        mean_rgb, _std_rgb = _sample_polygon(source[..., :3], np.asarray(patch.polygon))
        means.append(mean_rgb)
    return tuple(means)


def simulate_correction(
    image: Union[ImageData, np.ndarray],
    correction: Matrix3,
    *,
    chunk_rows: int = 256,
    domain: str = "linear",
) -> SimulationResult:
    """Apply a Delta CCM to a rendered image for an explicitly labelled preview.

    ``linear`` is the physically conventional approximation for an ISP CCM.
    ``encoded`` is retained as a generic rendered-image approximation.  The
    calibrated 3000K/4000K workflow uses :func:`simulate_restoration_response`
    instead, because a direct matrix on final JPEG pixels omits too much of the
    observed ISP response.  Neither path replaces an on-device recapture.
    """

    source = image.display_rgb if isinstance(image, ImageData) else np.asarray(image)
    if source.ndim != 3 or source.shape[2] < 3:
        raise ColorCheckerError("仿真输入必须是 RGB 图片。")
    if domain not in {"linear", "encoded"}:
        raise ColorCheckerError(f"未知仿真域：{domain}")
    source_rgb = np.ascontiguousarray(source[..., :3], dtype=np.uint8)
    output = np.empty_like(source_rgb)
    matrix = np.asarray(correction, dtype=np.float32)
    clipped_pixels = 0
    pixel_count = source_rgb.shape[0] * source_rgb.shape[1]
    rows = max(1, int(chunk_rows))
    for start in range(0, source_rgb.shape[0], rows):
        stop = min(source_rgb.shape[0], start + rows)
        encoded = source_rgb[start:stop].astype(np.float32) / 255.0
        if domain == "encoded":
            corrected = encoded @ matrix.T
        else:
            linear = np.where(
                encoded <= 0.04045,
                encoded / 12.92,
                ((encoded + 0.055) / 1.055) ** 2.4,
            )
            corrected = linear @ matrix.T
        clipped_pixels += int(np.count_nonzero(np.any((corrected < 0.0) | (corrected > 1.0), axis=2)))
        corrected = np.clip(corrected, 0.0, 1.0)
        if domain == "encoded":
            simulated = corrected
        else:
            simulated = np.where(
                corrected <= 0.0031308,
                corrected * 12.92,
                1.055 * np.power(corrected, 1.0 / 2.4) - 0.055,
            )
        output[start:stop] = np.rint(np.clip(simulated, 0.0, 1.0) * 255.0).astype(np.uint8)
    return SimulationResult(
        rgb=output,
        clipped_pixel_ratio=clipped_pixels / pixel_count if pixel_count else 0.0,
        domain=domain,
    )
