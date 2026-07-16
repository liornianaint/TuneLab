from __future__ import annotations

import bisect
import math
from dataclasses import replace
from statistics import mean
from typing import Callable, Optional, Sequence

from .models import (
    GammaCurveHealth,
    GammaEngineeringCheck,
    GammaLossBreakdown,
    GammaMetrics,
    GammaModuleDiagnosis,
    GammaOptimizationConfig,
    GammaOptimizationResult,
    GammaPairResult,
    GammaRegion,
    GammaZoneResult,
    GrayDataset,
    GrayRangeAnalysis,
    GrayZone,
)
from .imatest import analyze_pixel_values, select_fit_zones


class GammaOptimizationError(ValueError):
    pass


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def minimum_continuity_gap(threshold: float) -> float:
    """Return the shared capture/quantization reserve for a required pair."""

    return threshold + min(0.5, max(0.25, threshold * 0.0625))


def _normalized(values: Sequence[int], maximum: int) -> tuple[float, ...]:
    return tuple(value / maximum for value in values)


def _average_curves(*curves: Sequence[float]) -> tuple[float, ...]:
    return tuple(sum(values) / len(values) for values in zip(*curves))


def evaluate_lut(curve: Sequence[float], input_value: float) -> float:
    position = _clamp(input_value) * (len(curve) - 1)
    low = int(math.floor(position))
    high = min(len(curve) - 1, low + 1)
    fraction = position - low
    return curve[low] * (1.0 - fraction) + curve[high] * fraction


def invert_lut(curve: Sequence[float], output_value: float) -> float:
    target = _clamp(output_value)
    index = bisect.bisect_left(curve, target)
    if index <= 0:
        return 0.0
    if index >= len(curve):
        return 1.0
    low_value = curve[index - 1]
    high_value = curve[index]
    fraction = 0.0 if high_value <= low_value else (target - low_value) / (high_value - low_value)
    return (index - 1 + fraction) / (len(curve) - 1)


def _linear_regression(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    if len(xs) != len(ys) or len(xs) < 2:
        raise GammaOptimizationError("Gamma 回归至少需要两个灰阶点。")
    x_mean = mean(xs)
    y_mean = mean(ys)
    denominator = sum((value - x_mean) ** 2 for value in xs)
    if denominator <= 1e-12:
        raise GammaOptimizationError("灰阶 Log Exposure 无变化，无法计算 Gamma。")
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    return slope, y_mean - slope * x_mean


def _brightness_target_pixels(
    zones: Sequence[GrayZone],
    brightness_factor: float,
) -> dict[int, float]:
    """Return an intuitive brightness target: 1.0=no lift, larger=brighter."""

    exponent = 1.0 / brightness_factor
    return {
        zone.zone: 255.0 * _clamp(zone.pixel_normalized, 1e-9, 1.0) ** exponent
        for zone in zones
    }


def _pava_nonincreasing(values: Sequence[float]) -> list[float]:
    blocks: list[list[float]] = []
    for index, value in enumerate(values):
        blocks.append([float(value), 1.0, float(index), float(index)])
        while len(blocks) >= 2:
            previous = blocks[-2][0] / blocks[-2][1]
            current = blocks[-1][0] / blocks[-1][1]
            if previous >= current - 1e-12:
                break
            right = blocks.pop()
            left = blocks.pop()
            blocks.append(
                [left[0] + right[0], left[1] + right[1], left[2], right[3]]
            )
    output = [0.0] * len(values)
    for total, weight, start, end in blocks:
        block_mean = total / weight
        for index in range(int(start), int(end) + 1):
            output[index] = block_mean
    return output


def _project_minimum_pixel_gaps(
    desired: Sequence[float],
    minimum_gaps: Sequence[float],
) -> tuple[float, ...]:
    """Least-squares projection for q[i]-q[i+1] >= gap[i]."""

    if len(desired) != len(minimum_gaps) + 1:
        raise GammaOptimizationError("目标灰阶与相邻间隔数量不一致。")
    cumulative = [0.0]
    for gap in minimum_gaps:
        cumulative.append(cumulative[-1] + gap)
    transformed = [value + offset for value, offset in zip(desired, cumulative)]
    projected = _pava_nonincreasing(transformed)
    output = [value - offset for value, offset in zip(projected, cumulative)]
    # A constant shift preserves all gaps.  Choose the smallest shift needed to
    # fit the physical 8-bit Stepchart response domain.
    low_shift = -min(output)
    high_shift = 255.0 - max(output)
    if low_shift > high_shift + 1e-9:
        raise GammaOptimizationError("目标阶数与识别阈值组合超过 0~255 灰阶动态范围。")
    shift = max(low_shift, min(0.0, high_shift))
    return tuple(_clamp(value + shift, 0.0, 255.0) for value in output)


def _target_profile(
    dataset: GrayDataset,
    analysis: GrayRangeAnalysis,
    config: GammaOptimizationConfig,
) -> tuple[dict[int, float], tuple[int, ...], int]:
    ordered = sorted(dataset.zones, key=lambda zone: zone.zone)
    desired = _brightness_target_pixels(ordered, config.target_gamma)
    before_count = analysis.effective_count
    requested_count = config.target_step_count if config.target_step_count is not None else before_count
    requested_count = max(before_count, requested_count)
    if requested_count > len(ordered) - 1:
        raise GammaOptimizationError(
            f"目标可识别阶数最多为 {len(ordered) - 1}，当前请求 {requested_count}。"
        )
    start_zone = analysis.start_zone if analysis.start_zone is not None else ordered[0].zone
    start_index = next(
        (index for index, zone in enumerate(ordered) if zone.zone == start_zone),
        0,
    )
    if start_index + requested_count >= len(ordered):
        start_index = max(0, len(ordered) - requested_count - 1)
    constrained = ordered[start_index : start_index + requested_count + 1]
    current_pixels = [zone.pixel for zone in constrained]
    base_targets = [desired[zone.zone] for zone in constrained]
    # Add a quantization/capture reserve and recover the complete measured
    # shortfall.  This target is intentionally independent of the adjustment
    # limit: raising maximum_adjustment must never weaken the requested stage
    # separation.  The displacement itself is compensated for the limit below.
    strength = max(config.maximum_adjustment, 0.05)
    minimum_gaps = []
    for current, following in zip(current_pixels, current_pixels[1:]):
        current_gap = current - following
        # LUT interpolation and integer quantization reduce the
        # measured spacing slightly.  A 2.5-Pixel reserve keeps the final
        # Stepchart result on the requested side of the threshold.
        required_target_gap = (
            analysis.threshold
            + 2.50
            + max(0.0, analysis.threshold - current_gap)
        )
        minimum_gaps.append(required_target_gap)
    projected = _project_minimum_pixel_gaps(base_targets, minimum_gaps)
    # Compensate the target displacement for the maximum-strength interpolation
    # so the actual curve, not only the dashed target, reaches the threshold.
    for zone, projected_pixel in zip(constrained, projected):
        current = zone.pixel
        desired[zone.zone] = _clamp(
            current + (projected_pixel - current) / strength,
            1e-6,
            255.0,
        )
    target_densities = {
        zone.zone: -math.log10(max(desired[zone.zone] / 255.0, 1e-9))
        for zone in ordered
    }
    return target_densities, tuple(zone.zone for zone in constrained), requested_count


def _uniform_step_targets(
    dataset: GrayDataset,
    analysis: GrayRangeAnalysis,
    config: GammaOptimizationConfig,
    target_step_count: int,
) -> tuple[dict[int, float], dict[int, float], tuple[int, ...], tuple[int, ...]]:
    """Build an evenly spaced high-stage target and its compensated fit target.

    Some captures contain a narrow cluster of almost identical dark patches.
    Meeting a 17/18-stage request by repairing only those few pairs creates a
    visible local contrast hump.  Instead, redistribute the complete requested
    response span into equal Pixel gaps, then fit one broad, smooth tone warp.
    The first non-required pair is an explicit guard anchor so an exact request
    cannot silently grow by one or more stages after interpolation.
    """

    ordered = tuple(sorted(dataset.zones, key=lambda zone: zone.zone))
    start_zone = analysis.start_zone if analysis.start_zone is not None else ordered[0].zone
    start_index = next(
        (index for index, zone in enumerate(ordered) if zone.zone == start_zone),
        0,
    )
    if start_index + target_step_count >= len(ordered):
        start_index = max(0, len(ordered) - target_step_count - 1)
    constrained = ordered[start_index : start_index + target_step_count + 1]
    if len(constrained) != target_step_count + 1:
        raise GammaOptimizationError(
            f"连续 {target_step_count} 阶需要 {target_step_count + 1} 个 Zone。"
        )

    actual_pixels = _brightness_target_pixels(ordered, config.target_gamma)
    first_pixel = actual_pixels[constrained[0].zone]
    last_pixel = actual_pixels[constrained[-1].zone]
    natural_gap = (first_pixel - last_pixel) / target_step_count
    target_gap = max(
        natural_gap + 0.35,
        minimum_continuity_gap(config.threshold) + 0.75,
    )
    required_span = target_gap * target_step_count
    if required_span > 255.0 + 1e-9:
        raise GammaOptimizationError(
            f"连续 {target_step_count} 阶与阈值 {config.threshold:g} 超过 0~255 动态范围。"
        )
    centre = (first_pixel + last_pixel) / 2.0
    top = centre + required_span / 2.0
    bottom = centre - required_span / 2.0
    shift = max(-bottom, min(0.0, 255.0 - top))
    top += shift
    for index, zone in enumerate(constrained):
        actual_pixels[zone.zone] = top - index * target_gap

    anchor_zones = [zone.zone for zone in constrained]
    guard_index = start_index + target_step_count + 1
    if guard_index < len(ordered):
        guard = ordered[guard_index]
        # Leave a generous sub-threshold boundary reserve.  PCHIP smoothing and
        # integer quantization can add roughly 1–2 Pixel to this transition.
        guard_gap = max(0.5, config.threshold - 3.0)
        actual_pixels[guard.zone] = max(
            1e-6,
            actual_pixels[constrained[-1].zone] - guard_gap,
        )
        anchor_zones.append(guard.zone)

    strength = max(config.maximum_adjustment, 0.05)
    fit_pixels = dict(actual_pixels)
    by_zone = {zone.zone: zone for zone in ordered}
    for zone_id in anchor_zones:
        current = by_zone[zone_id].pixel
        fit_pixels[zone_id] = _clamp(
            current + (actual_pixels[zone_id] - current) / strength,
            1e-6,
            255.0,
        )

    actual_target = {
        zone.zone: -math.log10(max(actual_pixels[zone.zone] / 255.0, 1e-9))
        for zone in ordered
    }
    fit_target = {
        zone.zone: -math.log10(max(fit_pixels[zone.zone] / 255.0, 1e-9))
        for zone in ordered
    }
    return (
        actual_target,
        fit_target,
        tuple(zone.zone for zone in constrained),
        tuple(anchor_zones),
    )


def _pchip_derivatives(
    positions: Sequence[float],
    values: Sequence[float],
) -> tuple[float, ...]:
    """Return shape-preserving cubic slopes for monotonic mapping anchors."""

    intervals = [following - current for current, following in zip(positions, positions[1:])]
    secants = [
        (following - current) / interval
        for current, following, interval in zip(values, values[1:], intervals)
    ]
    if len(positions) == 2:
        return (secants[0], secants[0])
    derivatives = [0.0] * len(positions)
    for index in range(1, len(positions) - 1):
        previous = secants[index - 1]
        following = secants[index]
        if previous * following <= 0.0:
            derivatives[index] = 0.0
            continue
        previous_interval = intervals[index - 1]
        following_interval = intervals[index]
        first_weight = 2.0 * following_interval + previous_interval
        second_weight = following_interval + 2.0 * previous_interval
        derivatives[index] = (first_weight + second_weight) / (
            first_weight / previous + second_weight / following
        )

    def edge_derivative(
        first_interval: float,
        second_interval: float,
        first_secant: float,
        second_secant: float,
    ) -> float:
        derivative = (
            (2.0 * first_interval + second_interval) * first_secant
            - first_interval * second_secant
        ) / (first_interval + second_interval)
        if derivative * first_secant <= 0.0:
            return 0.0
        if first_secant * second_secant < 0.0 and abs(derivative) > 3.0 * abs(first_secant):
            return 3.0 * first_secant
        return derivative

    derivatives[0] = edge_derivative(
        intervals[0], intervals[1], secants[0], secants[1]
    )
    derivatives[-1] = edge_derivative(
        intervals[-1], intervals[-2], secants[-1], secants[-2]
    )
    return tuple(derivatives)


def _pchip_curve(
    points: Sequence[tuple[float, float]],
    length: int,
    *,
    first_derivative: Optional[float] = None,
    last_derivative: Optional[float] = None,
) -> tuple[float, ...]:
    positions = [point[0] for point in points]
    values = [point[1] for point in points]
    derivatives = list(_pchip_derivatives(positions, values))
    if first_derivative is not None:
        first_secant = (values[1] - values[0]) / (positions[1] - positions[0])
        derivatives[0] = _clamp(first_derivative, 0.0, 3.0 * first_secant)
    if last_derivative is not None:
        last_secant = (values[-1] - values[-2]) / (positions[-1] - positions[-2])
        derivatives[-1] = _clamp(last_derivative, 0.0, 3.0 * last_secant)
    output: list[float] = []
    for index in range(length):
        position = index / (length - 1)
        segment = max(
            0,
            min(len(points) - 2, bisect.bisect_right(positions, position) - 1),
        )
        interval = positions[segment + 1] - positions[segment]
        fraction = (position - positions[segment]) / interval
        fraction2 = fraction * fraction
        fraction3 = fraction2 * fraction
        output.append(
            (2.0 * fraction3 - 3.0 * fraction2 + 1.0) * values[segment]
            + (fraction3 - 2.0 * fraction2 + fraction)
            * interval
            * derivatives[segment]
            + (-2.0 * fraction3 + 3.0 * fraction2) * values[segment + 1]
            + (fraction3 - fraction2) * interval * derivatives[segment + 1]
        )
    output[0] = values[0]
    output[-1] = values[-1]
    return tuple(output)


def _smooth_monotonic_steps(
    values: Sequence[float],
    *,
    passes: int = 5,
) -> tuple[float, ...]:
    """Smooth LUT slopes while preserving endpoints and strict monotonicity."""

    total = values[-1] - values[0]
    if total <= 0.0:
        return tuple(values)
    average_step = total / (len(values) - 1)
    # A 30% nominal-slope floor prevents quantization plateaus near black.
    # Smoothing the positive slopes (rather than the LUT values) keeps the
    # mapping strictly monotonic throughout every pass.
    minimum_step = average_step * 0.30
    steps = [
        max(minimum_step, following - current)
        for current, following in zip(values, values[1:])
    ]
    for _ in range(passes):
        source = steps[:]
        for index in range(1, len(steps) - 1):
            steps[index] = (
                source[index - 1] + 2.0 * source[index] + source[index + 1]
            ) / 4.0
    scale = total / sum(steps)
    output = [values[0]]
    for step in steps:
        output.append(output[-1] + step * scale)
    output[-1] = values[-1]
    return tuple(output)


def _interpolated_delta(
    anchors: Sequence[tuple[float, float]],
    reference_curve: Sequence[float],
    *,
    highlight_protection: float,
    shadow_protection: float,
) -> tuple[float, ...]:
    """Build a C1-smooth monotonic target curve and return its LUT delta."""

    collapsed: dict[float, list[float]] = {}
    for position, delta in anchors:
        collapsed.setdefault(round(_clamp(position), 8), []).append(delta)
    measured = sorted((position, mean(values)) for position, values in collapsed.items())
    if len(measured) < 2:
        raise GammaOptimizationError(
            f"有效灰阶映射点不足，不能生成 {len(reference_curve)} 点 LUT。"
        )

    points: list[tuple[float, float]] = [(0.0, reference_curve[0])]
    points.extend(
        (
            position,
            evaluate_lut(reference_curve, position) + delta,
        )
        for position, delta in measured
    )
    points.append((1.0, reference_curve[-1]))

    target_by_position: dict[float, list[float]] = {}
    for position, value in points:
        target_by_position.setdefault(round(_clamp(position), 8), []).append(
            _clamp(value, reference_curve[0], reference_curve[-1])
        )
    positions = sorted(target_by_position)
    target_values = [mean(target_by_position[position]) for position in positions]
    target_values[0] = reference_curve[0]
    target_values[-1] = reference_curve[-1]
    monotonic_targets = _monotonic_float(
        target_values,
        reference_curve[0],
        reference_curve[-1],
    )
    native_shadow_slope = (reference_curve[1] - reference_curve[0]) * (
        len(reference_curve) - 1
    )
    native_highlight_slope = (reference_curve[-1] - reference_curve[-2]) * (
        len(reference_curve) - 1
    )
    natural_derivatives = _pchip_derivatives(positions, monotonic_targets)
    target_curve = _pchip_curve(
        tuple(zip(positions, monotonic_targets)),
        len(reference_curve),
        first_derivative=(
            (1.0 - shadow_protection) * natural_derivatives[0]
            + shadow_protection * native_shadow_slope
        ),
        last_derivative=(
            (1.0 - highlight_protection) * natural_derivatives[-1]
            + highlight_protection * native_highlight_slope
        ),
    )
    target_curve = _smooth_monotonic_steps(target_curve)
    return tuple(
        target - current for current, target in zip(reference_curve, target_curve)
    )


def _linked_delta_curve(
    zones: Sequence[GrayZone],
    reference_curve: Sequence[float],
    target: dict[int, float],
    config: GammaOptimizationConfig,
) -> tuple[float, ...]:
    anchors = []
    for zone in zones:
        current_output = _clamp(zone.pixel_normalized, 1e-6, 1.0)
        input_value = invert_lut(reference_curve, current_output)
        desired_output = _clamp(10.0 ** (-target[zone.zone]), 1e-6, 1.0)
        anchors.append((input_value, desired_output - evaluate_lut(reference_curve, input_value)))
    return _interpolated_delta(
        anchors,
        reference_curve,
        highlight_protection=config.highlight_protection,
        shadow_protection=config.shadow_protection,
    )


def _independent_delta_curve(
    zones: Sequence[GrayZone],
    curve: Sequence[float],
    target: dict[int, float],
    mean_getter: Callable[[GrayZone], Optional[float]],
    density_getter: Callable[[GrayZone], Optional[float]],
    config: GammaOptimizationConfig,
) -> tuple[float, ...]:
    offsets = [
        density - zone.density
        for zone in zones
        for density in (density_getter(zone),)
        if density is not None
    ]
    density_offset = mean(offsets) if offsets else 0.0
    anchors = []
    for zone in zones:
        channel_mean = mean_getter(zone)
        current_output = _clamp((channel_mean if channel_mean is not None else zone.pixel) / 255.0, 1e-6, 1.0)
        input_value = invert_lut(curve, current_output)
        desired_output = _clamp(10.0 ** (-(target[zone.zone] + density_offset)), 1e-6, 1.0)
        anchors.append((input_value, desired_output - evaluate_lut(curve, input_value)))
    return _interpolated_delta(
        anchors,
        curve,
        highlight_protection=config.highlight_protection,
        shadow_protection=config.shadow_protection,
    )


def _monotonic_float(values: Sequence[float], first: float, last: float) -> tuple[float, ...]:
    output = [_clamp(value, first, last) for value in values]
    output[0] = first
    output[-1] = last
    for index in range(1, len(output) - 1):
        output[index] = max(output[index - 1], min(output[index], last))
    for index in range(len(output) - 2, 0, -1):
        output[index] = min(output[index], output[index + 1])
    return tuple(output)


def _quantize(values: Sequence[float], maximum: int, first: int, last: int) -> tuple[tuple[int, ...], float]:
    quantized = [max(0, min(maximum, int(round(value * maximum)))) for value in values]
    quantized[0] = first
    quantized[-1] = last
    # Prefer a strictly increasing integer LUT whenever the XML range has
    # enough codes.  A merely non-decreasing quantizer can turn a smooth
    # floating-point curve into visible shelves after rounding.
    strict = last - first >= len(quantized) - 1
    if strict:
        for index in range(1, len(quantized) - 1):
            lower = quantized[index - 1] + 1
            upper = last - (len(quantized) - 1 - index)
            quantized[index] = max(lower, min(quantized[index], upper))
    else:
        for index in range(1, len(quantized) - 1):
            quantized[index] = max(quantized[index - 1], min(quantized[index], last))
        for index in range(len(quantized) - 2, 0, -1):
            quantized[index] = min(quantized[index], quantized[index + 1])
    error = max(abs(value - quantized[index] / maximum) for index, value in enumerate(values))
    return tuple(quantized), error


def _curve_naturalness(
    curves: Sequence[Sequence[float]],
) -> tuple[float, float, float]:
    """Return curvature RMS, jerk RMS, and total curvature variation."""

    second_differences = [
        curve[index + 1] - 2.0 * curve[index] + curve[index - 1]
        for curve in curves
        for index in range(1, len(curve) - 1)
    ]
    third_differences = [
        curve[index + 2]
        - 3.0 * curve[index + 1]
        + 3.0 * curve[index]
        - curve[index - 1]
        for curve in curves
        for index in range(1, len(curve) - 2)
    ]
    curvature_rms = (
        math.sqrt(mean(value * value for value in second_differences))
        if second_differences
        else 0.0
    )
    jerk_rms = (
        math.sqrt(mean(value * value for value in third_differences))
        if third_differences
        else 0.0
    )
    curvature_variation = sum(abs(value) for value in second_differences) / max(
        len(curves), 1
    )
    return curvature_rms, jerk_rms, curvature_variation


def _integer_curve_smoothness_key(
    curves: Sequence[Sequence[int]],
) -> tuple[int, int, float, float, float]:
    """Rank visible local jumps before aggregate curvature/jerk energy."""

    maximum_step = max(
        following - current
        for curve in curves
        for current, following in zip(curve, curve[1:])
    )
    maximum_slope_change = max(
        (
            abs(
                (curve[index + 1] - curve[index])
                - (curve[index] - curve[index - 1])
            )
            for curve in curves
            for index in range(1, len(curve) - 1)
        ),
        default=0,
    )
    curvature_rms, jerk_rms, curvature_variation = _curve_naturalness(curves)
    return (
        maximum_step,
        maximum_slope_change,
        curvature_rms,
        jerk_rms,
        curvature_variation,
    )


def _curve_slope_trend(
    curves: Sequence[Sequence[float]],
    *,
    window: int = 7,
) -> tuple[float, float]:
    """Return the worst secondary slope rise and positive slope variation.

    Curvature RMS can be small even when a tone curve first flattens and then
    accelerates over a broad interval.  That broad S-shaped defect is exactly
    what creates a visible brightness compression followed by a catch-up, so
    inspect a seven-point moving average of the LUT steps as a separate shape
    invariant.
    """

    worst_secondary_rise = 0.0
    worst_positive_variation = 0.0
    radius = max(0, window // 2)
    for curve in curves:
        steps = [
            following - current
            for current, following in zip(curve, curve[1:])
        ]
        if len(steps) < 2:
            continue
        smoothed = []
        for index in range(len(steps)):
            start = max(0, index - radius)
            stop = min(len(steps), index + radius + 1)
            smoothed.append(mean(steps[start:stop]))
        running_minimum = smoothed[0]
        secondary_rise = 0.0
        for slope in smoothed[1:]:
            running_minimum = min(running_minimum, slope)
            secondary_rise = max(secondary_rise, slope - running_minimum)
        positive_variation = sum(
            max(0.0, following - current)
            for current, following in zip(smoothed, smoothed[1:])
        )
        worst_secondary_rise = max(worst_secondary_rise, secondary_rise)
        worst_positive_variation = max(
            worst_positive_variation,
            positive_variation,
        )
    return worst_secondary_rise, worst_positive_variation


def _refine_quantized_curves(
    dataset: GrayDataset,
    old_curves: tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]],
    initial_curves: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    constraint_zones: tuple[int, ...],
    expected_step_count: int,
    maximum: int,
    config: GammaOptimizationConfig,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """Reduce integer curvature/jerk without weakening gray-step gates.

    Moving one shared code at a time keeps RGB linkage intact.  Every accepted
    move preserves endpoints, strict monotonicity, the exact contiguous stage
    count, and the capture reserve for all required pairs.
    """

    if not initial_curves or len(initial_curves[0]) < 5:
        return initial_curves
    if not all(
        curve[-1] - curve[0] >= len(curve) - 1 for curve in initial_curves
    ):
        return initial_curves

    curves = [list(curve) for curve in initial_curves]
    reference = [list(curve) for curve in initial_curves]
    old_reference = _average_curves(*old_curves)
    ordered_zones = tuple(sorted(dataset.zones, key=lambda zone: zone.zone))
    sample_positions = tuple(
        (zone.zone, invert_lut(old_reference, zone.pixel_normalized))
        for zone in ordered_zones
    )
    required_zone_pairs = tuple(zip(constraint_zones, constraint_zones[1:]))
    minimum_gap = minimum_continuity_gap(config.threshold)

    def modeled_pixels() -> tuple[tuple[int, float], ...]:
        reference_codes = _average_curves(*curves)
        return tuple(
            (
                zone,
                255.0 * evaluate_lut(reference_codes, position) / maximum,
            )
            for zone, position in sample_positions
        )

    def gates_are_kept() -> bool:
        values = modeled_pixels()
        by_zone = dict(values)
        if any(
            by_zone[current] - by_zone[following] < minimum_gap
            for current, following in required_zone_pairs
        ):
            return False
        return (
            analyze_pixel_values(
                values,
                config.threshold,
                middle_gray_zone=dataset.middle_gray_zone,
            ).effective_count
            == expected_step_count
        )

    def objective() -> float:
        curvature = 0.0
        jerk = 0.0
        deviation = 0.0
        for curve, original in zip(curves, reference):
            second = [
                curve[index + 1] - 2.0 * curve[index] + curve[index - 1]
                for index in range(1, len(curve) - 1)
            ]
            curvature += sum(value * value for value in second)
            jerk += sum(
                (following - current) ** 2
                for current, following in zip(second, second[1:])
            )
            deviation += sum(
                (value - baseline) ** 2
                for value, baseline in zip(curve, original)
            )
        return curvature + 0.35 * jerk + 0.003 * deviation

    current_score = objective()
    shadow_edge = round(len(curves[0]) * 0.05 * config.shadow_protection)
    highlight_edge = round(len(curves[0]) * 0.05 * config.highlight_protection)
    start = max(1, shadow_edge)
    stop = min(len(curves[0]) - 1, len(curves[0]) - highlight_edge)
    for _iteration in range(min(96, len(curves[0]))):
        best_move: Optional[tuple[float, int, int]] = None
        for index in range(start, stop):
            for shift in (-1, 1):
                if any(
                    not (
                        curve[index - 1]
                        < curve[index] + shift
                        < curve[index + 1]
                    )
                    for curve in curves
                ):
                    continue
                for curve in curves:
                    curve[index] += shift
                if gates_are_kept():
                    score = objective()
                    if score < current_score - 1e-9 and (
                        best_move is None or score < best_move[0]
                    ):
                        best_move = (score, index, shift)
                for curve in curves:
                    curve[index] -= shift
        if best_move is None:
            break
        current_score, index, shift = best_move
        for curve in curves:
            curve[index] += shift
    return tuple(tuple(curve) for curve in curves)  # type: ignore[return-value]


def _redistribute_high_stage_slopes(
    dataset: GrayDataset,
    old_curves: tuple[
        tuple[float, ...],
        tuple[float, ...],
        tuple[float, ...],
    ],
    initial_curves: tuple[
        tuple[int, ...],
        tuple[int, ...],
        tuple[int, ...],
    ],
    constraint_zones: tuple[int, ...],
    expected_step_count: int,
    target_pixel_gap: float,
    maximum: int,
    config: GammaOptimizationConfig,
) -> tuple[
    tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    float,
]:
    """Spread a required high-stage contrast lobe over neighboring LUT points.

    The equal-Pixel target fixes the gray-zone spacing, but clustered capture
    inputs can still concentrate the necessary gain into a short LUT interval.
    Project the linked average curve through bounded first/second-difference
    and gray-gap halfspaces, then apply the shared projection delta to R/G/B.
    Final capture and Curve Health gates decide whether this smoother candidate
    is usable; an infeasible projection can therefore never weaken safety.
    """

    length = len(initial_curves[0])
    if length < 5 or len(constraint_zones) < 2:
        return initial_curves, 0.0

    reference_curve = _average_curves(*old_curves)
    seed_curve = _average_curves(*initial_curves)
    positions = {
        zone.zone: invert_lut(reference_curve, zone.pixel_normalized)
        for zone in dataset.zones
    }

    def interpolation_weights(position: float) -> tuple[tuple[int, float], ...]:
        scaled = _clamp(position) * (length - 1)
        low = int(math.floor(scaled))
        high = min(length - 1, low + 1)
        fraction = scaled - low
        if low == high:
            return ((low, 1.0),)
        return ((low, 1.0 - fraction), (high, fraction))

    def subtract_weights(
        first: Sequence[tuple[int, float]],
        second: Sequence[tuple[int, float]],
    ) -> tuple[tuple[int, float], ...]:
        combined = dict(first)
        for index, value in second:
            combined[index] = combined.get(index, 0.0) - value
        return tuple(
            (index, value)
            for index, value in combined.items()
            if abs(value) > 1e-15
        )

    def negate(
        coefficients: Sequence[tuple[int, float]],
    ) -> tuple[tuple[int, float], ...]:
        return tuple((index, -value) for index, value in coefficients)

    constraints: list[tuple[tuple[tuple[int, float], ...], float]] = []
    nominal_step = maximum / max(1, length - 1)
    strict_possible = all(
        curve[-1] - curve[0] >= length - 1 for curve in initial_curves
    )
    minimum_code_step = 1.01 if strict_possible else 0.0
    # A 17-code floating ceiling may round to 18 codes.  That is close to the
    # mathematical minimum for the captured Zone 15/16 spacing, while avoiding
    # the previous 19-code peak.  The second-difference bound spreads both
    # sides of that peak and normally reduces integer Δslope from 3 to 2.
    maximum_code_step = max(minimum_code_step + 1.0, nominal_step * 4.25)
    curvature_bound = max(1.25, nominal_step * 0.4375)
    for index in range(length - 1):
        step = ((index + 1, 1.0), (index, -1.0))
        constraints.append((step, minimum_code_step))
        constraints.append((negate(step), -maximum_code_step))
    for index in range(1, length - 1):
        second_difference = (
            (index + 1, 1.0),
            (index, -2.0),
            (index - 1, 1.0),
        )
        constraints.append((second_difference, -curvature_bound))
        constraints.append((negate(second_difference), -curvature_bound))

    minimum_gap = minimum_continuity_gap(config.threshold)
    lower_pixel_gap = max(minimum_gap + 0.50, target_pixel_gap - 1.50)
    upper_pixel_gap = max(lower_pixel_gap + 1.0, target_pixel_gap + 0.75)
    pixel_to_code = maximum / 255.0
    for current, following in zip(constraint_zones, constraint_zones[1:]):
        gap = subtract_weights(
            interpolation_weights(positions[current]),
            interpolation_weights(positions[following]),
        )
        constraints.append((gap, lower_pixel_gap * pixel_to_code))
        constraints.append((negate(gap), -upper_pixel_gap * pixel_to_code))

    ordered_zone_ids = [
        zone.zone for zone in sorted(dataset.zones, key=lambda item: item.zone)
    ]
    boundary_index = ordered_zone_ids.index(constraint_zones[-1])
    if boundary_index + 1 < len(ordered_zone_ids):
        boundary_zone = ordered_zone_ids[boundary_index + 1]
        boundary_gap = subtract_weights(
            interpolation_weights(positions[constraint_zones[-1]]),
            interpolation_weights(positions[boundary_zone]),
        )
        guard_limit = max(0.5, config.threshold - 2.50) * pixel_to_code
        constraints.append((negate(boundary_gap), -guard_limit))

    values = list(seed_curve)
    first_value = values[0]
    last_value = values[-1]
    corrections = [0.0] * len(constraints)
    iterations = min(2000, max(800, 8 * length))
    for iteration in range(iterations):
        for constraint_index, (coefficients, lower_bound) in enumerate(
            constraints
        ):
            correction = corrections[constraint_index]
            denominator = 0.0
            dot_product = 0.0
            for index, coefficient in coefficients:
                if index == 0:
                    projected_value = first_value
                elif index == length - 1:
                    projected_value = last_value
                else:
                    projected_value = values[index] + correction * coefficient
                    denominator += coefficient * coefficient
                dot_product += projected_value * coefficient
            adjustment = (
                max(0.0, (lower_bound - dot_product) / denominator)
                if denominator > 0.0
                else 0.0
            )
            for index, coefficient in coefficients:
                if index not in (0, length - 1):
                    values[index] += (correction + adjustment) * coefficient
            corrections[constraint_index] = -adjustment
        values[0] = first_value
        values[-1] = last_value
        if iteration >= 499 and (iteration + 1) % 250 == 0:
            maximum_violation = max(
                max(
                    0.0,
                    lower_bound
                    - sum(values[index] * coefficient for index, coefficient in coefficients),
                )
                for coefficients, lower_bound in constraints
            )
            if maximum_violation <= 0.02:
                break

    shared_delta = tuple(
        projected - original for projected, original in zip(values, seed_curve)
    )
    projected_float_curves = tuple(
        _monotonic_float(
            [
                (value + delta) / maximum
                for value, delta in zip(curve, shared_delta)
            ],
            curve[0] / maximum,
            curve[-1] / maximum,
        )
        for curve in initial_curves
    )
    quantized_with_errors = tuple(
        _quantize(
            curve,
            maximum,
            initial_curves[index][0],
            initial_curves[index][-1],
        )
        for index, curve in enumerate(projected_float_curves)
    )
    projected_integer = tuple(item[0] for item in quantized_with_errors)
    quantization_error = max(item[1] for item in quantized_with_errors)
    projected_integer = _refine_quantized_curves(
        dataset,
        old_curves,
        projected_integer,  # type: ignore[arg-type]
        constraint_zones,
        expected_step_count,
        maximum,
        config,
    )
    return projected_integer, quantization_error  # type: ignore[return-value]


def _project_shape_preserving_curves(
    dataset: GrayDataset,
    reference_curve: Sequence[float],
    initial_curves: tuple[
        tuple[float, ...],
        tuple[float, ...],
        tuple[float, ...],
    ],
    constraint_zones: tuple[int, ...],
    maximum: int,
    config: GammaOptimizationConfig,
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    """Project a fallback candidate into a smooth concave feasible set.

    The ordinary PCHIP path gives the best target fit in most cases.  Close to
    the dark-end stage boundary, however, it can satisfy the requested gaps by
    flattening and then accelerating.  This fallback uses Dykstra projections
    over strict monotonicity, concavity, required contiguous Pixel gaps, and
    the exact-run boundary.  The integer refiner then removes quantization
    noise without giving any of those gates back.
    """

    length = len(reference_curve)
    if length < 3:
        return initial_curves

    def interpolation_weights(position: float) -> tuple[tuple[int, float], ...]:
        scaled = _clamp(position) * (length - 1)
        low = int(math.floor(scaled))
        high = min(length - 1, low + 1)
        fraction = scaled - low
        if low == high:
            return ((low, 1.0),)
        return ((low, 1.0 - fraction), (high, fraction))

    def subtract_weights(
        first: Sequence[tuple[int, float]],
        second: Sequence[tuple[int, float]],
    ) -> tuple[tuple[int, float], ...]:
        combined = dict(first)
        for index, value in second:
            combined[index] = combined.get(index, 0.0) - value
        return tuple(
            (index, value)
            for index, value in combined.items()
            if abs(value) > 1e-15
        )

    constraints: list[tuple[tuple[tuple[int, float], ...], float]] = []
    strict_possible = all(
        (curve[-1] - curve[0]) * maximum >= length - 1
        for curve in initial_curves
    )
    minimum_code_step = 1.02 if strict_possible else 0.0
    for index in range(length - 1):
        constraints.append(
            (((index + 1, 1.0), (index, -1.0)), minimum_code_step)
        )
    for index in range(1, length - 1):
        constraints.append(
            (
                (
                    (index, 2.0),
                    (index - 1, -1.0),
                    (index + 1, -1.0),
                ),
                0.0,
            )
        )

    positions = {
        zone.zone: invert_lut(reference_curve, zone.pixel_normalized)
        for zone in dataset.zones
    }
    projected_gap = (
        minimum_continuity_gap(config.threshold) + 0.25
    ) * maximum / 255.0
    for current, following in zip(constraint_zones, constraint_zones[1:]):
        constraints.append(
            (
                subtract_weights(
                    interpolation_weights(positions[current]),
                    interpolation_weights(positions[following]),
                ),
                projected_gap,
            )
        )

    # The first pair after the requested run must remain below the recognition
    # threshold; otherwise an exact 13-stage request can silently become 14+.
    ordered_zones = [zone.zone for zone in sorted(dataset.zones, key=lambda item: item.zone)]
    boundary_index = ordered_zones.index(constraint_zones[-1])
    if boundary_index + 1 < len(ordered_zones):
        boundary_zone = ordered_zones[boundary_index + 1]
        boundary = subtract_weights(
            interpolation_weights(positions[constraint_zones[-1]]),
            interpolation_weights(positions[boundary_zone]),
        )
        boundary_limit = max(0.0, config.threshold - 0.20) * maximum / 255.0
        constraints.append(
            (tuple((index, -value) for index, value in boundary), -boundary_limit)
        )

    iterations = min(4000, max(900, 16 * length))
    preview_iterations = min(300, iterations)
    nominal_step = maximum / max(1, length - 1)
    feasibility_limit = max(0.65, nominal_step * 0.20)
    cache: dict[tuple[float, ...], tuple[float, ...]] = {}

    def project(initial: tuple[float, ...]) -> tuple[float, ...]:
        if initial in cache:
            return cache[initial]
        values = [value * maximum for value in initial]
        first_value = values[0]
        last_value = values[-1]
        corrections = [0.0] * len(constraints)
        for iteration in range(iterations):
            for constraint_index, (coefficients, lower_bound) in enumerate(
                constraints
            ):
                correction = corrections[constraint_index]
                denominator = 0.0
                dot_product = 0.0
                for index, coefficient in coefficients:
                    if index == 0:
                        projected_value = first_value
                    elif index == length - 1:
                        projected_value = last_value
                    else:
                        projected_value = values[index] + correction * coefficient
                        denominator += coefficient * coefficient
                    dot_product += projected_value * coefficient
                adjustment = (
                    max(0.0, (lower_bound - dot_product) / denominator)
                    if denominator > 0.0
                    else 0.0
                )
                for index, coefficient in coefficients:
                    if index not in (0, length - 1):
                        values[index] += (correction + adjustment) * coefficient
                corrections[constraint_index] = -adjustment
            values[0] = first_value
            values[-1] = last_value
            if iteration + 1 == preview_iterations:
                maximum_violation = max(
                    max(
                        0.0,
                        lower_bound
                        - sum(
                            values[index] * coefficient
                            for index, coefficient in coefficients
                        ),
                    )
                    for coefficients, lower_bound in constraints
                )
                # Clearly incompatible concavity/gap systems converge very
                # slowly toward a large non-zero residual.  Abort those early
                # so a 15→13 safety search remains interactive.
                if maximum_violation > feasibility_limit:
                    cache[initial] = initial
                    return initial
        output = tuple(
            _clamp(value / maximum, initial[0], initial[-1]) for value in values
        )
        cache[initial] = output
        return output

    return tuple(project(curve) for curve in initial_curves)  # type: ignore[return-value]


def _simulate_channel(old_curve: Sequence[float], new_curve: Sequence[float], output: float) -> float:
    return evaluate_lut(new_curve, invert_lut(old_curve, _clamp(output)))


def _local_gamma(exposures: Sequence[float], densities: Sequence[float]) -> tuple[Optional[float], ...]:
    values: list[Optional[float]] = []
    for index in range(len(densities)):
        if index == len(densities) - 1:
            values.append(None)
            continue
        delta_exposure = exposures[index + 1] - exposures[index]
        values.append(None if abs(delta_exposure) <= 1e-12 else (densities[index + 1] - densities[index]) / delta_exposure)
    return tuple(values)


def _curve_health(
    before: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    after: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    quantization_error: float,
    maximum: int,
    *,
    enforce_naturalness: bool = False,
    allow_smooth_inflection: bool = False,
) -> GammaCurveHealth:
    reversal_count = sum(
        following < current
        for curve in after
        for current, following in zip(curve, curve[1:])
    )
    plateau_count = sum(
        following == current
        for curve in after
        for current, following in zip(curve, curve[1:])
    )
    strict_monotonicity_possible = all(
        curve[-1] - curve[0] >= len(curve) - 1 for curve in after
    )
    maximum_jump = max(following - current for curve in after for current, following in zip(curve, curve[1:]))
    original_jump = max(following - current for curve in before for current, following in zip(curve, curve[1:]))
    maximum_slope_change = max(
        (
            abs((curve[index + 1] - curve[index]) - (curve[index] - curve[index - 1]))
            for curve in after
            for index in range(1, len(curve) - 1)
        ),
        default=0,
    )
    original_slope_change = max(
        (
            abs((curve[index + 1] - curve[index]) - (curve[index] - curve[index - 1]))
            for curve in before
            for index in range(1, len(curve) - 1)
        ),
        default=0,
    )
    endpoint_ok = all(
        candidate[0] == original[0] and candidate[-1] == original[-1]
        for original, candidate in zip(before, after)
    )
    bounds_ok = all(0 <= value <= maximum for curve in after for value in curve)
    jump_warning = max(32, int(original_jump * 1.8) + 1)
    slope_change_limit = max(
        4,
        int(math.ceil(maximum / max(1, len(after[0]) - 1))) + 2,
        original_slope_change + 3,
    )
    curvature_rms, jerk_rms, curvature_variation = _curve_naturalness(after)
    original_curvature_rms, original_jerk_rms, _original_variation = (
        _curve_naturalness(before)
    )
    nominal_step = maximum / max(1, len(after[0]) - 1)
    curvature_rms_limit = max(original_curvature_rms * 1.10, nominal_step * 0.20)
    jerk_rms_limit = max(original_jerk_rms * 1.02, nominal_step * 0.25)
    naturally_smooth = (
        curvature_rms <= curvature_rms_limit + 1e-12
        and jerk_rms <= jerk_rms_limit + 1e-12
    )
    secondary_slope_rise, positive_slope_variation = _curve_slope_trend(after)
    original_secondary_rise, original_positive_variation = _curve_slope_trend(
        before
    )
    # Keep quantization noise from causing false failures, but allow only a
    # small fraction of one nominal LUT step to be added as a new broad-scale
    # acceleration.  The companion accumulated-variation gate catches several
    # smaller re-accelerations spread across the curve.
    secondary_rise_limit = max(
        original_secondary_rise + 0.75,
        nominal_step * 0.40,
    )
    positive_variation_limit = max(
        original_positive_variation + 2.50,
        nominal_step * 1.75,
    )
    strict_shape_preserved = (
        secondary_slope_rise <= secondary_rise_limit + 1e-12
        and positive_slope_variation <= positive_variation_limit + 1e-12
    )
    shape_preserved = strict_shape_preserved
    if allow_smooth_inflection:
        # A high-stage equalization may need one broad contrast lobe where the
        # capture has several nearly identical dark patches.  Permit that
        # intentional inflection only when its local curvature and jerk remain
        # naturally smooth; abrupt platforms/jumps are still rejected by the
        # independent monotonicity, slope-continuity and naturalness gates.
        secondary_rise_limit = max(secondary_rise_limit, nominal_step * 3.55)
        positive_variation_limit = max(
            positive_variation_limit,
            nominal_step * 5.0,
        )
        shape_preserved = (
            secondary_slope_rise <= secondary_rise_limit + 1e-12
            and positive_slope_variation <= positive_variation_limit + 1e-12
            and naturally_smooth
        )
    checks = (
        GammaEngineeringCheck(
            "LUT Monotonicity",
            "PASS" if reversal_count == 0 else "FAIL",
            f"reversals={reversal_count}",
            "0",
            f"{len(after[0])} 点 LUT 必须单调不下降且无局部反转。",
        ),
        GammaEngineeringCheck(
            "LUT Plateaus",
            (
                "PASS"
                if plateau_count == 0
                else "FAIL"
                if strict_monotonicity_possible
                else "WARNING"
            ),
            f"plateaus={plateau_count}",
            "0 when integer range permits",
            "整数范围足够时禁止相邻 LUT 点相等，避免实拍出现平台后突升。",
        ),
        GammaEngineeringCheck(
            "LUT Range / Endpoints",
            "PASS" if bounds_ok and endpoint_ok else "FAIL",
            f"range=0..{maximum}; endpoints={'kept' if endpoint_ok else 'changed'}",
            f"integer 0..{maximum}; keep endpoints",
            "保持 Qualcomm 原始整数范围与首尾点。",
        ),
        GammaEngineeringCheck(
            "LUT Local Jump",
            "PASS" if maximum_jump <= jump_warning else "WARNING" if maximum_jump <= 64 else "FAIL",
            f"max step={maximum_jump}",
            f"PASS≤{jump_warning}; FAIL>64",
            "限制量化后相邻 LUT 点突变。",
        ),
        GammaEngineeringCheck(
            "LUT Slope Continuity",
            "PASS" if maximum_slope_change <= slope_change_limit else "FAIL",
            f"max Δslope={maximum_slope_change}",
            f"≤{slope_change_limit}",
            "限制相邻 LUT 斜率突变，避免平台后陡升或局部折线。",
        ),
        GammaEngineeringCheck(
            "LUT Shape Preservation",
            "PASS" if shape_preserved else "FAIL",
            (
                f"secondary rise={secondary_slope_rise:.3f}; "
                f"positive trend={positive_slope_variation:.3f}"
            ),
            (
                f"rise≤{secondary_rise_limit:.3f}; "
                f"trend≤{positive_variation_limit:.3f}"
            ),
            (
                "高阶均匀化只允许通过自然平滑门禁的单个宽缓拐点；普通模式仍限制"
                "7 点平滑斜率在下降后再次抬升，避免局部亮度压缩后突然追高。"
                if allow_smooth_inflection
                else "限制 7 点平滑斜率在下降后再次抬升；这类 S 形下凹即使仍然单调、"
                "RMS 合格，也会引入局部亮度压缩后突然追高。"
            ),
        ),
        GammaEngineeringCheck(
            "LUT Natural Smoothness",
            (
                "PASS"
                if naturally_smooth
                else "FAIL"
                if enforce_naturalness
                else "WARNING"
            ),
            (
                f"curvature RMS={curvature_rms:.3f}; jerk RMS={jerk_rms:.3f}; "
                f"total variation={curvature_variation:.0f}"
            ),
            f"RMS≤{curvature_rms_limit:.3f}; jerk≤{jerk_rms_limit:.3f}",
            "同时限制二阶曲率与三阶变化，避免局部鼓包造成亮度加速或减速突变。",
        ),
        GammaEngineeringCheck(
            "LUT Quantization",
            "PASS" if quantization_error <= 0.5 / maximum + 1e-12 else "WARNING",
            f"max error={quantization_error:.7f}",
            f"≤{0.5 / maximum:.7f}",
            "检查浮点曲线量化到 XML 整数后的最大误差。",
        ),
    )
    status = "FAIL" if any(check.status == "FAIL" for check in checks) else "WARNING" if any(check.status == "WARNING" for check in checks) else "PASS"
    return GammaCurveHealth(status, checks, reversal_count == 0, reversal_count, maximum_jump, quantization_error)


def _evaluate(
    dataset: GrayDataset,
    selected: tuple[int, ...],
    constraint_zones: tuple[int, ...],
    target_step_count: int,
    target: dict[int, float],
    old_curves: tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]],
    new_curves: tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]],
    integer_before: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    integer_after: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    maximum: int,
    quantization_error: float,
    config: GammaOptimizationConfig,
    *,
    enforce_naturalness: bool = False,
    allow_smooth_inflection: bool = False,
) -> tuple[
    tuple[GammaZoneResult, ...],
    tuple[GammaPairResult, ...],
    GammaMetrics,
    GammaLossBreakdown,
    GammaCurveHealth,
]:
    selected_set = set(selected)
    constraint_set = set(constraint_zones)
    selected_rows = [zone for zone in dataset.zones if zone.zone in selected_set]
    reference_old = _average_curves(*old_curves)
    reference_new = _average_curves(*new_curves)

    before_density: dict[int, float] = {}
    after_density: dict[int, float] = {}
    target_density = target
    rgb_before_values: list[float] = []
    rgb_after_values: list[float] = []
    for zone in dataset.zones:
        before_output = _clamp(zone.pixel_normalized, 1e-9, 1.0)
        after_output = _simulate_channel(reference_old, reference_new, before_output)
        before_density[zone.zone] = -math.log10(before_output)
        after_density[zone.zone] = -math.log10(max(after_output, 1e-9))
        channel_before = []
        channel_after = []
        for old_curve, new_curve, measured in zip(
            old_curves,
            new_curves,
            (zone.mean_r, zone.mean_g, zone.mean_b),
        ):
            channel_value = before_output if measured is None else _clamp(measured / 255.0)
            channel_before.append(channel_value)
            channel_after.append(_simulate_channel(old_curve, new_curve, channel_value))
        rgb_before_values.append(max(channel_before) - min(channel_before))
        rgb_after_values.append(max(channel_after) - min(channel_after))

    exposures = [-zone.log_exposure for zone in dataset.zones]
    local_before = _local_gamma(exposures, [before_density[zone.zone] for zone in dataset.zones])
    local_target = _local_gamma(exposures, [target_density[zone.zone] for zone in dataset.zones])
    local_after = _local_gamma(exposures, [after_density[zone.zone] for zone in dataset.zones])
    zone_results: list[GammaZoneResult] = []
    for index, zone in enumerate(dataset.zones):
        before_error = before_density[zone.zone] - target_density[zone.zone]
        after_error = after_density[zone.zone] - target_density[zone.zone]
        denominator = abs(before_error)
        percent = None if denominator < 0.005 else (denominator - abs(after_error)) / denominator * 100.0
        target_pixel = 255.0 * 10.0 ** (-target_density[zone.zone])
        after_pixel = 255.0 * 10.0 ** (-after_density[zone.zone])
        used = zone.zone in selected_set
        constrained = zone.zone in constraint_set
        zone_results.append(
            GammaZoneResult(
                zone=zone.zone,
                used=used,
                status="FIT" if used else "CONSTRAINT" if constrained else "EXCLUDED",
                pixel_before=zone.pixel,
                pixel_target=target_pixel,
                pixel_after=after_pixel,
                density_before=before_density[zone.zone],
                density_target=target_density[zone.zone],
                density_after=after_density[zone.zone],
                error_before=before_error,
                error_after=after_error,
                improvement_percent=percent,
                local_gamma_before=local_before[index],
                local_gamma_target=local_target[index],
                local_gamma_after=local_after[index],
            )
        )

    fit = [item for item in zone_results if item.used]
    objective = [item for item in zone_results if item.used or item.status == "CONSTRAINT"]
    fit_exposures = [-next(zone.log_exposure for zone in dataset.zones if zone.zone == item.zone) for item in fit]
    global_before, _ = _linear_regression(fit_exposures, [item.density_before for item in fit])
    global_target, _ = _linear_regression(fit_exposures, [item.density_target for item in fit])
    global_after, _ = _linear_regression(fit_exposures, [item.density_after for item in fit])
    rmse_before = math.sqrt(mean(item.error_before ** 2 for item in objective))
    rmse_after = math.sqrt(mean(item.error_after ** 2 for item in objective))
    local_before_values = [
        (item.local_gamma_before, item.local_gamma_target)
        for item in fit
        if item.local_gamma_before is not None and item.local_gamma_target is not None and item.zone != selected[-1]
    ]
    local_after_values = [
        (item.local_gamma_after, item.local_gamma_target)
        for item in fit
        if item.local_gamma_after is not None and item.local_gamma_target is not None and item.zone != selected[-1]
    ]
    local_error_before = mean(abs(value - target_value) for value, target_value in local_before_values) if local_before_values else 0.0
    local_error_after = mean(abs(value - target_value) for value, target_value in local_after_values) if local_after_values else 0.0

    before_analysis = analyze_pixel_values(
        tuple((item.zone, item.pixel_before) for item in zone_results),
        config.threshold,
        middle_gray_zone=dataset.middle_gray_zone,
    )
    after_analysis = analyze_pixel_values(
        tuple((item.zone, item.pixel_after) for item in zone_results),
        config.threshold,
        middle_gray_zone=dataset.middle_gray_zone,
    )
    metrics = GammaMetrics(
        global_gamma_before=global_before,
        global_gamma_target=global_target,
        global_gamma_after=global_after,
        rmse_before=rmse_before,
        rmse_after=rmse_after,
        maximum_error_before=max(abs(item.error_before) for item in objective),
        maximum_error_after=max(abs(item.error_after) for item in objective),
        local_gamma_error_before=local_error_before,
        local_gamma_error_after=local_error_after,
        rgb_gray_deviation_before=mean(rgb_before_values),
        rgb_gray_deviation_after=mean(rgb_after_values),
        distinguishable_before=before_analysis.effective_count,
        distinguishable_after=after_analysis.effective_count,
        distinguishable_target=target_step_count,
    )
    by_zone = {item.zone: item for item in zone_results}
    required_pairs = set(zip(constraint_zones, constraint_zones[1:]))
    pair_results: list[GammaPairResult] = []
    for current_zone, following_zone in zip(dataset.zones, dataset.zones[1:]):
        current = by_zone[current_zone.zone]
        following = by_zone[following_zone.zone]
        delta_before = current.pixel_before - following.pixel_before
        delta_target = current.pixel_target - following.pixel_target
        delta_after = current.pixel_after - following.pixel_after
        required = (current.zone, following.zone) in required_pairs
        pair_results.append(
            GammaPairResult(
                from_zone=current.zone,
                to_zone=following.zone,
                delta_before=delta_before,
                delta_target=delta_target,
                delta_after=delta_after,
                before_distinguishable=delta_before >= config.threshold,
                after_distinguishable=delta_after >= config.threshold,
                target_required=required,
            )
        )
    required_shortfalls = [
        max(0.0, config.threshold - pair.delta_after)
        for pair in pair_results
        if pair.target_required
    ]
    step_separation = (
        math.sqrt(mean(value * value for value in required_shortfalls)) / 255.0
        if required_shortfalls
        else 0.0
    )
    changes = [new - old for old_curve, new_curve in zip(old_curves, new_curves) for old, new in zip(old_curve, new_curve)]
    second_differences = [
        new_curve[index + 1] - 2.0 * new_curve[index] + new_curve[index - 1]
        for new_curve in new_curves
        for index in range(1, len(new_curve) - 1)
    ]
    edge = max(1, round(len(reference_new) * 0.15))
    highlight_change = mean(abs(new - old) for old, new in zip(reference_old[-edge:], reference_new[-edge:]))
    shadow_change = mean(abs(new - old) for old, new in zip(reference_old[:edge], reference_new[:edge]))
    smoothness = math.sqrt(mean(value * value for value in second_differences))
    change = math.sqrt(mean(value * value for value in changes))
    loss = GammaLossBreakdown(
        total=(
            rmse_after
            + 0.65 * local_error_after
            + 0.08 * smoothness
            + 0.12 * change
            + 0.15 * highlight_change
            + 0.15 * shadow_change
            + 0.75 * metrics.rgb_gray_deviation_after
            + 2.5 * step_separation
        ),
        gray_target=rmse_after,
        local_gamma=local_error_after,
        lut_smoothness=smoothness,
        lut_change=change,
        highlight=highlight_change,
        shadow=shadow_change,
        rgb_bias=metrics.rgb_gray_deviation_after,
        step_separation=step_separation,
    )
    health = _curve_health(
        integer_before,
        integer_after,
        quantization_error,
        maximum,
        enforce_naturalness=enforce_naturalness,
        allow_smooth_inflection=allow_smooth_inflection,
    )
    return tuple(zone_results), tuple(pair_results), metrics, loss, health


def _build_gamma_diagnostics(
    dataset: GrayDataset,
    metrics: GammaMetrics,
    health: GammaCurveHealth,
    pairs: Sequence[GammaPairResult],
) -> tuple[GammaModuleDiagnosis, ...]:
    def bounded(value: float) -> float:
        return max(0.0, min(0.99, value))

    def severity(confidence: float) -> str:
        return "HIGH" if confidence >= 0.70 else "MEDIUM" if confidence >= 0.40 else "LOW"

    failed_pairs = [pair for pair in pairs if not pair.before_distinguishable]
    remaining_pairs = [pair for pair in pairs if pair.target_required and not pair.after_distinguishable]
    tail = list(dataset.zones[-max(3, len(dataset.zones) // 4) :])
    noise_values = [zone.noise for zone in tail if zone.noise is not None]
    tail_noise = mean(noise_values) if noise_values else 0.0
    clipped = [zone for zone in dataset.zones if zone.pixel >= 250.0 or zone.pixel <= 1.0]
    gamma_conf = bounded(metrics.rmse_before / 0.12 + metrics.local_gamma_error_before / 0.30)
    step_conf = bounded(len(failed_pairs) / max(len(pairs), 1) * 2.0)
    noise_conf = bounded(tail_noise / 8.0)
    awb_conf = bounded(metrics.rgb_gray_deviation_before / 0.08)
    exposure_conf = bounded(len(clipped) / 3.0)
    curve_conf = 0.15 if health.status == "PASS" else 0.55 if health.status == "WARNING" else 0.95
    return (
        GammaModuleDiagnosis(
            "Gamma",
            gamma_conf,
            severity(gamma_conf),
            f"Global Gamma {metrics.global_gamma_before:.3f}→{metrics.global_gamma_after:.3f}，"
            f"目标响应 {metrics.global_gamma_target:.3f}。",
            (
                f"RMSE {metrics.rmse_before:.5f}→{metrics.rmse_after:.5f}",
                f"Local Gamma error {metrics.local_gamma_error_before:.5f}→{metrics.local_gamma_error_after:.5f}",
            ),
            "优先用当前 Region Gamma LUT 修正连续灰阶响应，再复拍 Stepchart 验证。",
        ),
        GammaModuleDiagnosis(
            "Gray Step Separation",
            step_conf,
            severity(step_conf),
            f"Before 连续可识别 {metrics.distinguishable_before} 阶，After {metrics.distinguishable_after} 阶，"
            f"目标 {metrics.distinguishable_target} 阶。",
            tuple(
                f"Zone {pair.from_zone}→{pair.to_zone}: ΔPixel {pair.delta_before:.2f}→{pair.delta_after:.2f}"
                for pair in (remaining_pairs or failed_pairs)[:8]
            ),
            "若目标仍未达到，提高最大调整强度；若暗部噪声同步上升，转交 Black Level/Noise 调整。",
        ),
        GammaModuleDiagnosis(
            "Exposure / AEC",
            exposure_conf,
            severity(exposure_conf),
            f"检测到 {len(clipped)} 个接近 0/255 的剪切 Zone。",
            tuple(f"Zone {zone.zone}: Pixel={zone.pixel:.1f}" for zone in clipped[:6]),
            "先排除过曝/欠曝和 AEC 波动；剪切数据不应由 Gamma LUT 强行恢复。",
        ),
        GammaModuleDiagnosis(
            "Black Level / Noise",
            noise_conf,
            severity(noise_conf),
            f"暗部平均 Noise={tail_noise:.3f}，不可区分相邻阶={len(failed_pairs)}。",
            tuple(
                f"Zone {zone.zone}: Pixel={zone.pixel:.1f}, Noise={'N/A' if zone.noise is None else f'{zone.noise:.3f}'}"
                for zone in tail
            ),
            "暗部阶数不足且噪声高时，先检查 BLS、denoise 与曝光，不继续抬升 Gamma。",
        ),
        GammaModuleDiagnosis(
            "AWB / RGB Neutrality",
            awb_conf,
            severity(awb_conf),
            f"RGB 灰阶偏差 {metrics.rgb_gray_deviation_before:.5f}→{metrics.rgb_gray_deviation_after:.5f}。",
            (f"RGB mode keeps bias gate; Curve Health={health.status}",),
            "默认保持 RGB 联动；仅在通道响应数据完整且偏色可解释时使用独立模式。",
        ),
        GammaModuleDiagnosis(
            "Gamma LUT / Quantization",
            curve_conf,
            severity(curve_conf),
            f"Curve Health={health.status}，单调={health.monotonic}，reversal={health.reversal_count}。",
            (
                f"Maximum jump={health.maximum_jump}",
                f"Quantization error={health.quantization_error:.7f}",
            ),
            "只有单调、端点、范围与量化检查通过时才允许写回 XML。",
        ),
    )


def _optimize_gamma_lut_exact(
    dataset: GrayDataset,
    region: GammaRegion,
    analysis: GrayRangeAnalysis,
    *,
    config: Optional[GammaOptimizationConfig] = None,
) -> GammaOptimizationResult:
    selected_config = config or GammaOptimizationConfig(threshold=analysis.threshold)
    selected_config.validate()
    if region.length < 2:
        raise GammaOptimizationError(f"Gamma LUT 至少需要 2 点，实际为 {region.length} 点。")
    if len({len(region.channel_r), len(region.channel_g), len(region.channel_b)}) != 1:
        raise GammaOptimizationError("当前 Region 的 R/G/B Gamma LUT 点数不一致。")
    if region.maximum <= 0:
        raise GammaOptimizationError("Gamma LUT 最大值必须大于 0。")
    selected = select_fit_zones(
        dataset,
        analysis,
        mode=selected_config.range_mode,
        manual_start=selected_config.manual_start_zone,
        manual_end=selected_config.manual_end_zone,
    )
    target, constraint_zones, target_step_count = _target_profile(
        dataset,
        analysis,
        selected_config,
    )
    anchor_set = set(selected).union(constraint_zones)
    zones = [zone for zone in dataset.zones if zone.zone in anchor_set]
    integer_before = (region.channel_r, region.channel_g, region.channel_b)
    old_curves = tuple(_normalized(curve, region.maximum) for curve in integer_before)
    reference = _average_curves(*old_curves)
    linked_delta = _linked_delta_curve(zones, reference, target, selected_config)
    if selected_config.rgb_mode == "linked":
        deltas = (linked_delta, linked_delta, linked_delta)
    else:
        independent = (
            _independent_delta_curve(zones, old_curves[0], target, lambda zone: zone.mean_r, lambda zone: zone.density_r, selected_config),
            _independent_delta_curve(zones, old_curves[1], target, lambda zone: zone.mean_g, lambda zone: zone.density_g, selected_config),
            _independent_delta_curve(zones, old_curves[2], target, lambda zone: zone.mean_b, lambda zone: zone.density_b, selected_config),
        )
        # Keep an explicit RGB-bias guard even in advanced mode: most of the
        # luminance correction stays linked and only a small residual may vary.
        deltas = tuple(
            tuple(0.75 * linked + 0.25 * separate for linked, separate in zip(linked_delta, channel))
            for channel in independent
        )

    baseline_zone_results, baseline_pair_results, baseline_metrics, baseline_loss, baseline_health = _evaluate(
        dataset,
        selected,
        constraint_zones,
        target_step_count,
        target,
        old_curves,  # type: ignore[arg-type]
        old_curves,  # type: ignore[arg-type]
        integer_before,
        integer_before,
        region.maximum,
        0.0,
        selected_config,
    )
    best = None
    shape_rejected = False
    allow_smooth_inflection = False
    uniform_profile_used = False
    maximum_strength = selected_config.maximum_adjustment
    strengths = tuple(
        maximum_strength * fraction / 100.0
        for fraction in range(20, 101)
    )
    for strength in strengths:
        float_curves = tuple(
            _monotonic_float(
                [old + strength * delta for old, delta in zip(old_curve, delta_curve)],
                old_curve[0],
                old_curve[-1],
            )
            for old_curve, delta_curve in zip(old_curves, deltas)
        )
        quantized_with_errors = tuple(
            _quantize(curve, region.maximum, integer_before[index][0], integer_before[index][-1])
            for index, curve in enumerate(float_curves)
        )
        integer_after = tuple(item[0] for item in quantized_with_errors)
        quantization_error = max(item[1] for item in quantized_with_errors)
        new_curves = tuple(_normalized(curve, region.maximum) for curve in integer_after)
        zone_results, pair_results, metrics, loss, health = _evaluate(
            dataset,
            selected,
            constraint_zones,
            target_step_count,
            target,
            old_curves,  # type: ignore[arg-type]
            new_curves,  # type: ignore[arg-type]
            integer_before,
            integer_after,  # type: ignore[arg-type]
            region.maximum,
            quantization_error,
            selected_config,
        )
        selected_results = [item for item in zone_results if item.used or item.status == "CONSTRAINT"]
        shape_check = next(
            (
                check
                for check in health.checks
                if check.name == "LUT Shape Preservation"
            ),
            None,
        )
        if shape_check is not None and shape_check.status == "FAIL":
            shape_rejected = True
        if health.status == "FAIL":
            continue
        required_after = [pair for pair in pair_results if pair.target_required]
        # A requested stage count means one contiguous run, never the sum of
        # isolated valid pairs.  Keep a small engineering reserve so LUT
        # interpolation/quantization and capture noise do not leave the final
        # two pairs sitting exactly on the UI threshold.
        if any(
            pair.delta_after < minimum_continuity_gap(selected_config.threshold)
            for pair in required_after
        ):
            continue
        if metrics.distinguishable_after < target_step_count:
            continue
        if (
            selected_config.target_step_count is not None
            and metrics.distinguishable_after != target_step_count
        ):
            continue
        if metrics.rmse_after >= baseline_metrics.rmse_before - 1e-7:
            continue
        if metrics.local_gamma_error_after > baseline_metrics.local_gamma_error_before + 0.003:
            continue
        if metrics.maximum_error_after > baseline_metrics.maximum_error_before + 0.003:
            continue
        if (
            selected_config.rgb_mode == "independent"
            and metrics.rgb_gray_deviation_after > baseline_metrics.rgb_gray_deviation_before + 0.002
        ):
            continue
        if any(abs(item.error_after) > abs(item.error_before) + 0.005 for item in selected_results):
            continue
        candidate = (
            metrics.distinguishable_after - target_step_count,
            loss.lut_smoothness,
            loss.total,
            strength,
            quantization_error,
            integer_after,
            new_curves,
            zone_results,
            pair_results,
            metrics,
            loss,
            health,
        )
        if best is None or candidate[:3] < best[:3]:
            best = candidate

    # A narrow cluster of almost identical dark patches cannot be repaired by
    # changing only the failed pairs without creating a local hump.  If the
    # ordinary target misses by at least two stages, redistribute the complete
    # requested response into equal Pixel intervals and fit one broad warp.
    # The result must still pass strict monotonicity, exact-run, capture reserve,
    # curvature, jerk, regression and RGB gates before it can be selected.
    if (
        best is None
        and maximum_strength > 0.0
        and target_step_count >= baseline_metrics.distinguishable_before + 2
    ):
        try:
            (
                uniform_target,
                uniform_fit_target,
                uniform_constraints,
                uniform_anchor_ids,
            ) = _uniform_step_targets(
                dataset,
                analysis,
                selected_config,
                target_step_count,
            )
        except GammaOptimizationError:
            uniform_target = None

        if uniform_target is not None:
            uniform_anchor_set = set(uniform_anchor_ids)
            uniform_zones = [
                zone for zone in dataset.zones if zone.zone in uniform_anchor_set
            ]
            uniform_linked_delta = _linked_delta_curve(
                uniform_zones,
                reference,
                uniform_fit_target,
                selected_config,
            )
            if selected_config.rgb_mode == "linked":
                uniform_deltas = (
                    uniform_linked_delta,
                    uniform_linked_delta,
                    uniform_linked_delta,
                )
            else:
                uniform_independent = (
                    _independent_delta_curve(
                        uniform_zones,
                        old_curves[0],
                        uniform_fit_target,
                        lambda zone: zone.mean_r,
                        lambda zone: zone.density_r,
                        selected_config,
                    ),
                    _independent_delta_curve(
                        uniform_zones,
                        old_curves[1],
                        uniform_fit_target,
                        lambda zone: zone.mean_g,
                        lambda zone: zone.density_g,
                        selected_config,
                    ),
                    _independent_delta_curve(
                        uniform_zones,
                        old_curves[2],
                        uniform_fit_target,
                        lambda zone: zone.mean_b,
                        lambda zone: zone.density_b,
                        selected_config,
                    ),
                )
                uniform_deltas = tuple(
                    tuple(
                        0.75 * linked + 0.25 * separate
                        for linked, separate in zip(
                            uniform_linked_delta,
                            channel,
                        )
                    )
                    for channel in uniform_independent
                )

            strength = maximum_strength
            uniform_float_curves = tuple(
                _monotonic_float(
                    [
                        old + strength * delta
                        for old, delta in zip(old_curve, delta_curve)
                    ],
                    old_curve[0],
                    old_curve[-1],
                )
                for old_curve, delta_curve in zip(old_curves, uniform_deltas)
            )
            uniform_quantized = tuple(
                _quantize(
                    curve,
                    region.maximum,
                    integer_before[index][0],
                    integer_before[index][-1],
                )
                for index, curve in enumerate(uniform_float_curves)
            )
            uniform_integer = tuple(item[0] for item in uniform_quantized)
            uniform_quantization_error = max(item[1] for item in uniform_quantized)
            uniform_integer = _refine_quantized_curves(
                dataset,
                old_curves,  # type: ignore[arg-type]
                uniform_integer,  # type: ignore[arg-type]
                uniform_constraints,
                target_step_count,
                region.maximum,
                selected_config,
            )
            uniform_curves = tuple(
                _normalized(curve, region.maximum) for curve in uniform_integer
            )
            (
                uniform_baseline_zones,
                uniform_baseline_pairs,
                uniform_baseline_metrics,
                uniform_baseline_loss,
                uniform_baseline_health,
            ) = _evaluate(
                dataset,
                selected,
                uniform_constraints,
                target_step_count,
                uniform_target,
                old_curves,  # type: ignore[arg-type]
                old_curves,  # type: ignore[arg-type]
                integer_before,
                integer_before,
                region.maximum,
                0.0,
                selected_config,
            )
            (
                uniform_zone_results,
                uniform_pair_results,
                uniform_metrics,
                uniform_loss,
                uniform_health,
            ) = _evaluate(
                dataset,
                selected,
                uniform_constraints,
                target_step_count,
                uniform_target,
                old_curves,  # type: ignore[arg-type]
                uniform_curves,  # type: ignore[arg-type]
                integer_before,
                uniform_integer,  # type: ignore[arg-type]
                region.maximum,
                uniform_quantization_error,
                selected_config,
                enforce_naturalness=True,
                allow_smooth_inflection=True,
            )

            def uniform_candidate_is_safe(
                zone_results: Sequence[GammaZoneResult],
                pair_results: Sequence[GammaPairResult],
                metrics: GammaMetrics,
                health: GammaCurveHealth,
            ) -> bool:
                required = [pair for pair in pair_results if pair.target_required]
                selected_results = [
                    item
                    for item in zone_results
                    if item.used or item.status == "CONSTRAINT"
                ]
                gaps = [pair.delta_after for pair in required]
                return bool(gaps) and (
                    health.status == "PASS"
                    and metrics.distinguishable_after == target_step_count
                    and all(
                        gap >= minimum_continuity_gap(selected_config.threshold)
                        for gap in gaps
                    )
                    and max(gaps) - min(gaps)
                    <= max(3.0, selected_config.threshold * 0.40)
                    and metrics.rmse_after
                    < uniform_baseline_metrics.rmse_before - 1e-7
                    and metrics.local_gamma_error_after
                    <= uniform_baseline_metrics.local_gamma_error_before + 0.003
                    and metrics.maximum_error_after
                    <= uniform_baseline_metrics.maximum_error_before + 0.003
                    and (
                        selected_config.rgb_mode != "independent"
                        or metrics.rgb_gray_deviation_after
                        <= uniform_baseline_metrics.rgb_gray_deviation_before
                        + 0.002
                    )
                    and not any(
                        abs(item.error_after) > abs(item.error_before) + 0.005
                        for item in selected_results
                    )
                )

            uniform_is_safe = uniform_candidate_is_safe(
                uniform_zone_results,
                uniform_pair_results,
                uniform_metrics,
                uniform_health,
            )
            uniform_required = [
                pair for pair in uniform_pair_results if pair.target_required
            ]
            if uniform_required:
                balanced_integer, balanced_quantization_error = (
                    _redistribute_high_stage_slopes(
                        dataset,
                        old_curves,  # type: ignore[arg-type]
                        uniform_integer,  # type: ignore[arg-type]
                        uniform_constraints,
                        target_step_count,
                        mean(pair.delta_target for pair in uniform_required),
                        region.maximum,
                        selected_config,
                    )
                )
                if balanced_integer != uniform_integer:
                    balanced_curves = tuple(
                        _normalized(curve, region.maximum)
                        for curve in balanced_integer
                    )
                    (
                        balanced_zone_results,
                        balanced_pair_results,
                        balanced_metrics,
                        balanced_loss,
                        balanced_health,
                    ) = _evaluate(
                        dataset,
                        selected,
                        uniform_constraints,
                        target_step_count,
                        uniform_target,
                        old_curves,  # type: ignore[arg-type]
                        balanced_curves,  # type: ignore[arg-type]
                        integer_before,
                        balanced_integer,
                        region.maximum,
                        balanced_quantization_error,
                        selected_config,
                        enforce_naturalness=True,
                        allow_smooth_inflection=True,
                    )
                    balanced_is_safe = uniform_candidate_is_safe(
                        balanced_zone_results,
                        balanced_pair_results,
                        balanced_metrics,
                        balanced_health,
                    )
                    if balanced_is_safe and (
                        not uniform_is_safe
                        or _integer_curve_smoothness_key(balanced_integer)
                        < _integer_curve_smoothness_key(uniform_integer)
                    ):
                        uniform_integer = balanced_integer
                        uniform_quantization_error = balanced_quantization_error
                        uniform_curves = balanced_curves
                        uniform_zone_results = balanced_zone_results
                        uniform_pair_results = balanced_pair_results
                        uniform_metrics = balanced_metrics
                        uniform_loss = balanced_loss
                        uniform_health = balanced_health
                        uniform_is_safe = True
            if uniform_is_safe:
                target = uniform_target
                constraint_zones = uniform_constraints
                baseline_zone_results = uniform_baseline_zones
                baseline_pair_results = uniform_baseline_pairs
                baseline_metrics = uniform_baseline_metrics
                baseline_loss = uniform_baseline_loss
                baseline_health = uniform_baseline_health
                allow_smooth_inflection = True
                uniform_profile_used = True
                best = (
                    0,
                    uniform_loss.lut_smoothness,
                    uniform_loss.total,
                    strength,
                    uniform_quantization_error,
                    uniform_integer,
                    uniform_curves,
                    uniform_zone_results,
                    uniform_pair_results,
                    uniform_metrics,
                    uniform_loss,
                    uniform_health,
                )

    # If the direct interpolator cannot reach the exact run without an S-shape,
    # make one constrained-concave attempt.  This path is deliberately only a
    # fallback: it is more expensive, but it recovers the highest safe stage
    # count (13 for the captured 15-stage request) instead of dropping all the
    # way back to the original 12 stages.
    if (
        best is None
        and maximum_strength > 0.0
        and target_step_count > baseline_metrics.distinguishable_before
    ):
        strength = maximum_strength * 0.60
        seed_curves = tuple(
            _monotonic_float(
                [old + strength * delta for old, delta in zip(old_curve, delta_curve)],
                old_curve[0],
                old_curve[-1],
            )
            for old_curve, delta_curve in zip(old_curves, deltas)
        )
        projected_curves = _project_shape_preserving_curves(
            dataset,
            reference,
            seed_curves,  # type: ignore[arg-type]
            constraint_zones,
            region.maximum,
            selected_config,
        )
        projected_quantized = tuple(
            _quantize(
                curve,
                region.maximum,
                integer_before[index][0],
                integer_before[index][-1],
            )
            for index, curve in enumerate(projected_curves)
        )
        projected_integer = tuple(item[0] for item in projected_quantized)
        quantization_error = max(item[1] for item in projected_quantized)
        projected_integer = _refine_quantized_curves(
            dataset,
            old_curves,  # type: ignore[arg-type]
            projected_integer,  # type: ignore[arg-type]
            constraint_zones,
            target_step_count,
            region.maximum,
            selected_config,
        )
        projected_normalized = tuple(
            _normalized(curve, region.maximum) for curve in projected_integer
        )
        (
            projected_zones,
            projected_pairs,
            projected_metrics,
            projected_loss,
            projected_health,
        ) = _evaluate(
            dataset,
            selected,
            constraint_zones,
            target_step_count,
            target,
            old_curves,  # type: ignore[arg-type]
            projected_normalized,  # type: ignore[arg-type]
            integer_before,
            projected_integer,  # type: ignore[arg-type]
            region.maximum,
            quantization_error,
            selected_config,
            enforce_naturalness=True,
        )
        projected_selected = [
            item
            for item in projected_zones
            if item.used or item.status == "CONSTRAINT"
        ]
        projected_required = [
            pair for pair in projected_pairs if pair.target_required
        ]
        projected_is_safe = (
            projected_health.status == "PASS"
            and projected_metrics.distinguishable_after == target_step_count
            and all(
                pair.delta_after
                >= minimum_continuity_gap(selected_config.threshold)
                for pair in projected_required
            )
            and projected_metrics.rmse_after
            < baseline_metrics.rmse_before - 1e-7
            and projected_metrics.local_gamma_error_after
            <= baseline_metrics.local_gamma_error_before + 0.003
            and projected_metrics.maximum_error_after
            <= baseline_metrics.maximum_error_before + 0.003
            and (
                selected_config.rgb_mode != "independent"
                or projected_metrics.rgb_gray_deviation_after
                <= baseline_metrics.rgb_gray_deviation_before + 0.002
            )
            and not any(
                abs(item.error_after) > abs(item.error_before) + 0.005
                for item in projected_selected
            )
        )
        if projected_is_safe:
            best = (
                0,
                projected_loss.lut_smoothness,
                projected_loss.total,
                strength,
                quantization_error,
                projected_integer,
                projected_normalized,
                projected_zones,
                projected_pairs,
                projected_metrics,
                projected_loss,
                projected_health,
            )

    warnings: list[str] = list(dataset.warnings)
    if best is None:
        strength = 0.0
        quantization_error = 0.0
        integer_after = integer_before
        zone_results = baseline_zone_results
        pair_results = baseline_pair_results
        metrics = baseline_metrics
        loss = baseline_loss
        health = baseline_health
        if shape_rejected:
            warnings.append(
                f"目标连续 {target_step_count} 阶的候选均造成 LUT 斜率在下降后再次抬升，"
                "形成局部 S 形下凹；已被形状保持门禁拒绝，并保留原 LUT。"
            )
        else:
            warnings.append(
                "没有候选同时满足精确目标阶数、连续灰阶余量、灰阶回退、RGB 色偏和 Curve Health 门禁；"
                "为避免生成孤立灰阶，已保留原 LUT。"
            )
    else:
        (
            _step_score,
            _smoothness_score,
            _loss_score,
            strength,
            quantization_error,
            integer_after,
            _new_curves,
            zone_results,
            pair_results,
            metrics,
            loss,
            health,
        ) = best

        refined_integer_after = _refine_quantized_curves(
            dataset,
            old_curves,  # type: ignore[arg-type]
            integer_after,  # type: ignore[arg-type]
            constraint_zones,
            metrics.distinguishable_after,
            region.maximum,
            selected_config,
        )
        if refined_integer_after != integer_after:
            refined_curves = tuple(
                _normalized(curve, region.maximum)
                for curve in refined_integer_after
            )
            (
                refined_zone_results,
                refined_pair_results,
                refined_metrics,
                refined_loss,
                refined_health,
            ) = _evaluate(
                dataset,
                selected,
                constraint_zones,
                target_step_count,
                target,
                old_curves,  # type: ignore[arg-type]
                refined_curves,  # type: ignore[arg-type]
                integer_before,
                refined_integer_after,
                region.maximum,
                quantization_error,
                selected_config,
                enforce_naturalness=True,
                allow_smooth_inflection=allow_smooth_inflection,
            )
            refined_selected = [
                item
                for item in refined_zone_results
                if item.used or item.status == "CONSTRAINT"
            ]
            refined_required = [
                pair for pair in refined_pair_results if pair.target_required
            ]
            refinement_is_safe = (
                refined_health.status == "PASS"
                and refined_metrics.distinguishable_after
                == metrics.distinguishable_after
                and all(
                    pair.delta_after
                    >= minimum_continuity_gap(selected_config.threshold)
                    for pair in refined_required
                )
                and refined_metrics.rmse_after
                < baseline_metrics.rmse_before - 1e-7
                and refined_metrics.local_gamma_error_after
                <= baseline_metrics.local_gamma_error_before + 0.003
                and refined_metrics.maximum_error_after
                <= baseline_metrics.maximum_error_before + 0.003
                and (
                    selected_config.rgb_mode != "independent"
                    or refined_metrics.rgb_gray_deviation_after
                    <= baseline_metrics.rgb_gray_deviation_before + 0.002
                )
                and not any(
                    abs(item.error_after) > abs(item.error_before) + 0.005
                    for item in refined_selected
                )
                and refined_loss.lut_smoothness
                < loss.lut_smoothness - 1e-12
            )
            if refinement_is_safe:
                integer_after = refined_integer_after
                zone_results = refined_zone_results
                pair_results = refined_pair_results
                metrics = refined_metrics
                loss = refined_loss
                health = refined_health

    # Natural smoothness is a final hard gate.  Candidate exploration treats
    # it as a warning so the constrained integer refiner has a curve to work
    # from; neither an unnatural curve nor a shape-reversing curve may be
    # returned to the UI as an "After" result.
    final_curves = tuple(
        _normalized(curve, region.maximum) for curve in integer_after
    )
    zone_results, pair_results, metrics, loss, health = _evaluate(
        dataset,
        selected,
        constraint_zones,
        target_step_count,
        target,
        old_curves,  # type: ignore[arg-type]
        final_curves,  # type: ignore[arg-type]
        integer_before,
        integer_after,  # type: ignore[arg-type]
        region.maximum,
        quantization_error,
        selected_config,
        enforce_naturalness=True,
        allow_smooth_inflection=allow_smooth_inflection,
    )
    if health.status == "FAIL" and integer_after != integer_before:
        failed_checks = tuple(
            check.name for check in health.checks if check.status == "FAIL"
        )
        if "LUT Shape Preservation" in failed_checks:
            shape_rejected = True
        warnings.append(
            "最终候选未通过 "
            + " / ".join(failed_checks)
            + "；未生成可写回的 After，已回退并保留原 LUT。"
        )
        strength = 0.0
        quantization_error = 0.0
        integer_after = integer_before
        final_curves = old_curves
        zone_results, pair_results, metrics, loss, health = _evaluate(
            dataset,
            selected,
            constraint_zones,
            target_step_count,
            target,
            old_curves,  # type: ignore[arg-type]
            final_curves,  # type: ignore[arg-type]
            integer_before,
            integer_before,
            region.maximum,
            quantization_error,
            selected_config,
            enforce_naturalness=True,
            allow_smooth_inflection=allow_smooth_inflection,
        )
    if metrics.distinguishable_after < target_step_count:
        if shape_rejected:
            warnings.append(
                f"目标连续 {target_step_count} 阶在形状保持约束下不可达；"
                f"当前保留 Before 的 {metrics.distinguishable_after} 阶，"
                "不会通过强拉 Gamma LUT 制造亮度压缩后追高。"
            )
        else:
            warnings.append(
                f"目标连续 {target_step_count} 阶尚未完全达到，当前 After 为 {metrics.distinguishable_after} 阶；"
                "可提高最大调整强度或降低识别阈值。"
            )
    natural_check = next(
        (
            check
            for check in health.checks
            if check.name == "LUT Natural Smoothness"
        ),
        None,
    )
    if natural_check is not None and natural_check.status != "PASS":
        warnings.append(
            "LUT 自然平滑度门禁未通过；为避免亮度加速/减速突变，当前结果禁止写回 XML。"
        )
    # This is the applied target after quantization-aware natural smoothing,
    # rather than the rejected maximum-strength theoretical curve.
    target_reference = _average_curves(*integer_after)
    required_gaps = [
        pair.delta_after for pair in pair_results if pair.target_required
    ]
    profile_explanation = (
        f"高阶均匀化已启用：目标区间相邻 Pixel 间隔="
        f"{min(required_gaps):.2f}~{max(required_gaps):.2f}；"
        "允许一个通过曲率、三阶变化和斜率连续门禁的宽缓拐点。"
        if uniform_profile_used and required_gaps
        else "使用普通形状保持目标；禁止局部平台、反转及亮度压缩后突然追高。"
    )
    explainability = (
        f"仅使用连续有效 Zone {selected[0]}-{selected[-1]}（{len(selected)} 阶）做 Gamma 拟合；"
        f"目标连续阶数约束={target_step_count}，Before/After={metrics.distinguishable_before}/{metrics.distinguishable_after}。",
        f"优化对象为当前 region #{region.index} 的 R/G/B {region.length} 点 LUT，整数范围 0~{region.maximum}；模式={selected_config.rgb_mode}。",
        f"Gamma 提亮系数={selected_config.target_gamma:.3f}（1.0=标称，数值越大越亮）；LUT 最高亮度={region.maximum}。",
        f"多目标 Loss={baseline_loss.total:.6f}->{loss.total:.6f}；RMSE={metrics.rmse_before:.5f}->{metrics.rmse_after:.5f}。",
        f"高光保护={selected_config.highlight_protection:.0%}，暗部保护={selected_config.shadow_protection:.0%}，实际强度={strength:.0%}。",
        profile_explanation,
        f"Curve Health={health.status}；单调={health.monotonic}，形状保持门禁已执行，最大步长={health.maximum_jump}，量化误差={health.quantization_error:.7f}。",
    )
    return GammaOptimizationResult(
        region_index=region.index,
        selected_zones=selected,
        before_r=region.channel_r,
        before_g=region.channel_g,
        before_b=region.channel_b,
        after_r=integer_after[0],
        after_g=integer_after[1],
        after_b=integer_after[2],
        target_lut=target_reference,
        zone_results=zone_results,
        pair_results=pair_results,
        metrics=metrics,
        loss_before=baseline_loss,
        loss_after=loss,
        health=health,
        applied_strength=strength,
        requested_step_count=target_step_count,
        rgb_mode=selected_config.rgb_mode,
        target_gamma_factor=selected_config.target_gamma,
        lut_length=region.length,
        maximum_value=region.maximum,
        diagnostics=_build_gamma_diagnostics(dataset, metrics, health, pair_results),
        explainability=explainability,
        warnings=tuple(warnings),
    )


def optimize_gamma_lut(
    dataset: GrayDataset,
    region: GammaRegion,
    analysis: GrayRangeAnalysis,
    *,
    config: Optional[GammaOptimizationConfig] = None,
) -> GammaOptimizationResult:
    """Return the highest safe contiguous stage count up to the request.

    An explicit target is an upper objective, not permission to introduce a
    platform, reversal, or abrupt local contrast jump.  A high-stage request
    may use one broad equalizing inflection, but only after curvature, jerk,
    continuity, regression, shape, naturalness, and quantization all pass.
    """

    selected_config = config or GammaOptimizationConfig(threshold=analysis.threshold)
    selected_config.validate()
    if selected_config.target_step_count is None:
        result = _optimize_gamma_lut_exact(
            dataset,
            region,
            analysis,
            config=selected_config,
        )
        return replace(
            result,
            requested_step_count=result.metrics.distinguishable_target,
        )

    requested_count = max(
        analysis.effective_count,
        selected_config.target_step_count,
    )
    first_failure: Optional[GammaOptimizationResult] = None
    for safe_target in range(requested_count, analysis.effective_count - 1, -1):
        trial_config = replace(
            selected_config,
            target_step_count=safe_target,
        )
        result = _optimize_gamma_lut_exact(
            dataset,
            region,
            analysis,
            config=trial_config,
        )
        if first_failure is None:
            first_failure = result
        exact_safe_result = (
            result.applied_strength > 0.0
            and result.health.status == "PASS"
            and result.metrics.distinguishable_after == safe_target
            and result.metrics.distinguishable_target == safe_target
        )
        if not exact_safe_result:
            continue
        if safe_target == requested_count:
            return replace(result, requested_step_count=requested_count)
        fallback_warning = (
            f"请求连续 {requested_count} 阶；在连续性、形状保持和自然平滑门禁下，"
            f"最高安全结果为连续 {safe_target} 阶，已自动采用。"
        )
        return replace(
            result,
            requested_step_count=requested_count,
            warnings=(fallback_warning, *result.warnings),
            explainability=(fallback_warning, *result.explainability),
        )

    assert first_failure is not None
    return replace(first_failure, requested_step_count=requested_count)
