from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from .models import OptimizationConfig


SETTINGS_VERSION = 2


# These descriptions are persisted next to the values in settings.json.  They
# deliberately live in Python rather than JSON comments, because comments make
# the file invalid JSON and break standard tooling.
_OPTIMIZATION_DESCRIPTION_SPEC: dict[str, tuple[str, str, str]] = {
    "strategy": ("选择优化搜索的保守程度。", "conservative | balanced | aggressive", "越激进越可能获得更大改善，同时更接近工程保护边界。"),
    "regularization": ("控制 CCM 相对原矩阵的正则化强度；Auto 表示自动搜索。", "Auto，或 0.0001-1.0", "数值越大，矩阵越平滑、变化越小。"),
    "max_blend": ("限制 Delta CCM 应用到原矩阵的最大比例。", "0.20-1.00", "降低可减少回退风险，提高可扩大优化幅度。"),
    "coefficient_min": ("设置最终矩阵允许的最小系数。", "-3.0 至 -0.1，且小于 coefficient_max", "提高下限可抑制异常负系数，但会缩小搜索空间。"),
    "coefficient_max": ("设置最终矩阵允许的最大系数。", "0.1 至 3.0，且大于 coefficient_min", "降低上限可抑制异常增益，但会缩小搜索空间。"),
    "saturation_factor": ("设置目标饱和度相对 Ideal 的系数。", "0.90-1.10；通常保持 1.0", "大于 1 会提高目标彩度，小于 1 会降低目标彩度。"),
    "focus_patches": ("指定优先优化并保护的 ColorChecker Patch。", "1-24 的编号列表；推荐 13,14,15", "列表内 Patch 会获得额外权重和更严格的回退保护。"),
    "focus_weight": ("设置重点 Patch 的附加优化权重。", "1.0-8.0；推荐 3.0-5.0", "权重越高越优先降低重点 Patch 的 ΔE、ΔC 和 Δh。"),
    "skin_weight": ("设置 Skin 分类 Patch 的基础权重。", "1.0-3.0", "提高会优先保护和改善肤色。"),
    "primary_weight": ("设置 Primary 分类 Patch 的基础权重。", "1.0-3.0", "提高会优先改善主色。"),
    "secondary_weight": ("设置 Secondary 分类 Patch 的基础权重。", "1.0-3.0", "提高会优先改善次生色。"),
    "memory_weight": ("设置 Memory Color 分类 Patch 的基础权重。", "1.0-3.0", "提高会优先保护视觉敏感的记忆色。"),
    "max_patch_regression": ("限制任一普通 Patch 允许的最大 ΔE00 回退。", "0.20-1.50", "降低会加强回退保护，但可能减少整体改善。"),
    "good_patch_regression": ("限制原本表现良好 Patch 的最大 ΔE00 回退。", "0.10-1.00", "降低会更严格保护已合格 Patch。"),
    "focus_patch_regression": ("限制重点 Patch 的最大 ΔE00 回退。", "0.05-0.50", "降低会更严格保护重点 Patch。"),
    "focus_delta_c_regression": ("限制重点 Patch 的 ΔC 退化量。", "0.20-2.00", "降低会更严格保护重点 Patch 彩度。"),
    "focus_delta_h_regression": ("限制重点 Patch 的 Δh 退化量。", "0.50-4.00 度", "降低会更严格保护重点 Patch 色相。"),
    "regression_epsilon": ("定义判断 Patch 改善或回退时忽略的数值噪声。", "0.01-0.20", "提高可减少微小波动被计为回退。"),
    "max_regressed_patches": ("限制一个候选中允许回退的 Patch 数量。", "0-8", "降低会加强全局回退保护。"),
    "saturation_tolerance": ("限制整体彩度比例偏离目标的容差。", "0.01-0.08", "降低会更严格抑制整体过饱和或欠饱和。"),
    "local_saturation_tolerance": ("限制单个 Patch 彩度偏离目标的容差。", "1.0-6.0", "降低会更严格抑制局部过饱和。"),
    "max_matrix_delta": ("限制最终矩阵相对原矩阵的最大系数变化。", "0.30-1.50", "降低可提升工程稳定性，但可能减少优化空间。"),
    "condition_warning": ("设置矩阵条件数的 WARNING 阈值。", "8-20", "降低会更早提示数值敏感矩阵。"),
    "condition_fail": ("设置矩阵条件数的 FAIL 阈值。", "20-50，且大于 condition_warning", "降低会更严格拒绝病态矩阵。"),
    "determinant_warning": ("设置行列式绝对值的 WARNING 下限。", "0.03-0.15", "提高会更早提示接近奇异的矩阵。"),
    "determinant_fail": ("设置行列式绝对值的 FAIL 下限。", "0.005-0.05，且小于 determinant_warning", "提高会更严格拒绝接近奇异的矩阵。"),
    "fixed_point_fraction_bits": ("设置 ISP 定点矩阵模拟的小数位数。", "8-16；CC13 常用验证值为 12", "位数越少，量化误差越明显。"),
    "loss_delta_e": ("设置平均 ΔE00 在多目标 Loss 中的权重。", "0.5-3.0", "提高会更强调总体感知色差。"),
    "loss_delta_c": ("设置 ΔC 在多目标 Loss 中的权重。", "0.0-1.0", "提高会更强调彩度准确性。"),
    "loss_delta_h": ("设置 Δh 在多目标 Loss 中的权重。", "0.0-1.0", "提高会更强调色相准确性。"),
    "loss_delta_l": ("设置 ΔL 在多目标 Loss 中的权重。", "0.0-0.5", "提高会让 CC 承担更多亮度误差，通常应保持较低。"),
    "loss_p90": ("设置高分位色差 P90 在多目标 Loss 中的权重。", "0.0-0.5", "提高会更重视最差一组 Patch。"),
    "loss_regression": ("设置 Patch 回退惩罚的权重。", "1.0-8.0", "提高会优先避免以局部退化换取平均值改善。"),
    "loss_saturation": ("设置整体和局部饱和度惩罚的权重。", "0.5-4.0", "提高会更严格避免通过增饱和降低 ΔE00。"),
    "loss_matrix": ("设置矩阵相对原值变化量的 Loss 权重。", "0.0-0.5", "提高会让新矩阵更接近原矩阵。"),
    "loss_smoothness": ("设置矩阵系数平滑性的 Loss 权重。", "0.0-0.5", "提高会抑制行内剧烈摆动。"),
    "loss_engineering": ("设置工程健康指标在多目标 Loss 中的权重。", "0.0-1.0", "提高会优先选择条件数、行列式等更稳健的矩阵。"),
}

_APPLICATION_DESCRIPTION_SPEC: dict[str, tuple[str, str, str]] = {
    "composition": ("设置 Delta CCM 与原 Qualcomm 矩阵的组合约定。", "pre | post_transposed；CC13 推荐 pre", "选择错误会导致 XML 系数排列与平台约定不一致。"),
    "show_motion": ("控制 TuneLab a*b* 图是否显示运动方向箭头。", "true | false", "关闭后仍保留 Ideal 与 Camera 的基础连线；只影响显示，不会重新运行优化或改变数据。"),
    "last_report_format": ("记录最近一次工程报告格式。", "html | pdf | xlsx | csv", "用于下次导出时选择默认扩展名，只影响文件对话框。"),
}


def _json_value(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _build_descriptions() -> dict[str, Any]:
    defaults = OptimizationConfig()
    optimization_fields = set(defaults.__dataclass_fields__)
    if optimization_fields != set(_OPTIMIZATION_DESCRIPTION_SPEC):
        missing = sorted(optimization_fields - set(_OPTIMIZATION_DESCRIPTION_SPEC))
        extra = sorted(set(_OPTIMIZATION_DESCRIPTION_SPEC) - optimization_fields)
        raise RuntimeError(f"settings 描述与 OptimizationConfig 不一致：missing={missing}, extra={extra}")

    optimization_descriptions: dict[str, Any] = {}
    for name in defaults.__dataclass_fields__:
        purpose, recommended_range, impact = _OPTIMIZATION_DESCRIPTION_SPEC[name]
        optimization_descriptions[name] = {
            "purpose": purpose,
            "default": _json_value(getattr(defaults, name)),
            "recommended_range": recommended_range,
            "impact": impact,
        }

    application_defaults = {
        "composition": "pre",
        "show_motion": True,
        "last_report_format": "html",
    }
    application_descriptions: dict[str, Any] = {}
    for name, default in application_defaults.items():
        purpose, recommended_range, impact = _APPLICATION_DESCRIPTION_SPEC[name]
        application_descriptions[name] = {
            "purpose": purpose,
            "default": default,
            "recommended_range": recommended_range,
            "impact": impact,
        }
    return {
        "schema": {
            "version": {
                "purpose": "标识 settings.json 的结构版本，用于兼容迁移。",
                "default": SETTINGS_VERSION,
                "recommended_range": f"固定为 {SETTINGS_VERSION}，由程序自动维护",
                "impact": "只影响配置文件解析兼容性，不影响优化结果。",
            }
        },
        "optimization": optimization_descriptions,
        "application": application_descriptions,
    }


def _platform_data_dir(application_name: str) -> Path:
    system = platform.system()
    if system == "Windows":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / application_name
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / application_name
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / application_name.lower()


def application_data_dir() -> Path:
    """Return the current TuneLab application-data directory."""

    return _platform_data_dir("TuneLab")


def legacy_application_data_dir() -> Path:
    """Return the pre-TuneLab data directory used for compatibility reads."""

    return _platform_data_dir("MatrixCorrect")


def existing_application_data_file(filename: str) -> Path:
    """Prefer TuneLab data, falling back to an existing legacy file."""

    current = application_data_dir() / filename
    legacy = legacy_application_data_dir() / filename
    if current.exists() or not legacy.exists():
        return current
    return legacy


@dataclass(frozen=True)
class CcmSettings:
    optimization: OptimizationConfig = field(default_factory=OptimizationConfig)
    composition: str = "pre"
    show_motion: bool = True
    last_report_format: str = "html"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SETTINGS_VERSION,
            "values": {
                "optimization": self.optimization.to_dict(),
                "composition": self.composition,
                "show_motion": self.show_motion,
                "last_report_format": self.last_report_format,
            },
            "descriptions": _build_descriptions(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CcmSettings":
        values = payload.get("values", payload)
        if not isinstance(values, dict):
            values = {}
        composition = str(values.get("composition", "pre"))
        if composition not in {"pre", "post_transposed"}:
            composition = "pre"
        report_format = str(values.get("last_report_format", "html")).lower()
        if report_format not in {"csv", "html", "pdf", "xlsx"}:
            report_format = "html"
        optimization_payload = values.get("optimization", {})
        if not isinstance(optimization_payload, dict):
            optimization_payload = {}
        return cls(
            optimization=OptimizationConfig.from_dict(optimization_payload),
            composition=composition,
            show_motion=bool(values.get("show_motion", True)),
            last_report_format=report_format,
        )


def load_settings(path: Optional[Union[str, Path]] = None) -> CcmSettings:
    settings_path = Path(path) if path is not None else existing_application_data_file("settings.json")
    if not settings_path.exists():
        return CcmSettings()
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return CcmSettings()
        return CcmSettings.from_dict(payload)
    except (OSError, ValueError, TypeError):
        return CcmSettings()


def save_settings(settings: CcmSettings, path: Optional[Union[str, Path]] = None) -> Path:
    settings_path = Path(path) if path is not None else application_data_dir() / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_suffix(settings_path.suffix + ".tmp")
    temporary.write_text(json.dumps(settings.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(settings_path)
    return settings_path
