from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias


Vector3: TypeAlias = tuple[float, float, float]
Matrix3: TypeAlias = tuple[Vector3, Vector3, Vector3]


PATCH_NAMES_ZH = (
    "深肤色",
    "浅肤色",
    "蓝天",
    "叶绿",
    "蓝花",
    "蓝绿色",
    "橙色",
    "紫蓝",
    "中度红",
    "紫色",
    "黄绿色",
    "橙黄色",
    "蓝色",
    "绿色",
    "红色",
    "黄色",
    "洋红",
    "青色",
    "白色",
    "中性灰 8",
    "中性灰 6.5",
    "中性灰 5",
    "中性灰 3.5",
    "黑色",
)


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


@dataclass
class ImatestDataset:
    source_path: Path
    patches: list[ColorPatch]
    image_name: str = ""
    run_date: str = ""
    color_space: str = "sRGB"
    inferred_cct: int | None = None
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
    def cct_range(self) -> TriggerRange | None:
        for item in reversed(self.trigger_path):
            if item.name == "CCT":
                return item
        return None

    def path_label(self) -> str:
        return " | ".join(item.label() for item in self.trigger_path)


@dataclass(frozen=True)
class PatchResult:
    zone: int
    name: str
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
    delta_c_before: float
    delta_h_before: float
    module_hint: str


@dataclass
class OptimizationResult:
    correction_matrix: Matrix3
    original_matrix: Matrix3
    optimized_matrix: Matrix3
    composition: str
    regularization: float
    blend: float
    patch_results: list[PatchResult]
    mean_before: float
    mean_after: float
    max_before: float
    max_after: float
    improved_count: int
    regressed_count: int
    warnings: list[str] = field(default_factory=list)

    @property
    def mean_improvement_percent(self) -> float:
        if self.mean_before <= 1e-12:
            return 0.0
        return (self.mean_before - self.mean_after) / self.mean_before * 100.0

