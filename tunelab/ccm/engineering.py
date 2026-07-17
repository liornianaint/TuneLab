from __future__ import annotations

import math

from .color_science import delta_e_2000, identity_matrix, mat_vec, srgb_to_lab, srgb_to_linear, linear_to_srgb
from .models import EngineeringCheck, ImatestDataset, Matrix3, MatrixHealth, OptimizationConfig


def determinant(matrix: Matrix3) -> float:
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def inverse(matrix: Matrix3) -> Matrix3:
    det = determinant(matrix)
    if abs(det) < 1e-12:
        raise ValueError("Matrix is singular")
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    return (
        ((e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det),
        ((f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det),
        ((d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det),
    )


def frobenius_norm(matrix: Matrix3) -> float:
    return math.sqrt(sum(value * value for row in matrix for value in row))


def matrix_distance(first: Matrix3, second: Matrix3) -> float:
    return math.sqrt(sum((first[row][col] - second[row][col]) ** 2 for row in range(3) for col in range(3)))


def max_matrix_delta(first: Matrix3, second: Matrix3) -> float:
    return max(abs(first[row][col] - second[row][col]) for row in range(3) for col in range(3))


def condition_number(matrix: Matrix3) -> float:
    try:
        return frobenius_norm(matrix) * frobenius_norm(inverse(matrix))
    except ValueError:
        return float("inf")


def matrix_rank(matrix: Matrix3, tolerance: float = 1e-8) -> int:
    rows = [list(row) for row in matrix]
    rank = 0
    for column in range(3):
        pivot = max(range(rank, 3), key=lambda row: abs(rows[row][column]), default=rank)
        if abs(rows[pivot][column]) <= tolerance:
            continue
        rows[rank], rows[pivot] = rows[pivot], rows[rank]
        divisor = rows[rank][column]
        rows[rank] = [value / divisor for value in rows[rank]]
        for row in range(3):
            if row == rank:
                continue
            factor = rows[row][column]
            rows[row] = [rows[row][index] - factor * rows[rank][index] for index in range(3)]
        rank += 1
    return rank


def quantize_matrix(matrix: Matrix3, fraction_bits: int) -> Matrix3:
    scale = float(1 << fraction_bits)
    return tuple(tuple(round(value * scale) / scale for value in row) for row in matrix)  # type: ignore[return-value]


def project_row_sum_and_bounds(matrix: Matrix3, minimum: float, maximum: float) -> Matrix3:
    projected: list[tuple[float, float, float]] = []
    for row_index, source_row in enumerate(matrix):
        row = [max(minimum, min(maximum, value)) for value in source_row]
        for _ in range(12):
            residual = 1.0 - sum(row)
            if abs(residual) < 1e-10:
                break
            candidates = [
                index for index, value in enumerate(row)
                if (residual > 0 and value < maximum - 1e-12)
                or (residual < 0 and value > minimum + 1e-12)
            ]
            if not candidates:
                break
            # Prefer the diagonal term because it is the least surprising place
            # to absorb a tiny row-sum residual after coefficient clipping.
            candidates.sort(key=lambda index: (index != row_index, index))
            share = residual / len(candidates)
            for index in candidates:
                row[index] = max(minimum, min(maximum, row[index] + share))
        projected.append((row[0], row[1], row[2]))
    return tuple(projected)  # type: ignore[return-value]


def _status(value: float, pass_limit: float, warning_limit: float, *, lower_is_better: bool = True) -> str:
    if lower_is_better:
        if value <= pass_limit:
            return "PASS"
        if value <= warning_limit:
            return "WARNING"
        return "FAIL"
    if value >= pass_limit:
        return "PASS"
    if value >= warning_limit:
        return "WARNING"
    return "FAIL"


def evaluate_matrix_health(
    original: Matrix3,
    optimized: Matrix3,
    correction: Matrix3,
    dataset: ImatestDataset,
    config: OptimizationConfig,
) -> MatrixHealth:
    flat = [value for row in optimized for value in row]
    row_sums = tuple(sum(row) for row in optimized)
    min_value, max_value = min(flat), max(flat)
    det = determinant(optimized)
    cond = condition_number(optimized)
    rank = matrix_rank(optimized)
    smoothness = matrix_distance(original, optimized)
    max_delta = max_matrix_delta(original, optimized)
    fixed_matrix = quantize_matrix(optimized, config.fixed_point_fraction_bits)
    fixed_correction = quantize_matrix(correction, config.fixed_point_fraction_bits)
    fixed_error = max(abs(optimized[row][col] - fixed_matrix[row][col]) for row in range(3) for col in range(3))
    fixed_delta_e = 0.0
    for patch in dataset.patches:
        linear = srgb_to_linear(patch.measured_srgb)
        float_rgb = linear_to_srgb(mat_vec(correction, linear))
        fixed_rgb = linear_to_srgb(mat_vec(fixed_correction, linear))
        fixed_delta_e = max(fixed_delta_e, delta_e_2000(srgb_to_lab(float_rgb), srgb_to_lab(fixed_rgb)))

    coefficient_status = "PASS" if min_value >= config.coefficient_min - 1e-9 and max_value <= config.coefficient_max + 1e-9 else "FAIL"
    if config.allow_common_neutral_scale:
        neutral_scale = sum(row_sums) / 3.0
        row_error = max(abs(value - neutral_scale) for value in row_sums)
        row_pass_limit = 1.1e-6
        row_warning_limit = 1e-5
        row_name = "Neutral Axis / Row Sum"
        row_limit = "PASS spread <= 1.1e-6; FAIL > 1e-5"
        row_message = (
            f"三行和相等可保持中性轴；当前公共亮度尺度为 {neutral_scale:.7f}。"
        )
    else:
        neutral_scale = 1.0
        row_error = max(abs(value - 1.0) for value in row_sums)
        row_pass_limit = 1e-7
        row_warning_limit = 1e-5
        row_name = "Row Sum"
        row_limit = "PASS error <= 1e-7; FAIL > 1e-5"
        row_message = "每行和为 1，保持中性轴与名义亮度。"
    row_status = _status(row_error, row_pass_limit, row_warning_limit)
    condition_status = _status(cond, config.condition_warning, config.condition_fail)
    determinant_status = _status(abs(det), config.determinant_warning, config.determinant_fail, lower_is_better=False)
    rank_status = "PASS" if rank == 3 else "FAIL"
    smooth_status = _status(smoothness, config.max_matrix_delta * 0.65, config.max_matrix_delta)
    max_delta_status = _status(max_delta, config.max_matrix_delta * 0.65, config.max_matrix_delta)
    fixed_status = _status(fixed_delta_e, 0.02, 0.10)
    checks = (
        EngineeringCheck("Coefficient Range", coefficient_status, f"[{min_value:.5f}, {max_value:.5f}]", f"[{config.coefficient_min:g}, {config.coefficient_max:g}]", "所有最终 CC 系数必须在工程范围内。"),
        EngineeringCheck(
            row_name,
            row_status,
            ", ".join(f"{value:.7f}" for value in row_sums),
            row_limit,
            row_message,
        ),
        EngineeringCheck("Condition Number", condition_status, f"{cond:.4f}", f"PASS <= {config.condition_warning:g}; FAIL > {config.condition_fail:g}", "Frobenius condition number，越低越稳健。"),
        EngineeringCheck("Determinant", determinant_status, f"{det:.6f}", f"abs(det) >= {config.determinant_warning:g}", "过小表示矩阵接近奇异。"),
        EngineeringCheck("Rank", rank_status, str(rank), "3", "CCM 必须保持满秩。"),
        EngineeringCheck("Matrix Smoothness", smooth_status, f"L2={smoothness:.5f}", f"PASS <= {config.max_matrix_delta * 0.65:g}; FAIL > {config.max_matrix_delta:g}", "限制相对原矩阵的整体跳变。"),
        EngineeringCheck("Max Coefficient Delta", max_delta_status, f"{max_delta:.5f}", f"PASS <= {config.max_matrix_delta * 0.65:g}; FAIL > {config.max_matrix_delta:g}", "限制单个系数突变。"),
        EngineeringCheck("Fixed Point Simulation", fixed_status, f"max dE={fixed_delta_e:.5f}; coeff err={fixed_error:.7f}", "dE <= 0.10", f"Q{config.fixed_point_fraction_bits} 量化后的颜色漂移。"),
    )
    statuses = {check.status for check in checks}
    overall = "FAIL" if "FAIL" in statuses else "WARNING" if "WARNING" in statuses else "PASS"
    return MatrixHealth(
        status=overall,
        checks=checks,
        determinant=det,
        condition_number=cond,
        rank=rank,
        row_sums=row_sums,  # type: ignore[arg-type]
        coefficient_min=min_value,
        coefficient_max=max_value,
        smoothness=smoothness,
        max_coefficient_delta=max_delta,
        fixed_point_matrix=fixed_matrix,
        fixed_point_max_error=fixed_error,
        fixed_point_max_delta_e=fixed_delta_e,
    )


def correction_magnitude(correction: Matrix3) -> float:
    return matrix_distance(correction, identity_matrix())
