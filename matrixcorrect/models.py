from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


Vector3 = tuple[float, float, float]
Matrix3 = tuple[Vector3, Vector3, Vector3]


PATCH_NAMES_ZH = (
    "深肤色", "浅肤色", "蓝天", "叶绿", "蓝花", "蓝绿色",
    "橙色", "紫蓝", "中度红", "紫色", "黄绿色", "橙黄色",
    "蓝色", "绿色", "红色", "黄色", "洋红", "青色",
    "白色", "中性灰 8", "中性灰 6.5", "中性灰 5", "中性灰 3.5", "黑色",
)


PATCH_CATEGORIES = {
    1: "Skin", 2: "Skin",
    3: "Memory", 4: "Memory", 5: "Chromatic", 6: "Chromatic",
    7: "Memory", 8: "Chromatic", 9: "Memory", 10: "Chromatic",
    11: "Memory", 12: "Memory",
    13: "Primary", 14: "Primary", 15: "Primary",
    16: "Secondary", 17: "Secondary", 18: "Secondary",
    19: "Neutral", 20: "Neutral", 21: "Neutral",
    22: "Neutral", 23: "Neutral", 24: "Neutral",
}


@dataclass(frozen=True)
class ColorPatch:
    zone: int
    measured_srgb: Vector3
    ideal_srgb: Vector3
    name: str = ""

    def display_name(self) -> str:
        if self.name:
            return self.name
        if 1 <= self.zone <= len(PATCH_NAMES_ZH):
            return PATCH_NAMES_ZH[self.zone - 1]
        return f"色块 {self.zone}"

    @property
    def category(self) -> str:
        return PATCH_CATEGORIES.get(self.zone, "Other")


@dataclass
class ImatestDataset:
    source_path: Path
    patches: list[ColorPatch]
    image_name: str = ""
    run_date: str = ""
    color_space: str = "sRGB"
    inferred_cct: Optional[int] = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TriggerRange:
    name: str
    start: float
    end: float

    def contains(self, value: float) -> bool:
        return self.start <= value <= self.end

    def label(self) -> str:
        return f"{self.name}[{self.start:g}-{self.end:g}]"


@dataclass(frozen=True)
class CCRegion:
    index: int
    trigger_path: tuple[TriggerRange, ...]
    matrix: Matrix3
    offsets: tuple[int, int, int]

    @property
    def cct_range(self) -> Optional[TriggerRange]:
        for item in reversed(self.trigger_path):
            if item.name == "CCT":
                return item
        return None

    def path_label(self) -> str:
        return " | ".join(item.label() for item in self.trigger_path)


@dataclass(frozen=True)
class OptimizationConfig:
    strategy: str = "balanced"
    regularization: Optional[float] = None
    max_blend: float = 0.80
    coefficient_min: float = -3.0
    coefficient_max: float = 3.0
    saturation_factor: float = 1.0
    focus_patches: tuple[int, ...] = (13, 14, 15)
    focus_weight: float = 4.0
    skin_weight: float = 1.5
    primary_weight: float = 1.4
    secondary_weight: float = 1.25
    memory_weight: float = 1.1
    max_patch_regression: float = 0.75
    good_patch_regression: float = 0.50
    focus_patch_regression: float = 0.20
    focus_delta_c_regression: float = 1.10
    focus_delta_h_regression: float = 2.00
    regression_epsilon: float = 0.05
    max_regressed_patches: int = 6
    saturation_tolerance: float = 0.035
    local_saturation_tolerance: float = 3.0
    max_matrix_delta: float = 1.10
    condition_warning: float = 12.0
    condition_fail: float = 25.0
    determinant_warning: float = 0.08
    determinant_fail: float = 0.02
    fixed_point_fraction_bits: int = 12
    loss_delta_e: float = 1.0
    loss_delta_c: float = 0.28
    loss_delta_h: float = 0.32
    loss_delta_l: float = 0.05
    loss_p90: float = 0.12
    loss_regression: float = 3.0
    loss_saturation: float = 1.4
    loss_matrix: float = 0.12
    loss_smoothness: float = 0.16
    loss_engineering: float = 0.20

    def validate(self) -> None:
        if self.strategy not in {"conservative", "balanced", "aggressive"}:
            raise ValueError(f"未知 Optimization Strategy: {self.strategy}")
        if self.regularization is not None and self.regularization <= 0:
            raise ValueError("Regularization 必须大于 0，或使用 Auto。")
        if not 0.05 <= self.max_blend <= 1.0:
            raise ValueError("最大优化强度必须在 0.05 到 1.0 之间。")
        if self.coefficient_min >= self.coefficient_max:
            raise ValueError("Matrix coefficient 最小值必须小于最大值。")
        if not 0.5 <= self.saturation_factor <= 1.5:
            raise ValueError("饱和度系数必须在 0.5 到 1.5 之间。")
        if not self.focus_patches or any(not 1 <= zone <= 24 for zone in self.focus_patches):
            raise ValueError("重点 Patch 必须是 1-24 的编号。")
        if self.focus_weight < 1.0:
            raise ValueError("重点 Patch 权重不能小于 1。")
        if not 4 <= self.fixed_point_fraction_bits <= 20:
            raise ValueError("Fixed Point 小数位必须在 4-20 bits。")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["focus_patches"] = list(self.focus_patches)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OptimizationConfig":
        allowed = {field_.name for field_ in cls.__dataclass_fields__.values()}
        values = {key: value for key, value in payload.items() if key in allowed}
        if "focus_patches" in values:
            values["focus_patches"] = tuple(int(value) for value in values["focus_patches"])
        config = cls(**values)
        config.validate()
        return config


@dataclass(frozen=True)
class EngineeringCheck:
    name: str
    status: str
    value: str
    limit: str
    message: str


@dataclass(frozen=True)
class MatrixHealth:
    status: str
    checks: tuple[EngineeringCheck, ...]
    determinant: float
    condition_number: float
    rank: int
    row_sums: Vector3
    coefficient_min: float
    coefficient_max: float
    smoothness: float
    max_coefficient_delta: float
    fixed_point_matrix: Matrix3
    fixed_point_max_error: float
    fixed_point_max_delta_e: float


@dataclass(frozen=True)
class LossBreakdown:
    total: float
    delta_e: float
    delta_c: float
    delta_h: float
    delta_l: float
    p90: float
    regression: float
    saturation: float
    matrix_regularization: float
    smoothness: float
    engineering: float


@dataclass(frozen=True)
class PatchResult:
    zone: int
    name: str
    category: str
    priority_weight: float
    before_srgb: Vector3
    after_srgb: Vector3
    ideal_srgb: Vector3
    before_lab: Vector3
    after_lab: Vector3
    ideal_lab: Vector3
    delta_e_before: float
    delta_e_after: float
    improvement_percent: float
    delta_l_before: float
    delta_l_after: float
    delta_c_before: float
    delta_c_after: float
    delta_h_before: float
    delta_h_after: float
    chroma_before: float
    chroma_after: float
    chroma_ideal: float
    regression: float
    regression_status: str
    module_hint: str


@dataclass(frozen=True)
class PassRateStatistics:
    thresholds: tuple[float, ...]
    before_counts: tuple[int, ...]
    after_counts: tuple[int, ...]
    sample_count: int

    def before_rate(self, index: int) -> float:
        return self.before_counts[index] / self.sample_count if self.sample_count else 0.0

    def after_rate(self, index: int) -> float:
        return self.after_counts[index] / self.sample_count if self.sample_count else 0.0


@dataclass(frozen=True)
class CategoryStatistics:
    category: str
    count: int
    mean_before: float
    mean_after: float
    improved: int
    regressed: int
    pass_rate_before_3: float
    pass_rate_after_3: float


@dataclass(frozen=True)
class ModuleDiagnosis:
    module: str
    confidence: float
    severity: str
    root_cause: str
    evidence: tuple[str, ...]
    action: str


@dataclass
class OptimizationResult:
    correction_matrix: Matrix3
    original_matrix: Matrix3
    optimized_matrix: Matrix3
    composition: str
    regularization: float
    blend: float
    strategy: str
    search_method: str
    saturation_factor: float
    patch_results: list[PatchResult]
    mean_before: float
    mean_after: float
    max_before: float
    max_after: float
    improved_count: int
    regressed_count: int
    pass_rates: PassRateStatistics
    category_statistics: tuple[CategoryStatistics, ...]
    saturation_ratio_before: float
    saturation_ratio_after: float
    matrix_health: MatrixHealth
    loss_before: LossBreakdown
    loss_after: LossBreakdown
    diagnostics: tuple[ModuleDiagnosis, ...]
    rejected_candidates: dict[str, int]
    explainability: tuple[str, ...]
    warnings: list[str] = field(default_factory=list)

    @property
    def mean_improvement_percent(self) -> float:
        if self.mean_before <= 1e-12:
            return 0.0
        return (self.mean_before - self.mean_after) / self.mean_before * 100.0

    @property
    def regression_patches(self) -> tuple[PatchResult, ...]:
        return tuple(patch for patch in self.patch_results if patch.regression > 0.05)


@dataclass(frozen=True)
class OptimizationHistoryRecord:
    timestamp: str
    dataset_name: str
    region_label: str
    strategy: str
    mean_before: float
    mean_after: float
    pass_rate_before_3: float
    pass_rate_after_3: float
    matrix_status: str
    optimized_matrix: Matrix3
    search_method: str = ""
    xml_diff: str = ""
