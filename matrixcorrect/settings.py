from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from .models import OptimizationConfig


SETTINGS_VERSION = 1


def application_data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "MatrixCorrect"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "MatrixCorrect"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "matrixcorrect"


@dataclass(frozen=True)
class AppSettings:
    optimization: OptimizationConfig = field(default_factory=OptimizationConfig)
    composition: str = "pre"
    show_motion: bool = True
    last_report_format: str = "html"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SETTINGS_VERSION,
            "optimization": self.optimization.to_dict(),
            "composition": self.composition,
            "show_motion": self.show_motion,
            "last_report_format": self.last_report_format,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AppSettings":
        composition = str(payload.get("composition", "pre"))
        if composition not in {"pre", "post_transposed"}:
            composition = "pre"
        report_format = str(payload.get("last_report_format", "html")).lower()
        if report_format not in {"csv", "html", "pdf", "xlsx"}:
            report_format = "html"
        optimization_payload = payload.get("optimization", {})
        if not isinstance(optimization_payload, dict):
            optimization_payload = {}
        return cls(
            optimization=OptimizationConfig.from_dict(optimization_payload),
            composition=composition,
            show_motion=bool(payload.get("show_motion", True)),
            last_report_format=report_format,
        )


def load_settings(path: Optional[Union[str, Path]] = None) -> AppSettings:
    settings_path = Path(path) if path is not None else application_data_dir() / "settings.json"
    if not settings_path.exists():
        return AppSettings()
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return AppSettings()
        return AppSettings.from_dict(payload)
    except (OSError, ValueError, TypeError):
        return AppSettings()


def save_settings(settings: AppSettings, path: Optional[Union[str, Path]] = None) -> Path:
    settings_path = Path(path) if path is not None else application_data_dir() / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_suffix(settings_path.suffix + ".tmp")
    temporary.write_text(json.dumps(settings.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(settings_path)
    return settings_path
