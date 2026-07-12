from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models import TriggerRange


GammaLUT = tuple[int, ...]


@dataclass(frozen=True)
class GrayZone:
    zone: int
    pixel: float
    pixel_normalized: float
    log_exposure: float
    density: float
    density_r: Optional[float] = None
    density_g: Optional[float] = None
    density_b: Optional[float] = None
    mean_r: Optional[float] = None
    mean_g: Optional[float] = None
    mean_b: Optional[float] = None
    slope: Optional[float] = None
    slope_r: Optional[float] = None
    slope_g: Optional[float] = None
    slope_b: Optional[float] = None
    noise: Optional[float] = None
    noise_r: Optional[float] = None
    noise_g: Optional[float] = None
    noise_b: Optional[float] = None


@dataclass(frozen=True)
class GrayDataset:
    source_path: Path
    zones: tuple[GrayZone, ...]
    image_name: str = ""
    run_date: str = ""
    reported_gamma: Optional[float] = None
    middle_gray_zone: int = 8
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class GrayPair:
    from_zone: int
    to_zone: int
    delta_pixel: float
    distinguishable: bool


@dataclass(frozen=True)
class GrayRangeAnalysis:
    threshold: float
    pairs: tuple[GrayPair, ...]
    runs: tuple[tuple[int, ...], ...]
    selected_zones: tuple[int, ...]
    start_zone: Optional[int]
    end_zone: Optional[int]

    @property
    def effective_count(self) -> int:
        return len(self.selected_zones)


@dataclass(frozen=True)
class GammaRegion:
    index: int
    trigger_path: tuple[TriggerRange, ...]
    channel_r: GammaLUT
    channel_g: GammaLUT
    channel_b: GammaLUT
    maximum: int

    @property
    def cct_range(self) -> Optional[TriggerRange]:
        for item in reversed(self.trigger_path):
            if item.name == "CCT":
                return item
        return None

    @property
    def length(self) -> int:
        return len(self.channel_r)

    def path_label(self) -> str:
        return " | ".join(item.label() for item in self.trigger_path)


@dataclass(frozen=True)
class GammaOptimizationConfig:
    # UI semantics: 1.0 keeps nominal brightness; a larger factor lifts the
    # tone curve.  This is deliberately not the measured Density/Exposure
    # regression slope reported as "Global Gamma" below.
    target_gamma: float = 1.0
    target_step_count: Optional[int] = None
    maximum_adjustment: float = 0.70
    highlight_protection: float = 0.75
    shadow_protection: float = 0.75
    rgb_mode: str = "linked"
    threshold: float = 8.0
    range_mode: str = "auto"
    manual_start_zone: Optional[int] = None
    manual_end_zone: Optional[int] = None

    def validate(self) -> None:
        if not 0.25 <= self.target_gamma <= 4.0:
            raise ValueError("Gamma 提亮系数必须在 0.25 到 4.0 之间；1.0 表示保持标称亮度。")
        if self.target_step_count is not None and self.target_step_count < 3:
            raise ValueError("目标可识别阶数不能少于 3。")
        if not 0.0 <= self.maximum_adjustment <= 1.0:
            raise ValueError("最大调整强度必须在 0 到 1 之间。")
        if not 0.0 <= self.highlight_protection <= 1.0:
            raise ValueError("高光保护必须在 0 到 1 之间。")
        if not 0.0 <= self.shadow_protection <= 1.0:
            raise ValueError("暗部保护必须在 0 到 1 之间。")
        if self.rgb_mode not in {"linked", "independent"}:
            raise ValueError("RGB 模式必须是 linked 或 independent。")
        if self.range_mode not in {"auto", "all", "manual"}:
            raise ValueError("灰阶范围必须是 auto、all 或 manual。")
        if self.threshold < 0.0:
            raise ValueError("灰阶识别阈值不能小于 0。")
        if self.range_mode == "manual":
            if self.manual_start_zone is None or self.manual_end_zone is None:
                raise ValueError("手动灰阶范围需要起止 Zone。")
            if self.manual_start_zone > self.manual_end_zone:
                raise ValueError("手动灰阶起始 Zone 不能大于结束 Zone。")


@dataclass(frozen=True)
class GammaLossBreakdown:
    total: float
    gray_target: float
    local_gamma: float
    lut_smoothness: float
    lut_change: float
    highlight: float
    shadow: float
    rgb_bias: float
    step_separation: float


@dataclass(frozen=True)
class GammaEngineeringCheck:
    name: str
    status: str
    value: str
    limit: str
    message: str


@dataclass(frozen=True)
class GammaCurveHealth:
    status: str
    checks: tuple[GammaEngineeringCheck, ...]
    monotonic: bool
    reversal_count: int
    maximum_jump: int
    quantization_error: float


@dataclass(frozen=True)
class GammaZoneResult:
    zone: int
    used: bool
    status: str
    pixel_before: float
    pixel_target: float
    pixel_after: float
    density_before: float
    density_target: float
    density_after: float
    error_before: float
    error_after: float
    improvement_percent: Optional[float]
    local_gamma_before: Optional[float]
    local_gamma_target: Optional[float]
    local_gamma_after: Optional[float]


@dataclass(frozen=True)
class GammaPairResult:
    from_zone: int
    to_zone: int
    delta_before: float
    delta_target: float
    delta_after: float
    before_distinguishable: bool
    after_distinguishable: bool
    target_required: bool


@dataclass(frozen=True)
class GammaMetrics:
    global_gamma_before: float
    global_gamma_target: float
    global_gamma_after: float
    rmse_before: float
    rmse_after: float
    maximum_error_before: float
    maximum_error_after: float
    local_gamma_error_before: float
    local_gamma_error_after: float
    rgb_gray_deviation_before: float
    rgb_gray_deviation_after: float
    distinguishable_before: int
    distinguishable_after: int
    distinguishable_target: int


@dataclass(frozen=True)
class GammaModuleDiagnosis:
    module: str
    confidence: float
    severity: str
    root_cause: str
    evidence: tuple[str, ...]
    action: str


@dataclass(frozen=True)
class GammaOptimizationResult:
    region_index: int
    selected_zones: tuple[int, ...]
    before_r: GammaLUT
    before_g: GammaLUT
    before_b: GammaLUT
    after_r: GammaLUT
    after_g: GammaLUT
    after_b: GammaLUT
    target_lut: tuple[float, ...]
    zone_results: tuple[GammaZoneResult, ...]
    pair_results: tuple[GammaPairResult, ...]
    metrics: GammaMetrics
    loss_before: GammaLossBreakdown
    loss_after: GammaLossBreakdown
    health: GammaCurveHealth
    applied_strength: float
    rgb_mode: str
    target_gamma_factor: float
    lut_length: int
    maximum_value: int
    diagnostics: tuple[GammaModuleDiagnosis, ...] = ()
    explainability: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
