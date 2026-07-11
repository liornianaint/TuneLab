from __future__ import annotations

import math
from statistics import mean

from .color import (
    delta_e_2000,
    hue_difference_degrees,
    identity_matrix,
    lab_chroma,
    linear_to_srgb,
    mat_mul,
    mat_vec,
    matrix_blend,
    row_sums,
    srgb_to_lab,
    srgb_to_linear,
)
from .models import ImatestDataset, Matrix3, OptimizationResult, PatchResult, Vector3


class OptimizationError(ValueError):
    pass


AUTO_REGULARIZATION = (0.0001, 0.0003, 0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0)


def transpose(matrix: Matrix3) -> Matrix3:
    return tuple(tuple(matrix[col][row] for col in range(3)) for row in range(3))  # type: ignore[return-value]


def _solve_linear(system: list[list[float]], right: list[float]) -> list[float]:
    size = len(right)
    augmented = [system[row][:] + [right[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise OptimizationError("矩阵拟合方程奇异；请检查 CSV 色块数据或提高正则化。")
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


def _patch_weight(zone: int) -> float:
    if zone in (1, 2):
        return 1.35
    if 13 <= zone <= 18:
        return 1.15
    return 1.0


def _fit_constrained_matrix(dataset: ImatestDataset, regularization: float) -> Matrix3:
    patches = [patch for patch in dataset.patches if patch.zone <= 18]
    if len(patches) < 9:
        raise OptimizationError("可用于 CC 拟合的彩色色块不足。")
    measured = [srgb_to_linear(patch.measured_srgb) for patch in patches]
    raw_ideal = [srgb_to_linear(patch.ideal_srgb) for patch in patches]
    # CC should correct chromaticity after exposure/AWB/Gamma are stable.  Imatest
    # JPEGs often still carry luminance error that a neutral-preserving CCM cannot
    # fix.  Match every target's linear luminance to the measured patch so that
    # brightness error is routed to Gamma/TMC instead of distorting the CCM fit.
    luma_weights = (0.2126729, 0.7151522, 0.0721750)
    ideal: list[Vector3] = []
    for source, target in zip(measured, raw_ideal):
        source_luma = sum(value * weight for value, weight in zip(source, luma_weights))
        target_luma = sum(value * weight for value, weight in zip(target, luma_weights))
        scale_factor = source_luma / target_luma if target_luma > 1e-12 else 1.0
        ideal.append(tuple(value * scale_factor for value in target))  # type: ignore[arg-type]
    weights = [_patch_weight(patch.zone) for patch in patches]
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
    corrected_srgb = linear_to_srgb(corrected_linear)
    return corrected_srgb, corrected_linear, overflow


def _candidate_score(dataset: ImatestDataset, correction: Matrix3) -> float:
    errors: list[float] = []
    weighted_total = 0.0
    total_weight = 0.0
    regressions = 0.0
    overflow = 0.0
    for patch in dataset.patches:
        after, _, patch_overflow = _predict(patch.measured_srgb, correction)
        overflow += patch_overflow
        before_error = delta_e_2000(srgb_to_lab(patch.measured_srgb), srgb_to_lab(patch.ideal_srgb))
        after_error = delta_e_2000(srgb_to_lab(after), srgb_to_lab(patch.ideal_srgb))
        if patch.zone <= 18:
            weight = _patch_weight(patch.zone)
            errors.append(after_error)
            weighted_total += weight * after_error
            total_weight += weight
            regressions += max(0.0, after_error - before_error - 0.5)
    if not errors:
        return float("inf")
    sorted_errors = sorted(errors)
    p90 = sorted_errors[min(len(sorted_errors) - 1, math.ceil(len(sorted_errors) * 0.9) - 1)]
    magnitude = sum((correction[row][col] - (1.0 if row == col else 0.0)) ** 2 for row in range(3) for col in range(3))
    return (
        weighted_total / total_weight
        + 0.12 * p90
        + 0.20 * regressions / len(errors)
        + 6.0 * overflow / max(len(dataset.patches), 1)
        + 0.025 * magnitude
    )


def _choose_correction(
    dataset: ImatestDataset,
    regularization: float | None,
    max_blend: float,
) -> tuple[Matrix3, float, float]:
    if not (0.05 <= max_blend <= 1.0):
        raise OptimizationError("最大优化强度必须在 0.05 到 1.0 之间。")
    regularizations = (regularization,) if regularization is not None else AUTO_REGULARIZATION
    blend_steps = sorted({max_blend * step / 10.0 for step in range(1, 11)} | {max_blend})
    best_matrix = identity_matrix()
    best_regularization = regularizations[0]
    best_blend = 0.0
    best_score = _candidate_score(dataset, best_matrix)
    for candidate_regularization in regularizations:
        if candidate_regularization is None or candidate_regularization <= 0:
            raise OptimizationError("正则化参数必须大于 0。")
        raw_matrix = _fit_constrained_matrix(dataset, candidate_regularization)
        for blend in blend_steps:
            candidate = matrix_blend(raw_matrix, blend)
            if any(not (-15.99 <= value <= 15.99) for row in candidate for value in row):
                continue
            score = _candidate_score(dataset, candidate)
            if score < best_score:
                best_score = score
                best_matrix = candidate
                best_regularization = candidate_regularization
                best_blend = blend
    best_matrix = _refine_delta_e(dataset, best_matrix, best_score, max_blend)
    return best_matrix, float(best_regularization), best_blend


def _refine_delta_e(
    dataset: ImatestDataset,
    initial: Matrix3,
    initial_score: float,
    max_blend: float,
) -> Matrix3:
    """Coordinate-refine the six row-sum-preserving degrees of freedom in ΔE00."""

    best = initial
    best_score = initial_score
    identity = identity_matrix()
    max_element_delta = 0.60 * max_blend + 0.01
    for base_step in (0.030, 0.015, 0.008, 0.004, 0.002, 0.001):
        step = base_step * max_blend
        for _pass in range(40):
            changed = False
            for row in range(3):
                for col in range(2):
                    for direction in (-1.0, 1.0):
                        candidate_rows = [list(values) for values in best]
                        candidate_rows[row][col] += direction * step
                        candidate_rows[row][2] -= direction * step
                        candidate: Matrix3 = tuple(tuple(values) for values in candidate_rows)  # type: ignore[assignment]
                        if any(
                            abs(candidate[r][c] - identity[r][c]) > max_element_delta
                            for r in range(3)
                            for c in range(3)
                        ):
                            continue
                        score = _candidate_score(dataset, candidate)
                        if score + 1e-10 < best_score:
                            best = candidate
                            best_score = score
                            changed = True
            if not changed:
                break
    return best


def _module_hint(zone: int, measured_lab: Vector3, ideal_lab: Vector3) -> str:
    delta_l = measured_lab[0] - ideal_lab[0]
    delta_c = lab_chroma(measured_lab) - lab_chroma(ideal_lab)
    delta_h = hue_difference_degrees(measured_lab, ideal_lab)
    if zone >= 19:
        if lab_chroma(measured_lab) > 3.0:
            return "AWB/CC：中性色偏"
        return "Gamma/曝光：中性亮度"
    if zone in (1, 2) and abs(delta_h) >= 4.0:
        return "SCE/2D LUT：肤色局部"
    if abs(delta_l) > max(5.0, abs(delta_c) * 1.5):
        return "Gamma/TMC/曝光：亮度主导"
    if abs(delta_c) >= 4.0 and abs(delta_h) < 5.0:
        return "CV/饱和度：Chroma 主导"
    if abs(delta_h) >= 6.0:
        return "CC；若仅单色异常则 2D LUT/SCE"
    return "CC：全局通道串扰"


def _build_patch_results(dataset: ImatestDataset, correction: Matrix3) -> list[PatchResult]:
    results: list[PatchResult] = []
    for patch in dataset.patches:
        after_srgb, _, _ = _predict(patch.measured_srgb, correction)
        before_lab = srgb_to_lab(patch.measured_srgb)
        after_lab = srgb_to_lab(after_srgb)
        ideal_lab = srgb_to_lab(patch.ideal_srgb)
        before_error = delta_e_2000(before_lab, ideal_lab)
        after_error = delta_e_2000(after_lab, ideal_lab)
        improvement = 0.0 if before_error <= 1e-12 else (before_error - after_error) / before_error * 100.0
        results.append(
            PatchResult(
                zone=patch.zone,
                name=patch.display_name(),
                before_srgb=patch.measured_srgb,
                after_srgb=after_srgb,
                ideal_srgb=patch.ideal_srgb,
                before_lab=before_lab,
                after_lab=after_lab,
                ideal_lab=ideal_lab,
                delta_e_before=before_error,
                delta_e_after=after_error,
                improvement_percent=improvement,
                delta_l_before=before_lab[0] - ideal_lab[0],
                delta_c_before=lab_chroma(before_lab) - lab_chroma(ideal_lab),
                delta_h_before=(0.0 if patch.zone >= 19 else hue_difference_degrees(before_lab, ideal_lab)),
                module_hint=_module_hint(patch.zone, before_lab, ideal_lab),
            )
        )
    return results


def optimize_ccm(
    dataset: ImatestDataset,
    original_matrix: Matrix3,
    *,
    composition: str = "pre",
    regularization: float | None = None,
    max_blend: float = 1.0,
) -> OptimizationResult:
    """Fit a neutral-preserving delta CCM and compose it with a Qualcomm matrix.

    ``pre`` assumes column vectors and writes ``M_new = A @ M_old``.
    ``post_transposed`` is the equivalent row-vector/C7 convention and writes
    ``M_new = M_old @ A.T``.
    """

    if composition not in {"pre", "post_transposed"}:
        raise OptimizationError(f"未知矩阵组合方式: {composition}")
    correction, selected_regularization, blend = _choose_correction(dataset, regularization, max_blend)
    optimized_matrix = (
        mat_mul(correction, original_matrix)
        if composition == "pre"
        else mat_mul(original_matrix, transpose(correction))
    )
    patch_results = _build_patch_results(dataset, correction)
    color_results = [result for result in patch_results if result.zone <= 18]
    before_errors = [result.delta_e_before for result in color_results]
    after_errors = [result.delta_e_after for result in color_results]
    warnings = list(dataset.warnings)
    original_sums = row_sums(original_matrix)
    optimized_sums = row_sums(optimized_matrix)
    if any(abs(value - 1.0) > 0.02 for value in original_sums):
        warnings.append(f"原 CC 矩阵行和偏离 1: {', '.join(f'{value:.4f}' for value in original_sums)}")
    if any(abs(value - 1.0) > 0.02 for value in optimized_sums):
        warnings.append(f"改后矩阵行和偏离 1: {', '.join(f'{value:.4f}' for value in optimized_sums)}")
    neutral_results = [result for result in patch_results if result.zone >= 19]
    if neutral_results:
        neutral_chroma = mean(lab_chroma(result.before_lab) for result in neutral_results)
        neutral_lightness_error = mean(abs(result.delta_l_before) for result in neutral_results)
        if neutral_chroma > 3.0:
            warnings.append("中性色平均 Chroma 偏高；建议先稳定 AWB，再接受 CC 结果。")
        if neutral_lightness_error > 5.0:
            warnings.append("中性色亮度误差较大；CC 无法替代曝光/Gamma 调整。")
    if blend == 0.0:
        warnings.append("自动搜索未找到稳健改善，已保留原矩阵；请先检查 AWB、Gamma 与 CSV 拍摄条件。")
    if any(result.delta_e_after - result.delta_e_before > 2.0 for result in color_results):
        warnings.append("部分色块回退超过 ΔE00 2；建议降低优化强度或改用 2D LUT/SCE 做局部修正。")

    return OptimizationResult(
        correction_matrix=correction,
        original_matrix=original_matrix,
        optimized_matrix=optimized_matrix,
        composition=composition,
        regularization=selected_regularization,
        blend=blend,
        patch_results=patch_results,
        mean_before=mean(before_errors),
        mean_after=mean(after_errors),
        max_before=max(before_errors),
        max_after=max(after_errors),
        improved_count=sum(result.delta_e_after < result.delta_e_before for result in color_results),
        regressed_count=sum(result.delta_e_after > result.delta_e_before for result in color_results),
        warnings=warnings,
    )
