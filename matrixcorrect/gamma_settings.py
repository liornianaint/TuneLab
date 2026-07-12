from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Union

from .gamma_models import GammaOptimizationConfig
from .settings import application_data_dir


GAMMA_SETTINGS_VERSION = 1


def _config_from_payload(payload: dict[str, Any]) -> GammaOptimizationConfig:
    values = payload.get("values", payload)
    if isinstance(values, dict) and isinstance(values.get("optimization"), dict):
        values = values["optimization"]
    if not isinstance(values, dict):
        values = {}
    allowed = set(GammaOptimizationConfig.__dataclass_fields__)
    config = GammaOptimizationConfig(**{key: value for key, value in values.items() if key in allowed})
    config.validate()
    return config


def gamma_settings_payload(config: GammaOptimizationConfig) -> dict[str, Any]:
    return {
        "version": GAMMA_SETTINGS_VERSION,
        "values": {"optimization": asdict(config)},
        "descriptions": {
            "target_gamma": "Gamma 提亮系数；1.0 保持标称亮度，数值越大曲线越亮。",
            "target_step_count": "目标连续可识别阶数；null 表示保持当前识别阶数且禁止退化。",
            "maximum_adjustment": "最大 LUT 调整强度，范围 0~1。",
            "highlight_protection": "高光端曲线保护强度，范围 0~1。",
            "shadow_protection": "暗部端曲线保护强度，范围 0~1。",
            "rgb_mode": "linked 保持中性灰；independent 允许受 RGB 偏差门禁约束的独立调整。",
            "threshold": "相邻灰阶可区分所需最小 ΔPixel。",
            "range_mode": "auto、all 或 manual 灰阶拟合范围。",
            "manual_start_zone": "手动拟合起始 Zone。",
            "manual_end_zone": "手动拟合结束 Zone。",
        },
    }


def load_gamma_settings(path: Optional[Union[str, Path]] = None) -> GammaOptimizationConfig:
    settings_path = Path(path) if path is not None else application_data_dir() / "gamma_settings.json"
    if not settings_path.exists():
        return GammaOptimizationConfig()
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        return _config_from_payload(payload if isinstance(payload, dict) else {})
    except (OSError, TypeError, ValueError):
        return GammaOptimizationConfig()


def save_gamma_settings(
    config: GammaOptimizationConfig,
    path: Optional[Union[str, Path]] = None,
) -> Path:
    config.validate()
    settings_path = Path(path) if path is not None else application_data_dir() / "gamma_settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_suffix(settings_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(gamma_settings_payload(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(settings_path)
    return settings_path
