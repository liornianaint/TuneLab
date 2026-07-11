from __future__ import annotations

import csv
from pathlib import Path

from .models import ImatestDataset, OptimizationResult


def save_analysis_csv(
    destination: str | Path,
    dataset: ImatestDataset,
    result: OptimizationResult,
    *,
    region_label: str = "",
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["MatrixCorrect 分析报告"])
        writer.writerow(["源 CSV", str(dataset.source_path)])
        writer.writerow(["图像", dataset.image_name])
        writer.writerow(["测试日期", dataset.run_date])
        writer.writerow(["色彩空间", dataset.color_space])
        writer.writerow(["CC region", region_label])
        writer.writerow(["矩阵组合", result.composition])
        writer.writerow(["正则化", f"{result.regularization:.7g}"])
        writer.writerow(["优化强度", f"{result.blend:.1%}"])
        writer.writerow(["彩色色块平均 ΔE00（改前）", f"{result.mean_before:.4f}"])
        writer.writerow(["彩色色块平均 ΔE00（改后）", f"{result.mean_after:.4f}"])
        writer.writerow(["平均改善", f"{result.mean_improvement_percent:.2f}%"])
        writer.writerow(["改善/回退色块数", result.improved_count, result.regressed_count])
        writer.writerow([])
        writer.writerow(["改前 CC 矩阵"])
        writer.writerows([[f"{value:.7f}" for value in row] for row in result.original_matrix])
        writer.writerow(["Delta correction 矩阵"])
        writer.writerows([[f"{value:.7f}" for value in row] for row in result.correction_matrix])
        writer.writerow(["改后 CC 矩阵"])
        writer.writerows([[f"{value:.7f}" for value in row] for row in result.optimized_matrix])
        writer.writerow([])
        writer.writerow(
            [
                "Zone",
                "色块",
                "ΔE00 改前",
                "ΔE00 改后",
                "改善百分比",
                "ΔL* 改前",
                "ΔC* 改前",
                "Δh° 改前",
                "建议模块",
                "R-meas",
                "G-meas",
                "B-meas",
                "R-sim",
                "G-sim",
                "B-sim",
                "R-ideal",
                "G-ideal",
                "B-ideal",
            ]
        )
        for patch in result.patch_results:
            writer.writerow(
                [
                    patch.zone,
                    patch.name,
                    f"{patch.delta_e_before:.4f}",
                    f"{patch.delta_e_after:.4f}",
                    f"{patch.improvement_percent:.2f}%",
                    f"{patch.delta_l_before:.4f}",
                    f"{patch.delta_c_before:.4f}",
                    f"{patch.delta_h_before:.4f}",
                    patch.module_hint,
                    *[f"{value:.6f}" for value in patch.before_srgb],
                    *[f"{value:.6f}" for value in patch.after_srgb],
                    *[f"{value:.6f}" for value in patch.ideal_srgb],
                ]
            )
        if result.warnings:
            writer.writerow([])
            writer.writerow(["警告"])
            for warning in result.warnings:
                writer.writerow([warning])
    return path

