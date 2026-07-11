from __future__ import annotations

import bisect
import math
from statistics import mean
from typing import Callable, Optional, Sequence

from .gamma_models import (
    GammaCurveHealth,
    GammaEngineeringCheck,
    GammaLossBreakdown,
    GammaMetrics,
    GammaOptimizationConfig,
    GammaOptimizationResult,
    GammaRegion,
    GammaZoneResult,
    GrayDataset,
    GrayRangeAnalysis,
    GrayZone,
)
from .gray_imatest import select_fit_zones


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


def _target_densities(zones: Sequence[GrayZone], target_gamma: float) -> dict[int, float]:
    xs = [-zone.log_exposure for zone in zones]
    intercept = mean(zone.density - target_gamma * x for zone, x in zip(zones, xs))
    return {zone.zone: intercept + target_gamma * (-zone.log_exposure) for zone in zones}


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
        raise GammaOptimizationError("有效灰阶映射点不足，不能生成 257 点 LUT。")
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
            "257 点 LUT 必须单调不下降且无局部反转。",
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
    target: dict[int, float],
    old_curves: tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]],
    new_curves: tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]],
    integer_before: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    integer_after: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]],
    maximum: int,
    quantization_error: float,
    config: GammaOptimizationConfig,
) -> tuple[tuple[GammaZoneResult, ...], GammaMetrics, GammaLossBreakdown, GammaCurveHealth]:
    selected_set = set(selected)
    selected_rows = [zone for zone in dataset.zones if zone.zone in selected_set]
    reference_old = _average_curves(*old_curves)
    reference_new = _average_curves(*new_curves)
    all_target = _target_densities(selected_rows, config.target_gamma)
    slope, intercept = _linear_regression(
        [-zone.log_exposure for zone in selected_rows],
        [all_target[zone.zone] for zone in selected_rows],
    )

    before_density: dict[int, float] = {}
    after_density: dict[int, float] = {}
    target_density: dict[int, float] = {}
    rgb_before_values: list[float] = []
    rgb_after_values: list[float] = []
    for zone in dataset.zones:
        x_value = -zone.log_exposure
        target_value = intercept + slope * x_value
        target_density[zone.zone] = target_value
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
        zone_results.append(
            GammaZoneResult(
                zone=zone.zone,
                used=used,
                status="FIT" if used else "EXCLUDED",
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
                local_gamma_after=local_after[index],
            )
        )

    fit = [item for item in zone_results if item.used]
    fit_exposures = [-next(zone.log_exposure for zone in dataset.zones if zone.zone == item.zone) for item in fit]
    global_before, _ = _linear_regression(fit_exposures, [item.density_before for item in fit])
    global_after, _ = _linear_regression(fit_exposures, [item.density_after for item in fit])
    rmse_before = math.sqrt(mean(item.error_before ** 2 for item in fit))
    rmse_after = math.sqrt(mean(item.error_after ** 2 for item in fit))
    local_before_values = [item.local_gamma_before for item in fit if item.local_gamma_before is not None and item.zone != selected[-1]]
    local_after_values = [item.local_gamma_after for item in fit if item.local_gamma_after is not None and item.zone != selected[-1]]
    local_error_before = mean(abs(value - config.target_gamma) for value in local_before_values) if local_before_values else 0.0
    local_error_after = mean(abs(value - config.target_gamma) for value in local_after_values) if local_after_values else 0.0
    metrics = GammaMetrics(
        global_gamma_before=global_before,
        global_gamma_after=global_after,
        rmse_before=rmse_before,
        rmse_after=rmse_after,
        maximum_error_before=max(abs(item.error_before) for item in fit),
        maximum_error_after=max(abs(item.error_after) for item in fit),
        local_gamma_error_before=local_error_before,
        local_gamma_error_after=local_error_after,
        rgb_gray_deviation_before=mean(rgb_before_values),
        rgb_gray_deviation_after=mean(rgb_after_values),
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
        ),
        gray_target=rmse_after,
        local_gamma=local_error_after,
        lut_smoothness=smoothness,
        lut_change=change,
        highlight=highlight_change,
        shadow=shadow_change,
        rgb_bias=metrics.rgb_gray_deviation_after,
    )
    health = _curve_health(integer_before, integer_after, quantization_error, maximum)
    return tuple(zone_results), metrics, loss, health


def optimize_gamma_lut(
    dataset: GrayDataset,
    region: GammaRegion,
    analysis: GrayRangeAnalysis,
    *,
    config: Optional[GammaOptimizationConfig] = None,
) -> GammaOptimizationResult:
    selected_config = config or GammaOptimizationConfig(threshold=analysis.threshold)
    selected_config.validate()
    if region.length != 257:
        raise GammaOptimizationError(f"当前 Gamma XML 必须为 257 点，实际为 {region.length} 点。")
    if region.maximum != 1023:
        raise GammaOptimizationError(f"当前 Gamma XML 必须为 0~1023，实际最大值为 {region.maximum}。")
    selected = select_fit_zones(
        dataset,
        analysis,
        mode=selected_config.range_mode,
        manual_start=selected_config.manual_start_zone,
        manual_end=selected_config.manual_end_zone,
    )
    zones = [zone for zone in dataset.zones if zone.zone in set(selected)]
    target = _target_densities(zones, selected_config.target_gamma)
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

    baseline_zone_results, baseline_metrics, baseline_loss, baseline_health = _evaluate(
        dataset,
        selected,
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
        zone_results, metrics, loss, health = _evaluate(
            dataset,
            selected,
            target,
            old_curves,  # type: ignore[arg-type]
            new_curves,  # type: ignore[arg-type]
            integer_before,
            integer_after,  # type: ignore[arg-type]
            region.maximum,
            quantization_error,
            selected_config,
        )
        selected_results = [item for item in zone_results if item.used]
        if health.status == "FAIL":
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
        candidate = (loss.total, strength, integer_after, new_curves, zone_results, metrics, loss, health)
        if best is None or candidate[0] < best[0]:
            best = candidate

    warnings: list[str] = list(dataset.warnings)
    if best is None:
        strength = 0.0
        integer_after = integer_before
        zone_results = baseline_zone_results
        metrics = baseline_metrics
        loss = baseline_loss
        health = baseline_health
        warnings.append("没有候选同时满足灰阶回退、RGB 色偏和 Curve Health 门禁；已保留原 LUT。")
    else:
        _score, strength, integer_after, _new_curves, zone_results, metrics, loss, health = best
    target_reference = tuple(
        _clamp(value + selected_config.maximum_adjustment * delta) * region.maximum
        for value, delta in zip(reference, linked_delta)
    )
    explainability = (
        f"仅使用连续有效 Zone {selected[0]}-{selected[-1]}（{len(selected)} 阶）拟合；阈值={analysis.threshold:g} Pixel。",
        f"优化对象为当前 region #{region.index} 的 R/G/B 257 点 LUT；模式={selected_config.rgb_mode}。",
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
        metrics=metrics,
        loss_before=baseline_loss,
        loss_after=loss,
        health=health,
        applied_strength=strength,
        rgb_mode=selected_config.rgb_mode,
        explainability=explainability,
        warnings=tuple(warnings),
    )

