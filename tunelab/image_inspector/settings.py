from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Union

from ..ccm.settings import application_data_dir
from .constants import DEFAULT_MATCH_SEARCH_RANGE, MATCH_CONFIDENCE_RELIABLE, MATCH_SEARCH_RANGES


@dataclass(frozen=True)
class ImageInspectorSettings:
    last_directory: str = ""
    search_range: int = DEFAULT_MATCH_SEARCH_RANGE
    match_threshold: float = MATCH_CONFIDENCE_RELIABLE
    show_histogram: bool = True
    show_luminance_histogram: bool = False
    show_exif: bool = True
    live_pixel: bool = True
    default_roi_name: str = "ROI 1"
    window_geometry: str = ""
    panel_ratio: float = 0.24
    include_full_path: bool = False

    def validated(self) -> "ImageInspectorSettings":
        search_range = self.search_range if self.search_range in MATCH_SEARCH_RANGES else DEFAULT_MATCH_SEARCH_RANGE
        threshold = min(1.0, max(0.0, float(self.match_threshold)))
        panel_ratio = min(0.4, max(0.15, float(self.panel_ratio)))
        roi_name = self.default_roi_name.strip() or "ROI 1"
        return ImageInspectorSettings(
            last_directory=str(self.last_directory),
            search_range=search_range,
            match_threshold=threshold,
            show_histogram=bool(self.show_histogram),
            show_luminance_histogram=bool(self.show_luminance_histogram),
            show_exif=bool(self.show_exif),
            live_pixel=bool(self.live_pixel),
            default_roi_name=roi_name,
            window_geometry=str(self.window_geometry),
            panel_ratio=panel_ratio,
            include_full_path=bool(self.include_full_path),
        )


def _settings_path(path: Optional[Union[str, Path]]) -> Path:
    return Path(path) if path is not None else application_data_dir() / "image_inspector_settings.json"


def load_image_inspector_settings(path: Optional[Union[str, Path]] = None) -> ImageInspectorSettings:
    source = _settings_path(path)
    if not source.exists():
        return ImageInspectorSettings()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        values: Any = payload.get("values", payload) if isinstance(payload, dict) else {}
        if not isinstance(values, dict):
            return ImageInspectorSettings()
        allowed = set(ImageInspectorSettings.__dataclass_fields__)
        return ImageInspectorSettings(**{key: value for key, value in values.items() if key in allowed}).validated()
    except (OSError, TypeError, ValueError):
        return ImageInspectorSettings()


def save_image_inspector_settings(
    settings: ImageInspectorSettings,
    path: Optional[Union[str, Path]] = None,
) -> Path:
    destination = _settings_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "values": asdict(settings.validated()),
        "descriptions": {
            "search_range": "选区锚点映射中心附近的匹配搜索范围。",
            "match_threshold": "低于该 NCC 分数时禁止输出确定性颜色结论。",
            "show_histogram": "是否默认显示全部图片的 RGB 直方图。",
            "show_luminance_histogram": "是否默认显示全部图片的亮度直方图。",
            "show_exif": "是否默认显示全部图片的 EXIF 信息。",
            "panel_ratio": "右侧分析数据栏占主工作区宽度的比例。",
            "include_full_path": "CSV 是否包含完整本地路径；关闭时仅写文件名。",
        },
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination
