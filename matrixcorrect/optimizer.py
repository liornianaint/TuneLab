from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, replace
from statistics import mean
from typing import Iterable, Optional

from .color import (
    delta_e_2000,
    hue_difference_degrees,
    identity_matrix,
    lab_chroma,
    linear_to_srgb,
    mat_mul,
    mat_vec,
    matrix_blend,
    srgb_to_lab,
    srgb_to_linear,
)
from .diagnostics import build_module_diagnostics
from .engineering import (
    condition_number,
    correction_magnitude,
    determinant,
    evaluate_matrix_health,
    inverse,
    matrix_distance,
    project_row_sum_and_bounds,
)
from .models import (
    CategoryStatistics,
    ImatestDataset,
    LossBreakdown,
    Matrix3,
    MatrixHealth,
    OptimizationConfig,
    OptimizationResult,
    PassRateStatistics,
    PatchResult,
    Vector3,
    safe_improvement_percent,
)


class OptimizationError(ValueError):
    pass


AUTO_REGULARIZATION = (0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0)
PASS_THRESHOLDS = (2.0, 3.0, 5.0, 10.0)
PASS_RATE_COMPARATOR = "<="
NEUTRAL_PATCHES = frozenset(range(19, 25))
NEUTRAL_PATCH_REGRESSION_LIMIT = 0.50


@dataclass(frozen=True)
class _Candidate:
    correction: Matrix3
    optimized: Matrix3
    patch_results: list[PatchResult]
    health: MatrixHealth
    loss: LossBreakdown
    regularization: float
    blend: float
    search_method: str


def transpose(matrix: Matrix3) -> Matrix3:
    return tuple(tuple(matrix[col][row] for col in range(3)) for row in range(3))  # type: ignore[return-value]


def _solve_linear(system: list[list[float]], right: list[float]) -> list[float]:
    size = len(right)
    augmented = [system[row][:] + [right[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise OptimizationError("矩阵拟合方程奇异；请检查 CSV 色块数据或提高 Regularization。")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                augmented[row][index] - factor * augmented[column][index]
                for index in range(size + 1)
            ]
    return [augmented[row][-1] for row in range(size)]


def _strategy_config(config: OptimizationConfig) -> OptimizationConfig:
    config.validate()
    if config.strategy == "conservative":
        return replace(
            config,
            max_blend=min(config.max_blend, 0.55),
            max_patch_regression=min(config.max_patch_regression, 0.50),
            good_patch_regression=min(config.good_patch_regression, 0.25),
            focus_patch_regression=min(config.focus_patch_regression, 0.12),
            loss_regression=config.loss_regression * 1.5,
            loss_smoothness=config.loss_smoothness * 1.4,
        )
    if config.strategy == "aggressive":
        return replace(
            config,
            max_patch_regression=min(config.max_patch_regression, 0.90),
            loss_delta_e=config.loss_delta_e * 1.15,
            loss_smoothness=config.loss_smoothness * 0.75,
        )
    return config


def _priority_weight(zone: int, category: str, config: OptimizationConfig) -> float:
    weights = {
        "Skin": config.skin_weight,
        "Primary": config.primary_weight,
        "Secondary": config.secondary_weight,
        "Memory": config.memory_weight,
        "Chromatic": 1.0,
        "Neutral": 0.35,
    }
    weight = weights.get(category, 1.0)
    if zone in config.focus_patches:
        weight *= config.focus_weight
    return weight


def _fit_target(source: Vector3, ideal: Vector3, saturation_factor: float) -> Vector3:
    luma_weights = (0.2126729, 0.7151522, 0.0721750)
    source_luma = sum(value * weight for value, weight in zip(source, luma_weights))
    ideal_luma = sum(value * weight for value, weight in zip(ideal, luma_weights))
    scale = source_luma / ideal_luma if ideal_luma > 1e-12 else 1.0
    scaled = tuple(value * scale for value in ideal)
    neutral = (source_luma, source_luma, source_luma)
    return tuple(neutral[index] + saturation_factor * (scaled[index] - neutral[index]) for index in range(3))  # type: ignore[return-value]


def saturation_target_chroma(ideal_chroma: float, saturation_factor: float) -> float:
    """Apply saturation_factor exactly once to the ideal chroma target."""

    return ideal_chroma * saturation_factor


def _fit_constrained_matrix(
    dataset: ImatestDataset,
    regularization: float,
    config: OptimizationConfig,
) -> Matrix3:
    patches = [patch for patch in dataset.patches if patch.zone <= 18]
    if len(patches) < 9:
        raise OptimizationError("可用于 CC 拟合的彩色色块不足。")
    measured = [srgb_to_linear(patch.measured_srgb) for patch in patches]
    ideal = [
        _fit_target(source, srgb_to_linear(patch.ideal_srgb), config.saturation_factor)
        for source, patch in zip(measured, patches)
    ]
    weights = [_priority_weight(patch.zone, patch.category, config) for patch in patches]
    gram = [[0.0] * 3 for _ in range(3)]
    for vector, weight in zip(measured, weights):
        for row in range(3):
            for col in range(3):
                gram[row][col] += weight * vector[row] * vector[col]
    scale = max(sum(gram[index][index] for index in range(3)) / 3.0, 1e-6)
    ridge = regularization * scale
    rows: list[Vector3] = []
    for output_channel in range(3):
        system = [[0.0] * 4 for _ in range(4)]
        right = [0.0] * 4
        for row in range(3):
            for col in range(3):
                system[row][col] = gram[row][col] + (ridge if row == col else 0.0)
            system[row][3] = 1.0
            system[3][row] = 1.0
            right[row] = sum(
                weight * vector[row] * target[output_channel]
                for vector, target, weight in zip(measured, ideal, weights)
            )
            if row == output_channel:
                right[row] += ridge
        right[3] = 1.0
        solution = _solve_linear(system, right)
        rows.append((solution[0], solution[1], solution[2]))
    return tuple(rows)  # type: ignore[return-value]


def _predict(patch_rgb: Vector3, correction: Matrix3) -> tuple[Vector3, Vector3, float]:
    corrected_linear = mat_vec(correction, srgb_to_linear(patch_rgb))
    overflow = sum(max(0.0, -value) + max(0.0, value - 1.0) for value in corrected_linear)
    return linear_to_srgb(corrected_linear), corrected_linear, overflow


def _module_hint(zone: int, before_lab: Vector3, ideal_lab: Vector3) -> str:
    delta_l = before_lab[0] - ideal_lab[0]
    delta_c = lab_chroma(before_lab) - lab_chroma(ideal_lab)
    delta_h = hue_difference_degrees(before_lab, ideal_lab)
    if zone >= 19:
        return "AWB/CC" if lab_chroma(before_lab) > 3.0 else "Gamma/TMC"
    if zone in (1, 2) and abs(delta_h) >= 4.0:
        return "SCE/2D LUT"
    if abs(delta_l) > max(5.0, abs(delta_c) * 1.5):
        return "Gamma/TMC"
    if abs(delta_c) >= 4.0 and abs(delta_h) < 5.0:
        return "CV/Saturation"
    if abs(delta_h) >= 6.0:
        return "CC -> 2D LUT if localized"
    return "CC"


def _build_patch_results(
    dataset: ImatestDataset,
    correction: Matrix3,
    config: OptimizationConfig,
) -> list[PatchResult]:
    results: list[PatchResult] = []
    for patch in dataset.patches:
        after_srgb, _, _ = _predict(patch.measured_srgb, correction)
        before_lab = srgb_to_lab(patch.measured_srgb)
        after_lab = srgb_to_lab(after_srgb)
        ideal_lab = srgb_to_lab(patch.ideal_srgb)
        before_e = delta_e_2000(before_lab, ideal_lab)
        after_e = delta_e_2000(after_lab, ideal_lab)
        before_c = lab_chroma(before_lab)
        after_c = lab_chroma(after_lab)
        ideal_c = lab_chroma(ideal_lab)
        before_h = 0.0 if patch.zone >= 19 else hue_difference_degrees(before_lab, ideal_lab)
        after_h = 0.0 if patch.zone >= 19 else hue_difference_degrees(after_lab, ideal_lab)
        regression = max(0.0, after_e - before_e)
        if patch.zone in config.focus_patches:
            regression_limit = config.focus_patch_regression
        elif patch.zone in NEUTRAL_PATCHES:
            regression_limit = min(config.good_patch_regression, NEUTRAL_PATCH_REGRESSION_LIMIT)
        elif before_e < 3.0:
            regression_limit = config.good_patch_regression
        else:
            regression_limit = config.max_patch_regression
        regression_status = "PASS" if regression <= config.regression_epsilon else "WARNING" if regression <= regression_limit else "FAIL"
        results.append(
            PatchResult(
                zone=patch.zone,
                name=patch.display_name(),
                category=patch.category,
                priority_weight=_priority_weight(patch.zone, patch.category, config),
                before_srgb=patch.measured_srgb,
                after_srgb=after_srgb,
                ideal_srgb=patch.ideal_srgb,
                before_lab=before_lab,
                after_lab=after_lab,
                ideal_lab=ideal_lab,
                delta_e_before=before_e,
                delta_e_after=after_e,
                improvement_percent=safe_improvement_percent(before_e, after_e),
                delta_l_before=before_lab[0] - ideal_lab[0],
                delta_l_after=after_lab[0] - ideal_lab[0],
                delta_c_before=before_c - ideal_c,
                delta_c_after=after_c - ideal_c,
                delta_h_before=before_h,
                delta_h_after=after_h,
                chroma_before=before_c,
                chroma_after=after_c,
                chroma_ideal=ideal_c,
                regression=regression,
                regression_status=regression_status,
                module_hint=_module_hint(patch.zone, before_lab, ideal_lab),
            )
        )
    return results


def _pass_rates(patches: list[PatchResult]) -> PassRateStatistics:
    return PassRateStatistics(
        thresholds=PASS_THRESHOLDS,
        before_counts=pass_rate_counts((patch.delta_e_before for patch in patches)),
        after_counts=pass_rate_counts((patch.delta_e_after for patch in patches)),
        sample_count=len(patches),
    )


def pass_rate_counts(
    errors: Iterable[float],
    thresholds: tuple[float, ...] = PASS_THRESHOLDS,
) -> tuple[int, ...]:
    values = tuple(float(value) for value in errors)
    return tuple(sum(value <= threshold for value in values) for threshold in thresholds)


def _category_statistics(patches: list[PatchResult]) -> tuple[CategoryStatistics, ...]:
    output: list[CategoryStatistics] = []
    for category in ("Skin", "Memory", "Chromatic", "Primary", "Secondary", "Neutral"):
        group = [patch for patch in patches if patch.category == category]
        if not group:
            continue
        output.append(
            CategoryStatistics(
                category=category,
                count=len(group),
                mean_before=mean(patch.delta_e_before for patch in group),
                mean_after=mean(patch.delta_e_after for patch in group),
                improved=sum(patch.delta_e_after < patch.delta_e_before - 1e-9 for patch in group),
                regressed=sum(patch.regression > 0.05 for patch in group),
                pass_rate_before_3=sum(patch.delta_e_before <= 3.0 for patch in group) / len(group),
                pass_rate_after_3=sum(patch.delta_e_after <= 3.0 for patch in group) / len(group),
            )
        )
    return tuple(output)


def _saturation_ratio(patches: list[PatchResult], *, after: bool) -> float:
    colors = [patch for patch in patches if patch.category != "Neutral" and patch.chroma_ideal > 1e-6]
    numerator = sum((patch.chroma_after if after else patch.chroma_before) * patch.priority_weight for patch in colors)
    denominator = sum(patch.chroma_ideal * patch.priority_weight for patch in colors)
    return numerator / denominator if denominator else 1.0


def _loss_breakdown(
    patches: list[PatchResult],
    correction: Matrix3,
    original: Matrix3,
    optimized: Matrix3,
    health: MatrixHealth,
    config: OptimizationConfig,
) -> LossBreakdown:
    colors = [patch for patch in patches if patch.category != "Neutral"]
    total_weight = sum(patch.priority_weight for patch in colors) or 1.0
    de = sum(patch.priority_weight * patch.delta_e_after for patch in colors) / total_weight
    dc = sum(patch.priority_weight * abs(patch.delta_c_after) for patch in colors) / total_weight
    dh = sum(patch.priority_weight * abs(patch.delta_h_after) / 10.0 for patch in colors) / total_weight
    dl = sum(patch.priority_weight * abs(patch.delta_l_after) / 10.0 for patch in colors) / total_weight
    ordered = sorted(patch.delta_e_after for patch in colors)
    p90 = ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.9) - 1))] if ordered else 0.0
    regression = sum(
        patch.priority_weight * patch.regression * patch.regression
        * (2.0 if patch.zone in config.focus_patches else 1.0)
        for patch in colors
    ) / total_weight
    ratio = _saturation_ratio(patches, after=True)
    global_sat = abs(ratio - config.saturation_factor) * 10.0
    local_sat = sum(
        patch.priority_weight * max(
            0.0,
            patch.chroma_after
            - saturation_target_chroma(patch.chroma_ideal, config.saturation_factor)
            - config.local_saturation_tolerance,
        )
        for patch in colors
    ) / total_weight
    saturation = global_sat + 0.12 * local_sat
    matrix_term = correction_magnitude(correction) ** 2
    smoothness = matrix_distance(original, optimized) ** 2
    engineering = (
        max(0.0, health.condition_number - config.condition_warning) / max(config.condition_warning, 1e-6)
        + max(0.0, config.determinant_warning - abs(health.determinant)) / max(config.determinant_warning, 1e-6)
        + health.fixed_point_max_delta_e
        + (5.0 if health.status == "FAIL" else 0.5 if health.status == "WARNING" else 0.0)
    )
    total = (
        config.loss_delta_e * de
        + config.loss_delta_c * dc
        + config.loss_delta_h * dh
        + config.loss_delta_l * dl
        + config.loss_p90 * p90
        + config.loss_regression * regression
        + config.loss_saturation * saturation
        + config.loss_matrix * matrix_term
        + config.loss_smoothness * smoothness
        + config.loss_engineering * engineering
    )
    return LossBreakdown(total, de, dc, dh, dl, p90, regression, saturation, matrix_term, smoothness, engineering)


def compose_correction_matrix(correction: Matrix3, original: Matrix3, composition: str = "pre") -> Matrix3:
    if composition not in {"pre", "post_transposed"}:
        raise OptimizationError(f"未知矩阵组合约定: {composition}")
    return mat_mul(correction, original) if composition == "pre" else mat_mul(original, transpose(correction))


def _compose(correction: Matrix3, original: Matrix3, composition: str) -> Matrix3:
    return compose_correction_matrix(correction, original, composition)


def _correction_for_final(original: Matrix3, final: Matrix3, composition: str) -> Matrix3:
    try:
        original_inverse = inverse(original)
    except ValueError as exc:
        raise OptimizationError("原 CC Matrix 奇异，无法组合 Delta CCM。") from exc
    if composition == "pre":
        return mat_mul(final, original_inverse)
    return transpose(mat_mul(original_inverse, final))


def _bounded_candidate(
    correction: Matrix3,
    original: Matrix3,
    composition: str,
    config: OptimizationConfig,
) -> tuple[Matrix3, Matrix3]:
    optimized = _compose(correction, original, composition)
    # Always normalize the final XML matrix, even when the source row sums only
    # miss 1.0 by a few ppm due to seven-decimal serialization.
    projected = project_row_sum_and_bounds(optimized, config.coefficient_min, config.coefficient_max)
    if matrix_distance(projected, optimized) > 1e-12:
        correction = _correction_for_final(original, projected, composition)
        optimized = _compose(correction, original, composition)
    return correction, optimized


def _evaluate_candidate(
    dataset: ImatestDataset,
    original: Matrix3,
    correction: Matrix3,
    composition: str,
    config: OptimizationConfig,
    regularization: float,
    blend: float,
    *,
    enforce_bounds: bool = True,
    search_method: str = "regularized-grid",
) -> _Candidate:
    if enforce_bounds:
        correction, optimized = _bounded_candidate(correction, original, composition, config)
    else:
        optimized = _compose(correction, original, composition)
    patches = _build_patch_results(dataset, correction, config)
    health = evaluate_matrix_health(original, optimized, correction, dataset, config)
    loss = _loss_breakdown(patches, correction, original, optimized, health, config)
    return _Candidate(correction, optimized, patches, health, loss, regularization, blend, search_method)


def _protection_failure(
    candidate: _Candidate,
    baseline: _Candidate,
    config: OptimizationConfig,
) -> Optional[str]:
    if candidate.health.status == "FAIL":
        return "matrix-health"
    colors = [patch for patch in candidate.patch_results if patch.category != "Neutral"]
    baseline_colors = [patch for patch in baseline.patch_results if patch.category != "Neutral"]
    if mean(patch.delta_e_after for patch in colors) >= mean(patch.delta_e_after for patch in baseline_colors) - 1e-4:
        return "no-mean-improvement"
    for patch in colors:
        if patch.regression_status == "FAIL":
            return "patch-regression"
        if patch.zone in config.focus_patches:
            if abs(patch.delta_c_after) > abs(patch.delta_c_before) + config.focus_delta_c_regression:
                return "focus-delta-c"
            if abs(patch.delta_h_after) > abs(patch.delta_h_before) + config.focus_delta_h_regression:
                return "focus-delta-h"
    neutral = [patch for patch in candidate.patch_results if patch.zone in NEUTRAL_PATCHES]
    baseline_neutral_by_zone = {
        patch.zone: patch for patch in baseline.patch_results if patch.zone in NEUTRAL_PATCHES
    }
    neutral_limit = min(config.good_patch_regression, NEUTRAL_PATCH_REGRESSION_LIMIT)
    for patch in neutral:
        if patch.regression > neutral_limit + 1e-12:
            return "neutral-regression"
    if neutral and mean(patch.delta_e_after for patch in neutral) > mean(
        baseline_neutral_by_zone[patch.zone].delta_e_after for patch in neutral
    ) + 0.10:
        return "neutral-mean-regression"
    if sum(patch.regression > config.regression_epsilon for patch in colors) > config.max_regressed_patches:
        return "regression-count"
    candidate_pass = _pass_rates(candidate.patch_results)
    baseline_pass = _pass_rates(baseline.patch_results)
    if any(after < before for after, before in zip(candidate_pass.after_counts, baseline_pass.after_counts)):
        return "pass-rate"
    before_ratio = _saturation_ratio(baseline.patch_results, after=True)
    after_ratio = _saturation_ratio(candidate.patch_results, after=True)
    if abs(after_ratio - config.saturation_factor) > abs(before_ratio - config.saturation_factor) + 0.008:
        return "global-saturation"
    if after_ratio > max(config.saturation_factor + config.saturation_tolerance, before_ratio + 0.01):
        return "over-saturation"
    for patch in colors:
        target = saturation_target_chroma(patch.chroma_ideal, config.saturation_factor)
        before = next(item for item in baseline.patch_results if item.zone == patch.zone)
        allowed_growth = config.focus_delta_c_regression if patch.zone in config.focus_patches else 1.0
        if patch.chroma_after - target > max(
            config.local_saturation_tolerance,
            before.chroma_after - target + allowed_growth,
        ):
            return "local-saturation"
    focus = [patch for patch in colors if patch.zone in config.focus_patches]
    baseline_focus = [patch for patch in baseline.patch_results if patch.zone in config.focus_patches]
    if focus and baseline_focus:
        focus_score = mean(patch.delta_e_after + 0.18 * abs(patch.delta_c_after) + 0.04 * abs(patch.delta_h_after) for patch in focus)
        baseline_score = mean(patch.delta_e_after + 0.18 * abs(patch.delta_c_after) + 0.04 * abs(patch.delta_h_after) for patch in baseline_focus)
        if focus_score >= baseline_score - 1e-4:
            return "focus-no-improvement"
    return None


def _refine_candidate(
    best: _Candidate,
    baseline: _Candidate,
    dataset: ImatestDataset,
    original: Matrix3,
    composition: str,
    config: OptimizationConfig,
    rejected: Counter[str],
) -> _Candidate:
    current = best
    for base_step in (0.018, 0.009, 0.004, 0.002, 0.001):
        step = base_step * config.max_blend
        for _pass in range(18):
            changed = False
            for row in range(3):
                for col in range(2):
                    for direction in (-1.0, 1.0):
                        rows = [list(values) for values in current.correction]
                        rows[row][col] += direction * step
                        rows[row][2] -= direction * step
                        proposal: Matrix3 = tuple(tuple(values) for values in rows)  # type: ignore[assignment]
                        candidate = _evaluate_candidate(
                            dataset, original, proposal, composition, config,
                            current.regularization, current.blend,
                        )
                        failure = _protection_failure(candidate, baseline, config)
                        if failure:
                            rejected[failure] += 1
                            continue
                        if candidate.loss.total + 1e-9 < current.loss.total:
                            current = replace(candidate, search_method="refined")
                            changed = True
            if not changed:
                break
    return current


def _fallback_score(candidate: _Candidate, config: OptimizationConfig) -> float:
    """Continuous objective used to cross an initially infeasible boundary.

    Hard Regression Protection is still applied before a fallback candidate can
    be returned.  This soft score only lets coordinate descent move through an
    intermediate state that is not itself releasable.
    """

    colors = [patch for patch in candidate.patch_results if patch.category != "Neutral"]
    score = mean(patch.delta_e_after for patch in colors)
    for patch in colors:
        if patch.zone in config.focus_patches:
            limit = config.focus_patch_regression
        elif patch.delta_e_before < 3.0:
            limit = config.good_patch_regression
        else:
            limit = config.max_patch_regression
        score += 80.0 * max(0.0, patch.regression - limit) ** 2
        score += max(0.0, patch.regression - config.regression_epsilon) ** 2
        if patch.zone in config.focus_patches:
            score += 80.0 * max(
                0.0,
                abs(patch.delta_c_after) - abs(patch.delta_c_before) - config.focus_delta_c_regression,
            ) ** 2
            score += 10.0 * max(
                0.0,
                abs(patch.delta_h_after) - abs(patch.delta_h_before) - config.focus_delta_h_regression,
            ) ** 2
    neutral_limit = min(config.good_patch_regression, NEUTRAL_PATCH_REGRESSION_LIMIT)
    for patch in candidate.patch_results:
        if patch.zone in NEUTRAL_PATCHES:
            score += 80.0 * max(0.0, patch.regression - neutral_limit) ** 2
    score += 2.0 * abs(_saturation_ratio(candidate.patch_results, after=True) - config.saturation_factor)
    score += 0.05 * candidate.health.smoothness ** 2
    if candidate.health.status == "FAIL":
        score += 10000.0
    return score


def _engineering_boundary_search(
    dataset: ImatestDataset,
    original: Matrix3,
    composition: str,
    config: OptimizationConfig,
    baseline: _Candidate,
    seed: Optional[_Candidate],
    rejected: Counter[str],
) -> Optional[_Candidate]:
    """Search final CCM coefficients directly while preserving each row sum.

    Searching all three coefficient-pair directions is important when a row is
    already touching a coefficient bound: it permits movement *along* that
    boundary instead of getting stuck at the clipped projection.
    """

    projected = project_row_sum_and_bounds(original, config.coefficient_min, config.coefficient_max)
    correction = _correction_for_final(original, projected, composition)
    current = _evaluate_candidate(
        dataset, original, correction, composition, config,
        config.regularization or AUTO_REGULARIZATION[-1], 0.0,
    )
    if seed is not None and _fallback_score(seed, config) < _fallback_score(current, config):
        current = seed

    for step in (0.30, 0.15, 0.075, 0.035, 0.015, 0.007, 0.003, 0.0015, 0.0007):
        for _iteration in range(150):
            best_step = current
            best_score = _fallback_score(current, config)
            for row in range(3):
                for first, second in ((0, 1), (0, 2), (1, 2)):
                    for direction in (-1.0, 1.0):
                        rows = [list(values) for values in current.optimized]
                        rows[row][first] += direction * step
                        rows[row][second] -= direction * step
                        if min(rows[row]) < config.coefficient_min or max(rows[row]) > config.coefficient_max:
                            continue
                        final: Matrix3 = tuple(tuple(values) for values in rows)  # type: ignore[assignment]
                        proposal = _evaluate_candidate(
                            dataset,
                            original,
                            _correction_for_final(original, final, composition),
                            composition,
                            config,
                            current.regularization,
                            current.blend,
                        )
                        if proposal.health.status == "FAIL":
                            continue
                        proposal_score = _fallback_score(proposal, config)
                        if proposal_score + 1e-10 < best_score:
                            best_step = proposal
                            best_score = proposal_score
            if best_step is current:
                break
            current = best_step

    failure = _protection_failure(current, baseline, config)
    if failure:
        # A direct boundary optimum can sit just beyond a regression gate.  A
        # deterministic backoff along the already-computed Delta CCM direction
        # preserves the fit semantics while finding the strongest safe point;
        # this is especially important for Neutral Patch 19-24 protection.
        safe_candidates: list[_Candidate] = []
        for fraction in (0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05):
            backed_off = _evaluate_candidate(
                dataset,
                original,
                matrix_blend(current.correction, fraction),
                composition,
                config,
                current.regularization,
                fraction,
            )
            backoff_failure = _protection_failure(backed_off, baseline, config)
            if backoff_failure:
                rejected[f"backoff-{backoff_failure}"] += 1
                continue
            safe_candidates.append(backed_off)
        if safe_candidates:
            return replace(min(safe_candidates, key=lambda item: item.loss.total), search_method="safety-backoff")
        rejected[f"boundary-{failure}"] += 1
        return None
    return replace(current, search_method="engineering-boundary")


def optimize_ccm(
    dataset: ImatestDataset,
    original_matrix: Matrix3,
    *,
    composition: str = "pre",
    regularization: Optional[float] = None,
    max_blend: Optional[float] = None,
    config: Optional[OptimizationConfig] = None,
) -> OptimizationResult:
    """Run protected, engineering-aware multi-objective Delta CCM optimization."""

    if composition not in {"pre", "post_transposed"}:
        raise OptimizationError(f"未知矩阵组合方式: {composition}")
    base_config = config or OptimizationConfig()
    if regularization is not None:
        base_config = replace(base_config, regularization=regularization)
    if max_blend is not None:
        base_config = replace(base_config, max_blend=max_blend)
    config = _strategy_config(base_config)
    config.validate()
    # Qualcomm XML commonly serializes nominal row sums as 0.999999.  Normalize
    # that representation before composition so both Delta CCM and final CCM
    # preserve the neutral axis exactly; the report still shows the source XML.
    working_original = project_row_sum_and_bounds(original_matrix, -1.0e9, 1.0e9)

    baseline = _evaluate_candidate(
        dataset, working_original, identity_matrix(), composition, config,
        config.regularization or AUTO_REGULARIZATION[-1], 0.0,
        enforce_bounds=False,
        search_method="baseline",
    )
    rejected: Counter[str] = Counter()
    best: Optional[_Candidate] = None
    regularizations = (config.regularization,) if config.regularization is not None else AUTO_REGULARIZATION
    blend_count = max(1, int(round(config.max_blend / 0.05)))
    blend_steps = tuple(min(config.max_blend, 0.05 * index) for index in range(1, blend_count + 1))

    for lambda_value in regularizations:
        if lambda_value is None:
            continue
        raw = _fit_constrained_matrix(dataset, lambda_value, config)
        for blend in blend_steps:
            correction = matrix_blend(raw, blend)
            candidate = _evaluate_candidate(
                dataset, working_original, correction, composition, config,
                lambda_value, blend,
            )
            failure = _protection_failure(candidate, baseline, config)
            if failure:
                rejected[failure] += 1
                continue
            if best is None or candidate.loss.total < best.loss.total:
                best = candidate

    # If the source matrix itself violates the requested coefficient range, add
    # a deterministic row-sum-preserving repair seed to the candidate pool.
    projected_original = project_row_sum_and_bounds(working_original, config.coefficient_min, config.coefficient_max)
    if projected_original != working_original:
        repair_correction = _correction_for_final(working_original, projected_original, composition)
        repair = _evaluate_candidate(
            dataset, working_original, repair_correction, composition, config,
            config.regularization or AUTO_REGULARIZATION[-1], 0.0,
            search_method="range-repair",
        )
        failure = _protection_failure(repair, baseline, config)
        if failure:
            rejected[f"repair-{failure}"] += 1
        elif best is None or repair.loss.total < best.loss.total:
            best = repair

    # Least-squares rays can miss a valid solution at an ISP coefficient bound,
    # or retain identity when a low-light case needs coupled row movement.
    if best is None or best.blend == 0.0:
        boundary = _engineering_boundary_search(
            dataset, working_original, composition, config, baseline, best, rejected,
        )
        if boundary is not None and (best is None or boundary.loss.total < best.loss.total):
            best = boundary

    if best is None:
        if baseline.health.status == "FAIL":
            reasons = ", ".join(f"{name}={count}" for name, count in rejected.most_common())
            raise OptimizationError(f"没有候选同时满足 Matrix 工程约束与 Regression Protection：{reasons}")
        best = baseline
    elif best.blend > 0.0:
        best = _refine_candidate(best, baseline, dataset, working_original, composition, config, rejected)

    patch_results = best.patch_results
    color_results = [patch for patch in patch_results if patch.category != "Neutral"]
    before_errors = [patch.delta_e_before for patch in color_results]
    after_errors = [patch.delta_e_after for patch in color_results]
    pass_rates = _pass_rates(patch_results)
    before_ratio = _saturation_ratio(patch_results, after=False)
    after_ratio = _saturation_ratio(patch_results, after=True)
    diagnostics = build_module_diagnostics(
        patch_results,
        before_ratio,
        after_ratio,
        best.health,
        config.saturation_factor,
    )
    warnings = list(dataset.warnings)
    warnings.extend(check.message + f" [{check.value}]" for check in best.health.checks if check.status != "PASS")
    if any(patch.regression_status == "WARNING" for patch in color_results):
        warnings.append("存在轻微 Patch trade-off，但均未越过 Regression Protection 硬阈值。")
    if best.search_method == "baseline":
        warnings.append("没有安全候选优于原矩阵，已保留原矩阵。")
    if abs(after_ratio - config.saturation_factor) > config.saturation_tolerance:
        warnings.append(f"After Chroma ratio={after_ratio:.3f}，仍偏离目标 {config.saturation_factor:.3f}。")

    # The solver composes against a ppm-normalized copy of the source matrix so
    # Row Sum remains exact.  Re-express the displayed/exported Delta correction
    # against the literal XML matrix; this makes the stated A×M (or M×Aᵀ)
    # relationship numerically exact instead of differing by XML rounding ppm.
    result_correction = _correction_for_final(original_matrix, best.optimized, composition)

    accepted_count = len(regularizations) * len(blend_steps) - sum(rejected.values())
    explainability = (
        f"Strategy={config.strategy}; 搜索 Regularization={list(regularizations)}，blend <= {config.max_blend:.2f}。",
        f"重点 Patch={','.join(map(str, config.focus_patches))}，额外权重={config.focus_weight:.2f}；同时保护 dE/dC/dh。",
        f"Saturation target={config.saturation_factor:.3f}; Chroma ratio {before_ratio:.3f}->{after_ratio:.3f}。",
        f"候选接受约 {max(0, accepted_count)}，拒绝 {sum(rejected.values())}；主要原因：{dict(rejected.most_common(5))}。",
        f"选择 method={best.search_method}, lambda={best.regularization:g}, blend={best.blend:.2f}, loss={baseline.loss.total:.3f}->{best.loss.total:.3f}。",
        "Delta correction 已按原 XML 矩阵重新表达，严格满足 "
        + ("M_new=A×M_old。" if composition == "pre" else "M_new=M_old×Aᵀ。"),
        f"Matrix {best.health.status}: coeff=[{best.health.coefficient_min:.3f},{best.health.coefficient_max:.3f}], cond={best.health.condition_number:.3f}, det={best.health.determinant:.4f}。",
    )
    return OptimizationResult(
        correction_matrix=result_correction,
        original_matrix=original_matrix,
        optimized_matrix=best.optimized,
        composition=composition,
        regularization=best.regularization,
        blend=best.blend,
        strategy=config.strategy,
        search_method=best.search_method,
        saturation_factor=config.saturation_factor,
        patch_results=patch_results,
        mean_before=mean(before_errors),
        mean_after=mean(after_errors),
        max_before=max(before_errors),
        max_after=max(after_errors),
        improved_count=sum(patch.delta_e_after < patch.delta_e_before - 1e-9 for patch in color_results),
        regressed_count=sum(patch.regression > config.regression_epsilon for patch in color_results),
        pass_rates=pass_rates,
        category_statistics=_category_statistics(patch_results),
        saturation_ratio_before=before_ratio,
        saturation_ratio_after=after_ratio,
        matrix_health=best.health,
        loss_before=baseline.loss,
        loss_after=best.loss,
        diagnostics=diagnostics,
        rejected_candidates=dict(rejected),
        explainability=explainability,
        warnings=warnings,
    )
