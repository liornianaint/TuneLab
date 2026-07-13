from __future__ import annotations

import bisect
import math
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
    # Add a small quantization reserve.  Scale the desired separation so the
    # configured maximum strength can still reach the requested post-LUT gap.
    strength = max(config.maximum_adjustment, 0.05)
    minimum_gaps = []
    for current, following in zip(current_pixels, current_pixels[1:]):
        current_gap = current - following
        # LUT interpolation, smoothing and integer quantization reduce the
        # measured spacing slightly.  A 2.5-Pixel reserve keeps the final
        # Stepchart result on the requested side of the threshold.
        required_target_gap = analysis.threshold + 2.50
        if current_gap < analysis.threshold:
            required_target_gap += (analysis.threshold - current_gap) * (1.0 - strength) / strength
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


def _smooth(values: Sequence[float], passes: int = 4) -> list[float]:
    output = list(values)
    for _ in range(passes):
        source = output[:]
        for index in range(2, len(output) - 2):
            output[index] = (
                source[index - 2]
                + 2.0 * source[index - 1]
                + 3.0 * source[index]
                + 2.0 * source[index + 1]
                + source[index + 2]
            ) / 9.0
    return output


def _interpolated_delta(
    anchors: Sequence[tuple[float, float]],
    length: int,
    *,
    highlight_protection: float,
    shadow_protection: float,
) -> tuple[float, ...]:
    collapsed: dict[float, list[float]] = {}
    for position, delta in anchors:
        collapsed.setdefault(round(_clamp(position), 8), []).append(delta)
    points = sorted((position, mean(values)) for position, values in collapsed.items())
    if len(points) < 2:
        raise GammaOptimizationError(f"有效灰阶映射点不足，不能生成 {length} 点 LUT。")
    positions = [item[0] for item in points]
    deltas = [item[1] for item in points]
    output: list[float] = []
    for index in range(length):
        position = index / (length - 1)
        insertion = bisect.bisect_left(positions, position)
        if insertion == 0:
            first_position = max(positions[0], 1e-9)
            delta = deltas[0] * position / first_position
        elif insertion >= len(points):
            last_position = min(positions[-1], 1.0 - 1e-9)
            delta = deltas[-1] * (1.0 - position) / (1.0 - last_position)
        else:
            x0, y0 = points[insertion - 1]
            x1, y1 = points[insertion]
            fraction = 0.0 if x1 <= x0 else (position - x0) / (x1 - x0)
            delta = y0 * (1.0 - fraction) + y1 * fraction
        if position < 0.16:
            shadow_factor = (position / 0.16) ** 1.5
            delta *= (1.0 - shadow_protection) + shadow_protection * shadow_factor
        if position > 0.78:
            highlight_factor = ((1.0 - position) / 0.22) ** 1.5
            delta *= (1.0 - highlight_protection) + highlight_protection * max(0.0, highlight_factor)
        output.append(delta)
    output[0] = 0.0
    output[-1] = 0.0
    output = _smooth(output)
    output[0] = 0.0
    output[-1] = 0.0
    return tuple(output)


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
        len(reference_curve),
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
        len(curve),
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
    for index in range(1, len(quantized) - 1):
        quantized[index] = max(quantized[index - 1], min(quantized[index], last))
    for index in range(len(quantized) - 2, 0, -1):
        quantized[index] = min(quantized[index], quantized[index + 1])
    error = max(abs(value - quantized[index] / maximum) for index, value in enumerate(values))
    return tuple(quantized), error


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
) -> GammaCurveHealth:
    reversal_count = sum(
        following < current
        for curve in after
        for current, following in zip(curve, curve[1:])
    )
    maximum_jump = max(following - current for curve in after for current, following in zip(curve, curve[1:]))
    original_jump = max(following - current for curve in before for current, following in zip(curve, curve[1:]))
    endpoint_ok = all(
        candidate[0] == original[0] and candidate[-1] == original[-1]
        for original, candidate in zip(before, after)
    )
    bounds_ok = all(0 <= value <= maximum for curve in after for value in curve)
    jump_warning = max(32, int(original_jump * 1.8) + 1)
    checks = (
        GammaEngineeringCheck(
            "LUT Monotonicity",
            "PASS" if reversal_count == 0 else "FAIL",
            f"reversals={reversal_count}",
            "0",
            f"{len(after[0])} 点 LUT 必须单调不下降且无局部反转。",
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
    health = _curve_health(integer_before, integer_after, quantization_error, maximum)
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
            f"Before 可识别 {metrics.distinguishable_before} 阶，After {metrics.distinguishable_after} 阶，"
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


def optimize_gamma_lut(
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
    maximum_strength = selected_config.maximum_adjustment
    strengths = tuple(maximum_strength * fraction for fraction in (0.20, 0.35, 0.50, 0.65, 0.80, 1.0))
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
        if health.status == "FAIL":
            continue
        if metrics.distinguishable_after < baseline_metrics.distinguishable_before:
            continue
        if metrics.rmse_after >= baseline_metrics.rmse_before - 1e-7:
            continue
        if metrics.local_gamma_error_after > baseline_metrics.local_gamma_error_before + 0.003:
            continue
        if metrics.maximum_error_after > baseline_metrics.maximum_error_before + 0.003:
            continue
        if metrics.rgb_gray_deviation_after > baseline_metrics.rgb_gray_deviation_before + 0.002:
            continue
        if any(abs(item.error_after) > abs(item.error_before) + 0.005 for item in selected_results):
            continue
        candidate = (
            -metrics.distinguishable_after,
            loss.total,
            strength,
            integer_after,
            new_curves,
            zone_results,
            pair_results,
            metrics,
            loss,
            health,
        )
        if best is None or candidate[:2] < best[:2]:
            best = candidate

    warnings: list[str] = list(dataset.warnings)
    if best is None:
        strength = 0.0
        integer_after = integer_before
        zone_results = baseline_zone_results
        pair_results = baseline_pair_results
        metrics = baseline_metrics
        loss = baseline_loss
        health = baseline_health
        warnings.append("没有候选同时满足灰阶回退、RGB 色偏和 Curve Health 门禁；已保留原 LUT。")
    else:
        (
            _step_score,
            _loss_score,
            strength,
            integer_after,
            _new_curves,
            zone_results,
            pair_results,
            metrics,
            loss,
            health,
        ) = best
    if metrics.distinguishable_after < target_step_count:
        warnings.append(
            f"目标 {target_step_count} 阶尚未完全达到，当前 After 为 {metrics.distinguishable_after} 阶；"
            "可提高最大调整强度或降低识别阈值。"
        )
    target_reference = tuple(
        _clamp(value + selected_config.maximum_adjustment * delta) * region.maximum
        for value, delta in zip(reference, linked_delta)
    )
    explainability = (
        f"仅使用连续有效 Zone {selected[0]}-{selected[-1]}（{len(selected)} 阶）做 Gamma 拟合；"
        f"目标阶数约束={target_step_count}，Before/After={metrics.distinguishable_before}/{metrics.distinguishable_after}。",
        f"优化对象为当前 region #{region.index} 的 R/G/B {region.length} 点 LUT，整数范围 0~{region.maximum}；模式={selected_config.rgb_mode}。",
        f"Gamma 提亮系数={selected_config.target_gamma:.3f}（1.0=标称，数值越大越亮）；LUT 最高亮度={region.maximum}。",
        f"多目标 Loss={baseline_loss.total:.6f}->{loss.total:.6f}；RMSE={metrics.rmse_before:.5f}->{metrics.rmse_after:.5f}。",
        f"高光保护={selected_config.highlight_protection:.0%}，暗部保护={selected_config.shadow_protection:.0%}，实际强度={strength:.0%}。",
        f"Curve Health={health.status}；单调={health.monotonic}，最大步长={health.maximum_jump}，量化误差={health.quantization_error:.7f}。",
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
        rgb_mode=selected_config.rgb_mode,
        target_gamma_factor=selected_config.target_gamma,
        lut_length=region.length,
        maximum_value=region.maximum,
        diagnostics=_build_gamma_diagnostics(dataset, metrics, health, pair_results),
        explainability=explainability,
        warnings=tuple(warnings),
    )
